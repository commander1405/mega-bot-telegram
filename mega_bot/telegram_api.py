from __future__ import annotations
import aiohttp
import asyncio
import logging
import json
import os
from typing import Any

log = logging.getLogger(__name__)

class TelegramAPIError(RuntimeError):
    pass

class TelegramAPI:
    """Thin async client for Telegram Bot API (https://api.telegram.org).

    Railway / non-Iran networks sometimes drop TCP to Telegram. We:
    - separate connect timeout from long-poll read timeout
    - recreate the HTTP session after connection failures
    - retry getUpdates with short backoff
    - allow override via TELEGRAM_API_BASE env (e.g. https://api.telegram.org)
    """

    def __init__(self, token: str, timeout: int = 40):
        base_host = (os.getenv('TELEGRAM_API_BASE') or 'https://api.telegram.org').rstrip('/')
        self.token = token
        self.base = f'{base_host}/bot{token}/'
        self._default_timeout = timeout
        # Default session timeout: generous total, but fail fast on dead network.
        self.timeout = aiohttp.ClientTimeout(total=timeout, connect=8, sock_connect=8, sock_read=timeout)
        self.session: aiohttp.ClientSession | None = None
        self._consecutive_failures = 0

    def _redact(self, msg: str) -> str:
        """Never leak the bot token into logs."""
        if not msg:
            return msg
        return msg.replace(self.token, '***TOKEN***').replace(self.base, 'https://…/bot***/')


    async def start(self) -> None:
        await self._ensure_session(force=True)

    async def _ensure_session(self, force: bool = False) -> aiohttp.ClientSession:
        if self.session and not self.session.closed and not force:
            return self.session
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
        connector = aiohttp.TCPConnector(
            limit=20,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=False,
            ssl=True,
        )
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def call(self, method: str, payload: dict[str, Any] | None = None, *, retries: int = 1) -> Any:
        """POST to Telegram Bot API.

        Critical: never swallow asyncio.CancelledError — handler timeouts cancel
        in-flight sends; catching CancelledError and recreating the shared session
        would break concurrent getUpdates and other requests.
        """
        session = await self._ensure_session()
        last_error: Exception | None = None
        req_timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_connect=8, sock_read=12)
        for attempt in range(retries + 1):
            try:
                async with session.post(
                    self.base + method,
                    json=payload or {},
                    timeout=req_timeout,
                ) as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        log.error(
                            '%s non-JSON HTTP %s body=%r',
                            method, response.status, raw[:200],
                        )
                        raise TelegramAPIError(f'{method}: HTTP {response.status}: non-JSON body')
                    if not isinstance(data, dict):
                        data = {'ok': False, 'description': str(data)}
                    if response.status >= 500 and attempt < retries:
                        log.warning('%s HTTP %s — retry', method, response.status)
                        await asyncio.sleep(1 + attempt)
                        continue
                    if response.status >= 400 or not data.get('ok', False):
                        desc = data.get('description') or data.get('error') or raw[:200]
                        log.error(
                            '%s API reject HTTP %s: %s',
                            method, response.status, self._redact(str(desc))[:300],
                        )
                        raise TelegramAPIError(f'{method}: HTTP {response.status}: {desc}')
                    self._consecutive_failures = 0
                    return data.get('result', data)
            except asyncio.CancelledError:
                # Propagate cancellations from wait_for / task cancel — do not rebuild session.
                raise
            except TelegramAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning(
                    '%s transport error (attempt %s): %s',
                    method, attempt + 1, type(exc).__name__,
                )
                await self._ensure_session(force=True)
                session = self.session  # type: ignore
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                break
        raise TelegramAPIError(self._redact(f'{method}: {type(last_error).__name__}: {last_error!r}'))

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        """Long-poll getUpdates.

        Connect/sock_connect fail fast; sock_read waits for the poll window.
        On network failure the session is recreated so the next loop can recover.
        """
        session = await self._ensure_session()
        params: dict[str, str] = {
            'timeout': str(max(1, int(timeout))),
            'limit': '100',
            # Explicit types so group messages/callbacks are never filtered by a previous setting
            'allowed_updates': '["message","callback_query","my_chat_member","edited_message"]',
        }
        if offset is not None:
            params['offset'] = str(offset)

        # total must cover long poll; connect must not wait the full poll window.
        request_timeout = aiohttp.ClientTimeout(
            total=max(timeout + 25, 50),
            connect=15,
            sock_connect=15,
            sock_read=max(timeout + 15, 40),
        )

        last_error: Exception | None = None
        # One quick retry with POST if GET cannot connect (some gateways differ).
        methods = ('GET', 'POST')
        for attempt, http_method in enumerate(methods):
            try:
                if http_method == 'GET':
                    cm = session.get(self.base + 'getUpdates', params=params, timeout=request_timeout)
                else:
                    cm = session.post(self.base + 'getUpdates', json={
                        'timeout': int(timeout),
                        'limit': 100,
                        'allowed_updates': ['message', 'callback_query', 'my_chat_member', 'edited_message'],
                        **({'offset': offset} if offset is not None else {}),
                    }, timeout=request_timeout)
                async with cm as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise TelegramAPIError(
                            f'getUpdates: non-JSON response (HTTP {response.status}): {raw[:300]!r}'
                        ) from exc

                    if response.status >= 400 or not data.get('ok', response.status < 400):
                        description = data.get('description') or data.get('error') or data
                        raise TelegramAPIError(f'getUpdates: HTTP {response.status}: {description}')
                    result = data.get('result', [])
                    if not isinstance(result, list):
                        raise TelegramAPIError(f'getUpdates: expected a list, received {type(result).__name__}')
                    self._consecutive_failures = 0
                    return result
            except TelegramAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                self._consecutive_failures += 1
                log.warning(
                    'getUpdates %s failed (failures=%s): %s',
                    http_method, self._consecutive_failures, type(exc).__name__,
                )
                await self._ensure_session(force=True)
                session = self.session  # type: ignore
                if attempt + 1 < len(methods):
                    await asyncio.sleep(0.5)
                    continue
                break

        raise TelegramAPIError(
            self._redact(f'getUpdates network error: {type(last_error).__name__}: {last_error!r}')
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = 'Markdown',
        *,
        fast: bool = False,
    ) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'text': text}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if reply_to_message_id is not None:
            payload['reply_to_message_id'] = reply_to_message_id
        if parse_mode:
            payload['parse_mode'] = parse_mode
        # Group replies must not wait for long connect retries — fail fast.
        retries = 0 if fast else 1
        return await self.call('sendMessage', payload, retries=retries)

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = 'Markdown') -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if parse_mode:
            payload['parse_mode'] = parse_mode
        return await self.call('editMessageText', payload)

    async def answer_callback(
        self,
        callback_id: str,
        text: str = '',
        *,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {'callback_query_id': callback_id}
        if text:
            payload['text'] = text
        if show_alert:
            payload['show_alert'] = True
        await self.call('answerCallbackQuery', payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    async def pin_message(self, chat_id: int, message_id: int) -> None:
        await self.call('pinChatMessage', {'chat_id': chat_id, 'message_id': message_id, 'disable_notification': True})

    async def get_file_bytes(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
        """Download a Bot API file, enforcing a conservative memory limit."""
        info = await self.call('getFile', {'file_id': file_id})
        if not isinstance(info, dict):
            raise TelegramAPIError('getFile returned invalid payload')
        # Some gateways nest under result again
        if 'file_path' not in info and isinstance(info.get('result'), dict):
            info = info['result']
        file_path = info.get('file_path')
        if not file_path:
            raise TelegramAPIError(f'getFile did not return file_path: {list(info.keys())}')
        # Host without embedding token in path twice
        host = self.base.split('/bot', 1)[0]
        url = f'{host}/file/bot{self.token}/{file_path}'
        session = await self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60, connect=15, sock_connect=15)) as response:
            if response.status >= 400:
                raise TelegramAPIError(f'file download HTTP {response.status}')
            length = int(response.headers.get('Content-Length') or 0)
            if length and length > max_bytes:
                raise TelegramAPIError(f'file too large: {length} bytes')
            data = await response.read()
            if len(data) > max_bytes:
                raise TelegramAPIError(f'file too large: {len(data)} bytes')
            name = file_path.rsplit('/', 1)[-1]
            return data, name


    async def send_voice_bytes(
        self,
        chat_id: int,
        data: bytes,
        filename: str = 'voice.ogg',
        caption: str | None = None,
        duration: int | None = None,
    ) -> dict:
        """Upload OGG/Opus (or compatible) voice note via sendVoice."""
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field('chat_id', str(chat_id))
        if caption:
            form.add_field('caption', caption)
        if duration is not None:
            form.add_field('duration', str(int(duration)))
        # Telegram/Telegram expect OGG OPUS for voice messages
        ctype = 'audio/ogg'
        if filename.endswith('.mp3'):
            ctype = 'audio/mpeg'
        form.add_field('voice', data, filename=filename, content_type=ctype)
        async with session.post(
            self.base + 'sendVoice',
            data=form,
            timeout=aiohttp.ClientTimeout(total=120, connect=20),
        ) as response:
            raw = await response.text()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TelegramAPIError(f'sendVoice returned non-JSON HTTP {response.status}') from exc
            if response.status >= 400 or not result.get('ok', response.status < 400):
                raise TelegramAPIError(f"sendVoice: HTTP {response.status}: {result.get('description', result)}")
            return result.get('result', result)

    async def send_audio_bytes(
        self,
        chat_id: int,
        data: bytes,
        filename: str = 'audio.mp3',
        caption: str | None = None,
    ) -> dict:
        """Fallback: send as audio file (mp3/ogg) via sendAudio."""
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field('chat_id', str(chat_id))
        if caption:
            form.add_field('caption', caption)
        ctype = 'audio/mpeg' if filename.endswith('.mp3') else 'audio/ogg'
        form.add_field('audio', data, filename=filename, content_type=ctype)
        async with session.post(
            self.base + 'sendAudio',
            data=form,
            timeout=aiohttp.ClientTimeout(total=120, connect=20),
        ) as response:
            raw = await response.text()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TelegramAPIError(f'sendAudio returned non-JSON HTTP {response.status}') from exc
            if response.status >= 400 or not result.get('ok', response.status < 400):
                raise TelegramAPIError(f"sendAudio: HTTP {response.status}: {result.get('description', result)}")
            return result.get('result', result)

    async def send_animation_bytes(self, chat_id: int, data: bytes, filename: str = 'mega-bot.gif', caption: str = '') -> dict:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field('chat_id', str(chat_id))
        if caption:
            form.add_field('caption', caption)
        form.add_field('animation', data, filename=filename, content_type='image/gif')
        async with session.post(self.base + 'sendAnimation', data=form, timeout=aiohttp.ClientTimeout(total=120, connect=20)) as response:
            raw = await response.text()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TelegramAPIError(f'sendAnimation returned non-JSON HTTP {response.status}') from exc
            if response.status >= 400 or not result.get('ok', response.status < 400):
                raise TelegramAPIError(f"sendAnimation: HTTP {response.status}: {result.get('description', result)}")
            return result.get('result', result)


    async def send_photo_bytes(
        self,
        chat_id: int,
        data: bytes,
        filename: str = 'stats.png',
        caption: str | None = None,
    ) -> dict:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field('chat_id', str(chat_id))
        if caption:
            form.add_field('caption', caption)
        form.add_field('photo', data, filename=filename, content_type='image/png')
        async with session.post(
            self.base + 'sendPhoto',
            data=form,
            timeout=aiohttp.ClientTimeout(total=120, connect=20),
        ) as response:
            raw = await response.text()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TelegramAPIError(f'sendPhoto returned non-JSON HTTP {response.status}') from exc
            if response.status >= 400 or not result.get('ok', response.status < 400):
                raise TelegramAPIError(f"sendPhoto: HTTP {response.status}: {result.get('description', result)}")
            return result.get('result', result)

    async def send_photo(self, chat_id: int, photo: str, caption: str | None = None, reply_markup: dict | None = None, parse_mode: str | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'photo': photo}
        if caption:
            payload['caption'] = caption
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if parse_mode:
            payload['parse_mode'] = parse_mode
        return await self.call('sendPhoto', payload, retries=0)

    async def send_video(self, chat_id: int, video: str, caption: str | None = None, reply_markup: dict | None = None, parse_mode: str | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'video': video}
        if caption:
            payload['caption'] = caption
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if parse_mode:
            payload['parse_mode'] = parse_mode
        return await self.call('sendVideo', payload, retries=0)

    async def send_document(self, chat_id: int, document: str, caption: str | None = None, reply_markup: dict | None = None, parse_mode: str | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'document': document}
        if caption:
            payload['caption'] = caption
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if parse_mode:
            payload['parse_mode'] = parse_mode
        return await self.call('sendDocument', payload, retries=0)

    async def send_animation_file_id(self, chat_id: int, animation: str, caption: str | None = None, reply_markup: dict | None = None, parse_mode: str | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'animation': animation}
        if caption:
            payload['caption'] = caption
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        if parse_mode:
            payload['parse_mode'] = parse_mode
        return await self.call('sendAnimation', payload, retries=0)

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, reply_markup: dict | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        return await self.call('copyMessage', payload, retries=0)

    async def ban_chat_member(self, chat_id: int, user_id: int, until_date: int | None = None) -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'user_id': user_id}
        if until_date is not None:
            payload['until_date'] = until_date
        return await self.call('banChatMember', payload)

    async def unban_chat_member(self, chat_id: int, user_id: int) -> dict:
        return await self.call('unbanChatMember', {'chat_id': chat_id, 'user_id': user_id})

    async def restrict_chat_member(self, chat_id: int, user_id: int, permissions: dict, until_date: int | None = None) -> dict:
        """Restrict a group member (e.g. mute). permissions follows Bot API ChatPermissions."""
        payload: dict[str, Any] = {'chat_id': chat_id, 'user_id': user_id, 'permissions': permissions}
        if until_date is not None:
            payload['until_date'] = until_date
        return await self.call('restrictChatMember', payload)

    async def set_chat_permissions(self, chat_id: int, permissions: dict) -> dict:
        """Set default permissions for all non-admin members (group-wide lock/unlock)."""
        return await self.call('setChatPermissions', {
            'chat_id': chat_id,
            'permissions': permissions,
        })
