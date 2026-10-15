from __future__ import annotations
import asyncio, json, logging, random, re, secrets, string, time
from collections import deque
from datetime import datetime, timezone, date, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from .telegram_api import TelegramAPI, TelegramAPIError
from .database import Database
from .media_tools import convert_media, MAX_INPUT_BYTES
from .keyboards import USER_KEYBOARD, inline, menu_page, main_admin_menu, user_actions, url_button, merge_inline
from . import prices as price_mod
from . import fun_content
from .profanity import contains_profanity
from . import schedule_lock as schlock
from . import levels as level_mod
from . import tts as tts_mod
from . import font_styles as font_mod
from . import time_literary as time_mod
from . import hafez_content as hafez_mod
from . import jokes_content as jokes_mod
from . import stats_dashboard as dash
from config import OWNER_ID, BOT_VERSION, SUPPORT_TEXT, DONATION_TEXT, BOT_USERNAME

log = logging.getLogger(__name__)

# Points awarded for each newly learned phrase (not for edits/updates).
LEARN_POINTS = 5
# Whisper rate-limit: max sends per user in this window (seconds).
WHISPER_RATE_LIMIT = 5
WHISPER_RATE_WINDOW = 60.0

DEFAULT_PERMS = {
    'stats': True, 'search': True, 'case_lookup': True,
    'unban_user': True, 'unban_group': True,
    'ban_user': False, 'ban_group': False,
    'edit_about': False, 'edit_team': False,
    'manage_admins': False, 'settings': False,
}
PERMISSION_LABELS = {
    'stats':'مشاهده آمار', 'search':'جست‌وجوی کاربران و گروه‌ها', 'case_lookup':'بررسی کد پیگیری',
    'unban_user':'رفع محدودیت کاربر', 'unban_group':'رفع محدودیت گروه',
    'ban_user':'محدودسازی کاربر', 'ban_group':'محدودسازی گروه', 'edit_about':'ویرایش درباره ما',
    'edit_team':'مدیریت تیم توسعه‌دهنده', 'manage_admins':'مدیریت ادمین‌ها', 'settings':'تنظیمات ربات'
}

class MegaBot:
    def __init__(self, api: TelegramAPI, db: Database):
        self.api, self.db = api, db
        self.offset: int | None = None
        self.states: dict[int, dict[str, Any]] = {}
        self.game_cache: dict[str, dict[str, Any]] = {}
        # Strong refs so update tasks are not GC'd under load (esp. after admin promotion).
        self._bg_tasks: set[asyncio.Task] = set()
        self._bot_member_status: dict[int, str] = {}
        self._update_sem = asyncio.Semaphore(8)
        # Ad button cache: (enabled, url, title) — refreshed from DB
        self._ad_cache: tuple[bool, str, str] | None = None
        self._ad_cache_ts: float = 0.0
        # Broadcast campaign control (in-memory; cancelled on restart)
        self._broadcast_cancel: set[int] = set()  # admin uids who requested cancel
        self._broadcast_running: bool = False
        # Whisper rate-limit: user_id -> list of send timestamps
        self._whisper_sends: dict[int, list[float]] = {}
        # Recent group message ids for purge: chat_id -> deque of message_id
        self._recent_mids: dict[int, deque] = {}
        # Pending purge confirmations: token -> {chat_id, uid, mids, expires}
        self._purge_pending: dict[str, dict[str, Any]] = {}
        self._ephemeral_tasks: set[asyncio.Task] = set()
        self._maintenance_cache: tuple[bool, float] = (False, 0.0)
        self._stats_cache: dict | None = None
        self._time_lock_cache: dict[int, tuple[bool, float]] = {}  # chat_id -> (locked, expires_ts)
        self._lock_announce_cooldown: dict[int, float] = {}  # chat_id -> last announce ts
        # Activity anti-spam: (chat_id, user_id) -> list[timestamps]
        self._xp_msg_times: dict[tuple[int, int], list[float]] = {}
        self._stats_cache_ts: float = 0.0

    async def _get_ad_config(self) -> tuple[bool, str, str]:
        """Return (enabled, url, title). Cached for 30s to avoid DB hit on every send."""
        now = time.time()
        if self._ad_cache is not None and (now - self._ad_cache_ts) < 30:
            return self._ad_cache
        try:
            enabled = bool(await self.db.get_setting('ad_enabled', False))
            url = str(await self.db.get_setting('ad_url', '') or '')
            title = str(await self.db.get_setting('ad_title', '') or '')
        except Exception:
            enabled, url, title = False, '', ''
        self._ad_cache = (enabled, url, title)
        self._ad_cache_ts = now
        return self._ad_cache

    def _invalidate_ad_cache(self) -> None:
        self._ad_cache = None
        self._ad_cache_ts = 0.0


    async def _metric_out(self, success: bool) -> None:
        try:
            day = await self.db.tehran_today()
            if success:
                await self.db.metric_inc('msg_out_ok', 1, day=day)
                await self.db.metric_set('last_out_ts', int(time.time()))
            else:
                await self.db.metric_inc('msg_out_fail', 1, day=day)
                await self.db.metric_set('last_err_ts', int(time.time()))
                await self.db.metric_inc('err_total', 1, day=day)
            self._invalidate_stats_cache()
        except Exception:
            log.debug('metric_out failed', exc_info=True)

    async def _metric_in(self, chat: dict, responded: bool = False) -> None:
        try:
            day = await self.db.tehran_today()
            ctype = (chat.get('type') or 'private')
            await self.db.metric_inc('msg_in', 1, day=day)
            if ctype in ('group', 'supergroup'):
                await self.db.metric_inc('msg_in_group', 1, day=day)
            else:
                await self.db.metric_inc('msg_in_private', 1, day=day)
            if responded:
                await self.db.metric_inc('msg_in_answered', 1, day=day)
            else:
                await self.db.metric_inc('msg_in_unanswered', 1, day=day)
            await self.db.metric_set('last_in_ts', int(time.time()))
            self._invalidate_stats_cache()
        except Exception:
            log.debug('metric_in failed', exc_info=True)

    async def _cached_stats(self) -> dict:
        """Short-lived stats cache (social replies). Invalidated on donation total change."""
        now = time.time()
        if self._stats_cache is not None and (now - self._stats_cache_ts) < 20:
            return self._stats_cache
        try:
            s = await self.db.stats()
            if not isinstance(s, dict):
                s = {}
            s['_ok'] = True
            # Merge persistent message counters when available
            try:
                m = await self.db.metrics_get_many(['msg_in', 'msg_out_ok', 'msg_out_fail'])
                s['msg_in'] = m.get('msg_in')
                s['msg_out_ok'] = m.get('msg_out_ok')
                s['msg_out_fail'] = m.get('msg_out_fail')
            except Exception:
                pass
            self._stats_cache = s
        except Exception:
            log.warning('stats() failed for social cache', exc_info=True)
            # Do not invent zeros — keep previous good cache or empty without _ok
            if not self._stats_cache or not self._stats_cache.get('_ok'):
                self._stats_cache = {}
        self._stats_cache_ts = now
        return self._stats_cache or {}

    def _invalidate_stats_cache(self) -> None:
        self._stats_cache = None
        self._stats_cache_ts = 0.0


    async def _with_ad_keyboard(self, keyboard: dict | None, *, skip_ad: bool = False) -> dict | None:
        """Attach ad URL button to an inline keyboard when ads are enabled.

        - Never mutates the original keyboard.
        - Skips reply keyboards (groups strip them anyway via _group_keyboard).
        - skip_ad=True for GIF sends and internal probes.
        """
        if skip_ad:
            return keyboard
        enabled, url, title = await self._get_ad_config()
        if not enabled or not url or not title:
            return keyboard
        ad_btn = {'text': title, 'url': url}
        if keyboard and 'inline_keyboard' in keyboard:
            return merge_inline(keyboard, [ad_btn])
        if keyboard and 'keyboard' in keyboard:
            # Reply keyboard: keep as-is; ad only on inline
            return keyboard
        return {'inline_keyboard': [[ad_btn]]}

    async def safe_send(self, chat_id: int, text: str, keyboard: dict | None = None, reply_to: int | None = None, parse_mode: str | None = 'Markdown', *, skip_ad: bool = False, permanent: bool = False) -> dict | None:
        """Send with progressive fallbacks. Prefer no-reply first for reliability in groups.

        Logs every attempt outcome. Uses fast=True (no long retries) so group handlers
        cannot burn the 15s update budget on a single stuck TCP connect.
        When ads are enabled, attaches the ad URL button to inline keyboards.
        """
        keyboard = await self._with_ad_keyboard(keyboard, skip_ad=skip_ad)
        plain = (text or '').replace('*', '').replace('`', '').replace('_', '')
        # IMPORTANT: try WITHOUT reply_to first — reply_to is a common Telegram failure mode
        # and previously caused the whole handler to sit on a long timeout before fallback.
        attempts: list[tuple[str, dict | None, int | None, str | None]] = [
            (text, keyboard, None, parse_mode),
            (text, keyboard, None, None),
            (text, None, None, None),
            (plain, None, None, None),
        ]
        if reply_to is not None:
            # Prefer reply when requested (group UX), but keep no-reply fallbacks.
            attempts = [
                (text, keyboard, reply_to, parse_mode),
                (text, keyboard, None, parse_mode),
                (text, keyboard, reply_to, None),
                (text, keyboard, None, None),
                (text, None, None, None),
                (plain, None, None, None),
            ]
        last_exc: Exception | None = None
        for idx, (body, kb, rt, pm) in enumerate(attempts):
            try:
                result = await self.api.send_message(
                    chat_id, body, kb, rt, pm, fast=True,
                )
                log.info('sendMessage ok chat=%s attempt=%s', chat_id, idx)
                try:
                    await self._metric_out(True)
                except Exception:
                    pass
                return self._maybe_ephemeral_result(chat_id, result, permanent=permanent)
            except Exception as exc:
                last_exc = exc
                log.warning(
                    'sendMessage fail chat=%s attempt=%s err=%s detail=%s',
                    chat_id, idx, type(exc).__name__, str(exc)[:200],
                )
        log.error('sendMessage gave up chat=%s err=%s', chat_id, type(last_exc).__name__ if last_exc else None)
        try:
            await self._metric_out(False)
        except Exception:
            pass
        return None

    def _group_keyboard(self, keyboard: dict | None) -> dict | None:
        """Reply keyboards often break in Telegram groups; keep only inline keyboards there."""
        if not keyboard:
            return None
        if 'inline_keyboard' in keyboard:
            return keyboard
        # Strip reply-keyboard (USER_KEYBOARD) when used in groups
        return None

    @staticmethod
    def _is_group_chat(chat: dict) -> bool:
        t = (chat.get('type') or '').lower().strip()
        if t in ('group', 'supergroup', 'megagroup', 'chat'):
            return True
        if t in ('private', 'channel'):
            return False
        # Telegram sometimes omits type or uses unexpected values; title implies group/channel.
        if chat.get('title') and t != 'private':
            return True
        return False

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        """Strip @botusername from /cmd@bot and trailing mentions for matching."""
        t = (text or '').strip()
        # /start@MyBot -> /start
        t = re.sub(r'^(/[\w\u0600-\u06FF]+)@\w+', r'\1', t)
        return t.strip()


    def _is_private_chat_id(self, chat_id: int) -> bool:
        """Telegram: private chats have positive ids; groups/supergroups negative."""
        try:
            return int(chat_id) > 0
        except Exception:
            return True

    async def _is_maintenance(self) -> bool:
        now = time.time()
        cached, exp = self._maintenance_cache
        if exp > now:
            return cached
        try:
            val = await self.db.get_setting('maintenance_mode', False)
            on = bool(val) if not isinstance(val, str) else val.strip().lower() in ('1', 'true', 'yes', 'on')
        except Exception:
            on = False
        self._maintenance_cache = (on, now + 5.0)
        return on

    def _schedule_ephemeral_delete(self, chat_id: int, message_id: int | None) -> None:
        """Delete a group bot message after a random 50–90s delay."""
        if not message_id or self._is_private_chat_id(chat_id):
            return
        delay = random.uniform(50.0, 90.0)

        async def _job() -> None:
            try:
                await asyncio.sleep(delay)
                await self.api.delete_message(chat_id, int(message_id))
                log.info('ephemeral delete ok chat=%s mid=%s after=%.0fs', chat_id, message_id, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug('ephemeral delete fail chat=%s mid=%s: %s', chat_id, message_id, type(exc).__name__)

        try:
            task = asyncio.create_task(_job())
            self._ephemeral_tasks.add(task)
            task.add_done_callback(self._ephemeral_tasks.discard)
        except Exception:
            log.debug('could not schedule ephemeral delete', exc_info=True)

    def _maybe_ephemeral_result(self, chat_id: int, result: dict | None, *, permanent: bool) -> dict | None:
        if permanent or not result or self._is_private_chat_id(chat_id):
            return result
        mid = result.get('message_id') if isinstance(result, dict) else None
        if mid is None and isinstance(result, dict):
            inner = result.get('result')
            if isinstance(inner, dict):
                mid = inner.get('message_id')
        if mid:
            self._schedule_ephemeral_delete(chat_id, int(mid))
        return result

    async def safe_edit(self, chat_id: int, message_id: int, text: str, keyboard: dict | None = None) -> None:
        try:
            await self.api.edit_message(chat_id, message_id, text, keyboard)
        except Exception as exc:
            log.warning('editMessageText failed: %s', exc)
            await self.safe_send(chat_id, text, keyboard)

    async def run(self, poll_timeout: int = 25) -> None:
        await self._startup_checks()
        self._started_at = time.time()
        try:
            await self.db.metric_inc('bot_restarts', 1)
            await self.db.metric_set('last_start_ts', int(self._started_at))
        except Exception:
            pass
        # Single background scheduler for timed group locks (not per-group tasks).
        if not getattr(self, '_sched_task', None) or self._sched_task.done():
            self._sched_task = asyncio.create_task(self._schedule_lock_loop(), name='group-lock-sched')
            self._bg_tasks.add(self._sched_task)
            self._sched_task.add_done_callback(self._bg_tasks.discard)
        log.info('MEGA BOT polling started (target host api.telegram.org)')
        backoff = 1.0
        while True:
            try:
                updates = await self.api.get_updates(self.offset, poll_timeout)
                backoff = 1.0  # reset after a successful poll
                for update in updates:
                    self.offset = int(update.get('update_id', 0)) + 1
                    # MUST keep a strong reference to tasks; otherwise CPython may
                    # GC them mid-flight under load (typical right after the bot is
                    # promoted to group admin and message volume spikes).
                    task = asyncio.create_task(
                        self._handle_update_safe(update),
                        name=f"upd-{update.get('update_id')}",
                    )
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Network blips to api.telegram.org are common from some regions; back off without stack spam every cycle.
                msg = f'{type(exc).__name__}: {exc}'
                if 'network error' in msg or 'Timeout' in msg or 'ClientConnector' in msg:
                    log.error('Polling failed (network); retry in %.0fs — %s', backoff, msg)
                else:
                    log.exception('Polling failed; retry in %.0fs', backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


    async def _startup_checks(self) -> None:
        """Best-effort startup probes — never block polling for long.

        Railway (and many non-Iran hosts) often cannot TCP-connect to
        api.telegram.org. Fail fast and let the poll loop keep retrying.
        """
        self.bot_id = None
        self.bot_username = ''
        # Single attempt each, short overall budget.
        try:
            await asyncio.wait_for(
                self.api.call('deleteWebhook', {'drop_pending_updates': False}, retries=0),
                timeout=8.0,
            )
            log.info('deleteWebhook ok')
        except Exception as exc:
            log.warning('deleteWebhook skipped (network?): %s', type(exc).__name__)
        try:
            me = await asyncio.wait_for(self.api.call('getMe', retries=0), timeout=8.0)
            self.bot_id = int((me or {}).get('id') or 0) or None
            self.bot_username = (me or {}).get('username') or ''
            log.info(
                'getMe: @%s id=%s can_read_all_group_messages=%s',
                self.bot_username,
                self.bot_id,
                (me or {}).get('can_read_all_group_messages'),
            )
        except Exception as exc:
            log.warning(
                'getMe failed at startup (%s). Will still poll — if this keeps failing, '
                'Cannot reach api.telegram.org — check network / firewall.',
                type(exc).__name__,
            )


    async def _handle_update_safe(self, update: dict) -> None:
        uid = update.get('update_id')
        try:
            sem = getattr(self, '_update_sem', None)
            if sem is None:
                self._update_sem = asyncio.Semaphore(8)
                sem = self._update_sem
            async with sem:
                await asyncio.wait_for(self._dispatch_update(update), timeout=15.0)
        except asyncio.TimeoutError:
            log.error('Update %s timed out (15s) — skipped', uid)
        except Exception:
            log.exception('Failed to process update %s', uid)

    async def _dispatch_update(self, update: dict) -> None:
        uid = update.get('update_id')
        # Skip duplicate update_ids when possible (restart-safe via DB)
        if uid is not None:
            try:
                is_new = await self.db.mark_update_processed(int(uid))
                if not is_new:
                    log.info('UPDATE id=%s duplicate — skip', uid)
                    return
            except Exception:
                pass
        kinds = [k for k in ('message', 'callback_query', 'my_chat_member', 'edited_message', 'chat_member') if k in update]
        log.info('UPDATE id=%s kinds=%s', uid, kinds or list(update.keys())[:6])
        if 'message' in update:
            await self.on_message(update['message'])
        elif 'callback_query' in update:
            await self.on_callback(update['callback_query'])
        elif 'my_chat_member' in update:
            await self.on_my_chat_member(update['my_chat_member'])
        elif 'chat_member' in update:
            await self.on_chat_member(update['chat_member'])
        elif kinds:
            log.info('UPDATE id=%s ignored kinds=%s', uid, kinds)

    async def on_message(self, msg: dict) -> None:
        chat = msg.get('chat') or {}
        chat_id = int(chat.get('id', 0) or 0)
        if not chat_id:
            return

        # Pure service events — do not run the full pipeline (critical after bot is
        # promoted to group admin: Telegram then delivers many status messages).
        if msg.get('left_chat_member'):
            try:
                await self.db.record_member_left(int(chat.get('id') or 0), 1)
            except Exception:
                pass
            try:
                await self.goodbye_left_member(msg, chat)
            except Exception:
                log.exception('goodbye_left_member failed')
            return
        service_keys = (
            'new_chat_title', 'new_chat_photo', 'delete_chat_photo',
            'group_chat_created', 'supergroup_chat_created', 'channel_chat_created',
            'migrate_to_chat_id', 'migrate_from_chat_id', 'pinned_message',
            'message_auto_delete_timer_changed', 'connected_website',
        )
        if any(k in msg for k in service_keys):
            return
        if msg.get('new_chat_members'):
            try:
                await self.welcome_new_members(msg, chat)
            except Exception:
                log.exception('welcome_new_members failed')
            return

        # Telegram may omit "from" on some service messages; also support sender_chat.
        user = msg.get('from') or msg.get('sender') or {}
        # Real inbound message accepted for processing (not service events above)
        try:
            await self._metric_in(chat, responded=False)
        except Exception:
            pass
        if not user and msg.get('sender_chat'):
            user = {
                'id': msg['sender_chat'].get('id'),
                'first_name': msg['sender_chat'].get('title') or 'ناشناس',
                'is_bot': False,
            }
        uid = int(user.get('id', 0) or 0)
        # Never process the bot's own messages (prevents loops after group replies).
        if user.get('is_bot') or not uid:
            return
        text = (msg.get('text') or msg.get('caption') or '').strip()
        text = self._normalize_command_text(text)
        chat_type = (chat.get('type') or '')
        is_group = self._is_group_chat(chat)

        # Log every group message (critical for diagnosing admin-only failures).
        if is_group:
            bot_st = getattr(self, '_bot_member_status', {}).get(chat_id, '?')
            log.info(
                'GROUP inbound chat=%s type=%r uid=%s text=%r reply=%s bot_status=%s',
                chat_id, chat_type, uid, (text or '')[:80],
                bool(msg.get('reply_to_message')), bot_st,
            )
            if msg.get('message_id'):
                self._track_group_mid(chat_id, int(msg['message_id']))

        # /ping works in EVERY chat, BEFORE any DB or admin logic.
        # This is the primary liveness probe for group-admin regressions.
        low_fast = (text or '').lower().replace('؟', '').replace('?', '').strip()
        if text in ('/ping', 'ping', '/id', '/chatid') or low_fast in ('پینگ',):
            bot_st = getattr(self, '_bot_member_status', {}).get(chat_id, '?')
            log.info('PING reply chat=%s uid=%s bot_status=%s', chat_id, uid, bot_st)
            result = await self.safe_send(
                chat_id,
                f'pong ✅\nchat_id={chat_id}\ntype={chat_type or "?"}\nuid={uid}\ngroup={is_group}\nbot_status={bot_st}',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            if not result:
                log.error('PING safe_send returned None chat=%s', chat_id)
            return

        if is_group and text and (text.startswith('/start') or low_fast in ('استارت',)):
            await self.safe_send(
                chat_id,
                'سلام! من آنلاینم 🌿\nدستورها: دوز | سلام | ربات | /ping\nاگر جواب نگرفتی روی پیام ربات ریپلای کن.',
                parse_mode=None,
            )
            return

        # Timed/manual group lock — earliest enforcement (before GIF/commands)
        # /ping above remains available for diagnostics.
        if is_group and uid and uid != OWNER_ID:
            try:
                # VIP and admins are exempt (checked inside enforce + mod power)
                if await self._enforce_group_time_lock(chat_id, uid, msg):
                    return
            except Exception:
                log.exception('time-lock enforce error chat=%s', chat_id)

        try:
            await asyncio.wait_for(self.db.upsert_user(user), timeout=3.0)
        except Exception:
            log.warning('upsert_user skipped/failed for %s', uid)


                # ── Maintenance mode (owner can toggle): block all non-owner interaction
        if uid and uid != OWNER_ID:
            try:
                if await self._is_maintenance():
                    await self.safe_send(
                        chat_id,
                        '🛠️ ربات الان در *حالت تعمیرات* است.\n'
                        'احتمالاً در حال به‌روزرسانی یا رفع مشکل هستیم.\n'
                        'لطفاً کمی بعد دوباره سر بزن 🌱',
                        parse_mode='Markdown',
                        permanent=False,
                    )
                    return
            except Exception:
                log.debug('maintenance check failed', exc_info=True)

# Media conversion: reply to a photo/video/animation and write "گیف" or "گیف متن".
        replied = msg.get('reply_to_message') or {}
        if text == 'گیف' or text.startswith('گیف '):
            if not replied:
                await self.safe_send(chat_id, 'برای تبدیل، روی عکس یا ویدئو ریپلای کن و بنویس «گیف». برای نوشتن متن روی آن بنویس «گیف متن دلخواه».')
                return
            media_file_id = None
            media_name = 'image.jpg'
            if replied.get('photo'):
                photos = replied['photo']
                best = photos[-1] if isinstance(photos, list) else photos
                media_file_id = best.get('file_id')
                media_name = 'image.jpg'
            elif replied.get('video'):
                media_file_id = replied['video'].get('file_id'); media_name = replied['video'].get('file_name') or 'video.mp4'
            elif replied.get('animation'):
                media_file_id = replied['animation'].get('file_id'); media_name = replied['animation'].get('file_name') or 'animation.mp4'
            elif replied.get('document'):
                doc = replied['document']; media_file_id = doc.get('file_id'); media_name = doc.get('file_name') or 'media.bin'
            if not media_file_id:
                await self.safe_send(chat_id, 'این پیام عکس یا ویدئوی قابل پردازش ندارد.')
                return
            overlay = text[4:].strip() if text.startswith('گیف ') else ''
            await self.safe_send(chat_id, 'چند لحظه صبر کن تا برات گیف رو آماده کنم..✨️😊')
            try:
                raw, api_name = await self.api.get_file_bytes(media_file_id, MAX_INPUT_BYTES)
                result = await convert_media(raw, media_name or api_name, overlay)
                await self.api.send_animation_bytes(chat_id, result, 'mega-bot.gif', 'ساخته‌شده با MEGA BOT')
            except Exception as exc:
                log.exception('GIF conversion failed')
                await self.safe_send(chat_id, f'ساخت GIF ناموفق بود: {self._short(exc)}')
            return

        # Track recent message ids (for پاک)
        if is_group and msg.get('message_id'):
            self._track_group_mid(chat_id, int(msg['message_id']))

        # Group purge: پاک / پاک N / reply+پاک
        if is_group and text:
            handled_purge = await self._handle_purge_command(chat_id, uid, text, msg, replied)
            if handled_purge:
                return

        # Group moderation + warn commands (reply or @user / numeric id)
        if self._is_group_chat(chat) and text:
            handled_mod = await self._handle_group_mod_text(chat_id, uid, text, msg, replied)
            if handled_mod:
                return
        # ── Whisper (نجوا): reply + «نجوا متن»
        if is_group and text.startswith('نجوا'):
            await self._handle_whisper(chat_id, uid, text, msg, replied)
            return

        if self._is_group_chat(chat):
            try:
                await asyncio.wait_for(self.db.upsert_group(chat), timeout=2.0)
            except Exception:
                log.warning('upsert_group skipped for %s', chat_id)
        try:
            await asyncio.wait_for(self.db.record_usage(uid, chat, 'message'), timeout=2.0)
        except Exception:
            log.warning('record_usage skipped')

        # Per-group daily stats (best-effort)
        if is_group and uid:
            try:
                await self.db.record_group_message(chat_id, uid, msg)
            except Exception:
                pass

        # Cache profile photo in background (for stats dashboard avatars)
        if uid and user:
            try:
                asyncio.create_task(self._cache_sender_avatar(user))
            except Exception:
                pass

        # Activity XP / levels — every 2 msgs = 1 XP; spam window blocks XP
        if is_group and uid and level_mod.is_valid_activity_message(text, msg):
            try:
                uname = (user.get('first_name') or user.get('name') or '')[:80]
                grant_xp = not self._is_xp_spam(chat_id, uid)
                act = await self.db.record_activity_xp(
                    chat_id, uid,
                    grant_xp=grant_xp,
                    display_name=uname,
                    count_message=True,
                    messages_per_xp=level_mod.MESSAGES_PER_XP,
                    xp_per_grant=level_mod.XP_PER_GRANT,
                )
                # Level-up only when XP was actually granted (never on spam)
                if grant_xp and act.get('leveled_up'):
                    await self._announce_level_up(chat_id, uid, uname, int(act['level']), msg.get('message_id'))
            except Exception:
                log.debug('activity xp failed', exc_info=True)

        # Security filters (groups only; admins/owner exempt)
        if is_group and uid and uid != OWNER_ID:
            try:
                if not await self._has_group_mod_power(chat_id, uid):
                    blocked = await self._apply_security_filters(chat_id, uid, msg, text)
                    if blocked:
                        return
            except Exception:
                log.exception('security filter error chat=%s', chat_id)

        # User restrictions apply in private and group chats, while keeping case tracking accessible.
        try:
            restricted_user = await asyncio.wait_for(self.db.is_restricted('user', uid), timeout=2.0)
        except Exception:
            log.warning('is_restricted(user) skipped')
            restricted_user = None
        if restricted_user and uid != OWNER_ID and not await self.db.is_admin(uid) and text not in ('تیم من',) and not text.startswith('/admin') and not text.startswith('/owner'):
            await self.safe_send(chat_id, f"*⛔ دسترسی تو به قابلیت‌های MEGA BOT محدود شده است.*\nکد پیگیری: `#{restricted_user['case_code']}`\nبرای بررسی دلیل و مدت، بخش *تیم من* را باز کن.", USER_KEYBOARD)
            return

        # Keep group-level restriction enforced for bot features.
        if self._is_group_chat(chat):
            try:
                restricted_group = await asyncio.wait_for(self.db.is_restricted('group', chat_id), timeout=2.0)
            except Exception:
                log.warning('is_restricted(group) skipped')
                restricted_group = None
            feature_request = text.startswith('/') or text in ('دوز','بازی دوز','شیر یا خط','قوانین','پین','لیست ویژه ها','خوبی','چخبر','خدافظ','شب خوش','سلام ربات') or text.startswith(('اکو ','تنظیم قوانین ','هرکی تک')) or ('ربات' in text.lower())
            if restricted_group and feature_request and not self._is_admin_identity(uid):
                await self.safe_send(chat_id, f"*⛔ این گروه توسط مدیریت محدود شده است.*\nکد پیگیری: `#{restricted_group['case_code']}`", reply_to=msg.get('message_id'))
                return
        if text.startswith('/start'):
            await self.safe_send(chat_id, '*سلام! به MEGA BOT خوش اومدی* 🌿\nاز منوی پایین برای دیدن بخش‌ها استفاده کن.', USER_KEYBOARD)
            if not is_group:
                try:
                    await self._deliver_pending_hafez_dm(chat_id, uid)
                except Exception:
                    log.debug('pending hafez dm failed', exc_info=True)
            return
        if text.startswith('/admin'):
            if uid != OWNER_ID and not await self.db.is_admin(uid):
                await self.safe_send(chat_id, '⛔ دسترسی به پنل مدیریت نداری.')
                return
            await self.show_admin(chat_id, uid, owner=(uid == OWNER_ID))
            return
        if text in ('تنظیم مبلغ حمایت', 'تنظیم مبلغ حمایت مالی', '/set_donations_total'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ این دستور فقط برای مالک اصلی ربات است.', parse_mode=None)
                return
            try:
                current = await self.db.get_donations_total()
            except Exception:
                log.exception('get_donations_total failed')
                await self.safe_send(chat_id, '❌ خواندن مبلغ فعلی ممکن نشد.', parse_mode=None)
                return
            self.states[uid] = {'step': 'set_donation_total', 'chat_id': chat_id}
            await self.safe_send(
                chat_id,
                f'💚 مبلغ فعلی حمایت: {current:,} تومان\n\n'
                f'لطفاً مبلغ صحیح را به تومان وارد کن.\n'
                f'اعداد فارسی یا انگلیسی؛ صفر مجاز است.\n'
                f'برای لغو: لغو',
                parse_mode=None,
            )
            return

        if text.startswith('/owner'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ این پنل فقط برای مالک ربات است.')
                return
            await self.show_admin(chat_id, uid, owner=True)
            return
        # Owner diagnostics for a specific group id (e.g. /checkgroup 4739068741)
        if text.startswith('/checkgroup') or text.startswith('/گروه'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ فقط مالک.')
                return
            parts = text.split()
            if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
                await self.safe_send(chat_id, 'مثال: `/checkgroup 4739068741`', parse_mode=None)
                return
            await self.diagnose_group(chat_id, int(parts[1]))
            return
        if text.startswith('/unrestrictgroup') or text.startswith('/رفعگروه'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ فقط مالک.')
                return
            parts = text.split()
            if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
                await self.safe_send(chat_id, 'مثال: `/unrestrictgroup 4739068741`', parse_mode=None)
                return
            gid = int(parts[1])
            lifted = await self.db.lift_restriction('group', gid, uid, 'owner unrestrictgroup command')
            if lifted:
                await self.safe_send(chat_id, f'✅ محدودیت گروه `{gid}` برداشته شد.', parse_mode=None)
            else:
                await self.safe_send(chat_id, f'برای گروه `{gid}` محدودیت فعالی پیدا نشد.', parse_mode=None)
            # Try a live ping into the group
            ping = await self.safe_send(gid, '✅ محدودیت گروه برداشته شد؛ ربات دوباره فعال است.', parse_mode=None)
            if ping:
                await self.safe_send(chat_id, f'پیام تست به گروه `{gid}` ارسال شد.', parse_mode=None)
            else:
                await self.safe_send(chat_id, f'پیام تست به گروه `{gid}` ارسال نشد؛ دسترسی ارسال پیام ربات در گروه را چک کن.', parse_mode=None)
            return
        # If an admin/user is in a multi-step flow, consume their next text before menu shortcuts.
        state = self.states.get(uid)
        if state and int(state.get('chat_id', chat_id)) == chat_id:
            handled = await self.handle_state(chat_id, uid, user, text, state, msg)
            if handled:
                return

        if text == '🆘 پشتیبانی' or text == 'پشتیبانی':
            await self.safe_send(chat_id, '*پشتیبانی*\n' + SUPPORT_TEXT, USER_KEYBOARD)
            return
        if text == '💚 حمایت مالی' or text == 'حمایت مالی':
            await self.show_donation_intro(chat_id)
            return
        if text == '👨‍💻 تیم توسعه' or text == 'تیم توسعه':
            team = await self.db.get_team()
            body = '*تیم توسعه‌دهنده*\n\n' + ('\n'.join(f"*{m['name']}* — {m['title']}\n{m['description']}" for m in team) if team else 'هنوز عضوی ثبت نشده است.')
            await self.safe_send(chat_id, body, USER_KEYBOARD)
            return
        if text == '📊 آمار و اطلاعات' or text == 'آمار و اطلاعات':
            await self.safe_send(chat_id, self.stats_text(await self.db.stats()), USER_KEYBOARD)
            return
        if text == 'منوی بیشتر':
            await self.safe_send(chat_id, '*منوی بیشتر*\nبخش موردنظرت رو انتخاب کن.', inline([
                [('ℹ️ درباره ما','more:about'),('📚 راهنمای ربات','more:guide')],
                [('🎫 تیم من / پیگیری','more:my_cases')],
                [('↩️ برگشت','home'),('✖️ بستن','close')]
            ]), USER_KEYBOARD)
            return
        if text == 'درباره ما':
            about = await self.db.get_setting('about', 'MEGA BOT یک ربات اجتماعی برای بله است.')
            await self.safe_send(chat_id, f'*درباره MEGA BOT*\n\n{about}', USER_KEYBOARD)
            return
        if text == 'راهنما ربات':
            await self.show_guide(chat_id, 0)
            return
        if text == 'تیم من':
            await self.my_cases(chat_id, uid)
            return
        if text == 'پین' and msg.get('reply_to_message'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه می‌تواند پیام را پین کند.')
                return
            try:
                await self.api.pin_message(chat_id, int(msg['reply_to_message']['message_id']))
                await self.safe_send(chat_id, '📌 پیام پین شد.')
            except Exception as exc:
                await self.safe_send(chat_id, f'نتونستم پیام رو پین کنم؛ دسترسی ربات رو بررسی کن.\n`{self._short(exc)}`')
            return
        if text.startswith('تنظیم قوانین '):
            if not self._is_group_chat(chat) or not await self._is_chat_admin(chat_id, uid):
                await self.safe_send(chat_id, 'فقط ادمین گروه می‌تواند قوانین را تنظیم کند.')
                return
            await self.db.set_setting(f'rules:{chat_id}', text[len('تنظیم قوانین '):].strip(), uid)
            await self.safe_send(chat_id, '✅ قوانین گروه ذخیره شد.')
            return
        if text == 'قوانین' and self._is_group_chat(chat):
            await self.safe_send(chat_id, 'میخوای قوانین این گروه رو ببینی؟', inline([[('بله','rules:yes'),('خیر','rules:no')], [('✖️ بستن','close')]]))
            return
        if text.startswith('اکو '):
            echo = text[4:].strip()
            try:
                await self.api.delete_message(chat_id, int(msg['message_id']))
            except Exception:
                pass
            if echo:
                await self.safe_send(chat_id, echo)
            return
        if text.lower().lstrip('/') in ('دوز','بازی دوز','دوز بازی') and self._is_group_chat(chat):
            log.info('CMD دوز chat=%s uid=%s', chat_id, uid)
            await self.start_ttt(chat_id, uid, user.get('first_name') or 'بازیکن')
            return
        if re.fullmatch(r'شیر\s*یا\s*خط', text):
            game_id = secrets.token_hex(3)
            self.game_cache[game_id] = {'kind':'coin','creator':uid,'chat_id':chat_id,'players':{}}
            await self.safe_send(chat_id, '*بازی شیر یا خط*\nهر دو بازیکن انتخابشون رو ثبت کنن.', inline([
                [('🦁 شیر','coin:choose:'+game_id+':heads'),('🪙 خط','coin:choose:'+game_id+':tails')],
                [('✖️ لغو بازی','game:cancel:'+game_id)]
            ]))
            return
        if re.fullmatch(r'هرکی تک بیاره+', text) or re.fullmatch(r'هرکی تک+ بیاره+', text):
            await self.start_one_game(chat_id, uid, user.get('first_name') or 'بازیکن')
            return
        # ── Group VIP list / management commands
        if is_group and text in ('لیست ویژه ها', 'لیست ویژه‌ها', 'لیست ویژه'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._show_vip_list(chat_id, 0)
            return
        if is_group and text in ('ویژه',) and msg.get('reply_to_message'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه می‌تواند ویژه اضافه کند.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            target = (msg.get('reply_to_message') or {}).get('from') or {}
            tid = int(target.get('id') or 0)
            if not tid or target.get('is_bot'):
                await self.safe_send(chat_id, 'کاربر هدف معتبر نیست.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self.db.add_vip(chat_id, tid, uid)
            await self.db.log(uid, 'vip_add', 'user', tid, {'chat_id': chat_id})
            tname = target.get('first_name') or str(tid)
            await self.safe_send(chat_id, f'⭐ کاربر {tname} به ویژه‌های این گروه اضافه شد.', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if is_group and text in ('حذف ویژه',) and msg.get('reply_to_message'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            target = (msg.get('reply_to_message') or {}).get('from') or {}
            tid = int(target.get('id') or 0)
            if not tid:
                await self.safe_send(chat_id, 'کاربر هدف معتبر نیست.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            ok = await self.db.remove_vip(chat_id, tid)
            await self.db.log(uid, 'vip_remove', 'user', tid, {'chat_id': chat_id})
            await self.safe_send(
                chat_id,
                '✅ دسترسی ویژه حذف شد.' if ok else 'این کاربر در فهرست ویژه این گروه نبود.',
                reply_to=msg.get('message_id'), parse_mode=None,
            )
            return
        if is_group and (text.startswith('ویژه ') or text.startswith('افزودن ویژه ')):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            term = text.split(None, 1)[1].strip().lstrip('@')
            if term.isdigit() or (term.startswith('-') and term[1:].isdigit()):
                tid = int(term)
            else:
                found = await self.db.search_target(term, 'user')
                tid = int(found[0]['id']) if found else 0
            if not tid:
                await self.safe_send(chat_id, 'کاربر پیدا نشد. شناسه عددی بفرست یا قبلاً با ربات چت کرده باشد.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self.db.add_vip(chat_id, tid, uid)
            await self.safe_send(chat_id, f'⭐ کاربر `{tid}` ویژه این گروه شد.', reply_to=msg.get('message_id'), parse_mode=None)
            return

        # ── Locks / anti-spam panel
        # TTS: «ویس» / «ویس تست» / «ویس متن دلخواه» (any length)
        if text == 'ویس' or text.startswith('ویس '):
            await self._cmd_voice_tts(chat_id, uid, text, msg)
            return

        if is_group and text in ('لول', 'لول من', 'level'):
            await self._cmd_level_profile(chat_id, uid, user, msg, mode='level')
            return
        if is_group and text in ('پروفایل', 'پروفایل من', 'profile'):
            await self._cmd_level_profile(chat_id, uid, user, msg, mode='profile')
            return

        if is_group and text in ('قفل', 'قفل ها', 'قفل‌ها', 'ضد اسپم', 'ضداسپم', 'پنل قفل'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._show_locks_panel(chat_id, message_id=None)
            return

        # ── Welcome panel
        if is_group and text in ('خوشامد', 'خوش آمد', 'خوشامد ها', 'خوشامدها', 'پنل خوشامد'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._show_welcome_panel(chat_id, message_id=None)
            return

        # ── Goodbye / farewell panel (exact phrases; avoid social_reply conflict)
        if is_group and text in ('خداحافظی', 'تنظیم خداحافظی', 'بدرقه', 'پنل بدرقه', 'پنل خداحافظی', 'پنل خدافظ', 'خدافظی'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._show_goodbye_panel(chat_id, message_id=None)
            return
        # Exact «خداحافظ» in group by admin opens goodbye panel; members get social reply later
        if is_group and text in ('خداحافظ', 'خدافظ') and (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
            await self._show_goodbye_panel(chat_id, message_id=None)
            return

        # ── Group settings panel
        if is_group and text in ('تنظیمات گروه', 'پنل گروه', 'امنیت گروه'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._show_group_panel(chat_id, uid)
            return
        if is_group and text.startswith('خوشامدگویی '):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            body = text[len('خوشامدگویی '):].strip()
            if body in ('خاموش', 'off', 'غیرفعال'):
                await self.db.set_group_setting_fields(chat_id, welcome_enabled=False)
                await self.safe_send(chat_id, '🔴 خوشامدگویی خاموش شد.', reply_to=msg.get('message_id'), parse_mode=None)
            elif body in ('روشن', 'on', 'فعال'):
                await self.db.set_group_setting_fields(chat_id, welcome_enabled=True)
                await self.safe_send(chat_id, '🟢 خوشامدگویی روشن شد.', reply_to=msg.get('message_id'), parse_mode=None)
            elif body in ('پیش‌فرض', 'پیشفرض', 'default'):
                await self.db.set_group_setting_fields(chat_id, welcome_text=None, welcome_enabled=True)
                await self.safe_send(chat_id, 'متن خوشامد به پیش‌فرض برگشت.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                if len(body) > 1000:
                    await self.safe_send(chat_id, '❌ متن خیلی بلنده (حداکثر ۱۰۰۰ کاراکتر).', reply_to=msg.get('message_id'), parse_mode=None)
                    return
                await self.db.set_group_setting_fields(chat_id, welcome_text=body, welcome_enabled=True)
                await self.safe_send(chat_id, f'✅ متن خوشامدگویی ذخیره شد.\nپیش‌نمایش:\n{body}', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if is_group and text.startswith('خداحافظی '):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            body = text[len('خداحافظی '):].strip()
            if body in ('خاموش', 'off', 'غیرفعال'):
                await self.db.set_group_setting_fields(chat_id, goodbye_enabled=False)
                await self.safe_send(chat_id, '🔴 خداحافظی خاموش شد.', reply_to=msg.get('message_id'), parse_mode=None)
            elif body in ('روشن', 'on', 'فعال'):
                await self.db.set_group_setting_fields(chat_id, goodbye_enabled=True)
                await self.safe_send(chat_id, '🟢 خداحافظی روشن شد.', reply_to=msg.get('message_id'), parse_mode=None)
            elif body in ('پیش‌فرض', 'پیشفرض', 'default'):
                await self.db.set_group_setting_fields(chat_id, goodbye_text=None, goodbye_enabled=True)
                await self.safe_send(chat_id, 'متن خداحافظی به پیش‌فرض برگشت.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                if len(body) > 1000:
                    await self.safe_send(chat_id, '❌ متن خیلی بلنده (حداکثر ۱۰۰۰ کاراکتر).', reply_to=msg.get('message_id'), parse_mode=None)
                    return
                await self.db.set_group_setting_fields(chat_id, goodbye_text=body, goodbye_enabled=True)
                await self.safe_send(chat_id, f'✅ متن خداحافظی ذخیره شد.\nپیش‌نمایش:\n{body}', reply_to=msg.get('message_id'), parse_mode=None)
            return
            return

        # ── Group stats
        if is_group and text in ('آمار', 'امار', 'آمار گروه', 'آمارگروه', 'امار گروه'):
            if not await self._require_group_admin(chat_id, uid):
                await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', parse_mode=None)
                return
            await self._send_group_stats(chat_id, chat)
            return

        # ── Nickname: reply + «لقب X» / «تنظیم لقب X»
        if is_group and (text.startswith('لقب ') or text.startswith('تنظیم لقب ')):
            await self._cmd_set_nickname(chat_id, uid, text, msg)
            return
        # ── Hafez fortune
        if text.strip() in ('فال', 'فال حافظ', 'فال‌حافظ'):
            await self._cmd_hafez(chat_id, uid, user, msg, is_group=is_group)
            return
        # ── Joke
        if text.strip() in ('جوک', 'جک'):
            await self._cmd_joke(chat_id, msg)
            return

        # ── Font stylizer: «فونت متن» or reply+«فونت»
        if text == 'فونت' or text.startswith('فونت '):
            await self._cmd_font(chat_id, text, msg)
            return
        # ── Literary time (Tehran)
        if text in ('تایم', 'ساعت', 'زمان'):
            await self._cmd_time_story(chat_id, msg)
            return

        # ── Fun commands
        _fun_key = text.replace('‌', '').replace('ـ', '').strip()
        if _fun_key in ('چالش', 'فکت', 'حقیقت', 'جرئت', 'جرأت'):
            await self._handle_fun(chat_id, _fun_key if _fun_key != 'جرأت' else 'جرئت', msg)
            return

        # ── Prices
        if text in ('دلار',):
            data = await price_mod.get_usd_toman()
            if not data:
                await self.safe_send(chat_id, '⚠️ دریافت قیمت دلار ممکن نشد. بعداً دوباره تلاش کن.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                await self.safe_send(chat_id, price_mod.format_usd_message(data), reply_to=msg.get('message_id'))
            return
        if text in ('ارز', 'ارزها'):
            rows = await price_mod.get_currencies()
            if not rows:
                await self.safe_send(chat_id, '⚠️ دریافت قیمت ارزها ممکن نشد.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                await self.safe_send(chat_id, price_mod.format_currencies_message(rows), reply_to=msg.get('message_id'))
            return
        if text.replace('‌', '') in ('بیتکوین', 'بیت‌کوین', 'bitcoin', 'btc'):
            data = await price_mod.get_bitcoin()
            if not data:
                await self.safe_send(chat_id, '⚠️ دریافت قیمت بیت‌کوین ممکن نشد.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                await self.safe_send(chat_id, price_mod.format_btc_message(data), reply_to=msg.get('message_id'))
            return
        if text in ('طلا',):
            data = await price_mod.get_gold()
            if not data:
                await self.safe_send(chat_id, '⚠️ دریافت قیمت طلا ممکن نشد.', reply_to=msg.get('message_id'), parse_mode=None)
            else:
                await self.safe_send(chat_id, price_mod.format_gold_message(data), reply_to=msg.get('message_id'))
            return

        # ── Learned list / delete (in groups: admin/owner only)
        if text in ('لیست یادگیری', '🧠 لیست یادگیری'):
            if is_group and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            scope_type, scope_id = ('group', chat_id) if is_group else ('private', uid)
            await self._show_learned_list(chat_id, scope_type, scope_id, 0, requester_uid=uid)
            return
        if text.startswith('حذف کلمه '):
            if is_group and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            phrase = text[len('حذف کلمه '):].strip()
            if not phrase:
                await self.safe_send(chat_id, 'عبارت را بعد از «حذف کلمه» بنویس.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            scope_type, scope_id = ('group', chat_id) if is_group else ('private', uid)
            ok = await self.db.delete_learned(scope_type, scope_id, self._normalize_trigger(phrase))
            await self.safe_send(
                chat_id,
                f'🗑 عبارت «{phrase}» از حافظه این محیط حذف شد.' if ok else f'عبارت «{phrase}» در حافظه این محیط پیدا نشد.',
                reply_to=msg.get('message_id'), parse_mode=None,
            )
            return

        # Social / emoji reactions (groups + private). Bot messages already skipped above.
        if not text:
            return

        # ── Group learn: «گروهی یاد بگیر» + pairs on separate lines (group: admin only)
        if text.strip().startswith('گروهی یاد بگیر'):
            if is_group and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            await self._handle_group_learn(chat_id, uid, is_group, text, msg)
            return

        # ── Learn command: «یاد بگیر X = Y» or «یاد بگیر X Y» (group: admin only)
        learned_pair = self._parse_learn_command(text)
        if learned_pair:
            if is_group and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', reply_to=msg.get('message_id'), parse_mode=None)
                return
            trigger_raw, response = learned_pair
            trigger_norm = self._normalize_trigger(trigger_raw)
            if not trigger_norm or not response.strip():
                await self.safe_send(chat_id, '❓ عبارت یا پاسخ خالی قابل ذخیره نیست.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            if is_group:
                scope_type, scope_id = 'group', chat_id
            else:
                scope_type, scope_id = 'private', uid
            try:
                result = await self.db.upsert_learned(scope_type, scope_id, trigger_norm, trigger_raw, response, uid)
            except Exception:
                log.exception('upsert_learned failed scope=%s id=%s', scope_type, scope_id)
                await self.safe_send(chat_id, '❌ انجام نشد به دلیل: ذخیره اطلاعات در دیتابیس ناموفق بود.', reply_to=msg.get('message_id'), parse_mode=None)
                return
            points_line = ''
            if result == 'inserted' and is_group:
                try:
                    total_pts = await self.db.add_learn_points(chat_id, uid, LEARN_POINTS)
                    points_line = f'\n🏆 {LEARN_POINTS} امتیاز به امتیازات یادگیریت اضافه شد. (مجموع: {total_pts})'
                except Exception:
                    log.exception('add_learn_points failed')
            if result == 'inserted':
                msg_ok = f'✅ یادگیری جدید با موفقیت ذخیره شد! 🧠\nاز این به بعد اگه بگی «{trigger_raw}»، جواب می‌دم «{response}».{points_line}'
            else:
                msg_ok = f'🧠 کلمه به‌روزرسانی شد!\nاز این به بعد اگه بگی «{trigger_raw}»، جواب می‌دم «{response}».'
            await self.safe_send(chat_id, msg_ok, reply_to=msg.get('message_id'), parse_mode=None)
            return

        # ── Auto-reply from learned memory (after all built-in commands)
        try:
            if is_group:
                learned = await self.db.get_learned('group', chat_id, self._normalize_trigger(text))
            else:
                learned = await self.db.get_learned('private', uid, self._normalize_trigger(text))
            if learned:
                await self.safe_send(chat_id, learned, reply_to=msg.get('message_id'), parse_mode=None)
                return
        except Exception:
            log.warning('learned lookup failed chat=%s', chat_id)

        # Emoji-only: single emoji reply, no text. Rate-limited per user.
        if self._is_emoji_only(text):
            last = self._emoji_last.get(uid, 0)
            now = time.time()
            if now - last >= 3.0:
                reaction = self._pick_emoji_reaction(text)
                if reaction:
                    self._emoji_last[uid] = now
                    await self.safe_send(chat_id, reaction, reply_to=msg.get('message_id'), parse_mode=None)
            return
        # Lightweight social replies — use cached stats, never block the handler on DB failure.
        try:
            stats = await self._cached_stats()
        except Exception:
            stats = {}
        reply = self.social_reply(text, stats)
        if reply:
            await self.safe_send(chat_id, reply, reply_to=msg.get('message_id'))

    async def on_callback(self, query: dict) -> None:
        """Route every inline-button click to a visible outcome.

        Critical fixes:
        - answerCallbackQuery always (stops client spinner)
        - handle admin:back / admin:page from menu_page
        - never silent-drop unknown callback_data
        - isolate handler errors so one bad click cannot break others
        """
        callback_id = query.get('id')
        data = (query.get('data') or '').strip()
        user = query.get('from') or {}
        uid = int(user.get('id', 0) or 0)
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        chat_id = int(chat.get('id', 0) or 0)
        if uid and uid != OWNER_ID and not data.startswith('own:'):
            try:
                if await self._is_maintenance():
                    try:
                        await self.api.answer_callback(callback_id, '🛠️ ربات در حالت تعمیرات است', show_alert=True)
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        chat = message.get('chat') or {}
        chat_id = int(chat.get('id', 0) or 0)
        message_id = int(message.get('message_id', 0) or 0)

        log.info(
            'CALLBACK data=%r chat=%s type=%r uid=%s mid=%s',
            data[:64], chat_id, chat.get('type'), uid, message_id,
        )

        # Whisper callbacks must answer with alert text themselves (body must not go to group).
        if data.startswith('wh:'):
            try:
                handled = await self._handle_whisper_callback(uid, chat_id, message_id, data, callback_id)
                if handled:
                    return
            except Exception:
                log.exception('whisper callback failed')
                if callback_id:
                    try:
                        await self.api.answer_callback(callback_id, 'خطا در پردازش نجوا.', show_alert=True)
                    except Exception:
                        pass
                return

        # Always ack the click first so the client is not left loading.
        if callback_id:
            try:
                await asyncio.wait_for(self.api.answer_callback(callback_id), timeout=5.0)
            except Exception as exc:
                log.warning('answerCallbackQuery failed: %s', type(exc).__name__)

        if not data:
            await self.safe_edit(chat_id, message_id, 'دکمه نامعتبر بود.', None)
            return

        try:
            await self._route_callback(uid, chat_id, message_id, data, user, chat, query)
        except Exception:
            log.exception('Callback handler crashed data=%r uid=%s', data, uid)
            try:
                await self.safe_send(chat_id, '⚠️ خطایی در پردازش دکمه رخ داد. دوباره تلاش کن.')
            except Exception:
                pass

    async def _route_callback(
        self, uid: int, chat_id: int, message_id: int, data: str,
        user: dict, chat: dict, query: dict,
    ) -> None:
        if data == 'close':
            self.states.pop(uid, None)
            await self.safe_edit(
                chat_id, message_id,
                'این منو بسته شد. برای بازکردن دوباره از منوی اصلی استفاده کن.',
                None,
            )
            return

        if data == 'home' or data == 'admin:back:home' or data.startswith('admin:back:'):
            self.states.pop(uid, None)
            await self.safe_edit(chat_id, message_id, '*منوی اصلی MEGA BOT*', None)
            if not self._is_group_chat(chat):
                await self.safe_send(
                    chat_id,
                    'از کیبورد پایین یکی از گزینه‌ها را انتخاب کن.',
                    USER_KEYBOARD,
                )
            return

        # Group restriction for games only — never block admin panel callbacks.
        if (
            self._is_group_chat(chat)
            and uid != OWNER_ID
            and data.startswith(('ttt:', 'one:', 'coin:', 'rules:'))
        ):
            try:
                restricted = await asyncio.wait_for(
                    self.db.is_restricted('group', chat_id), timeout=2.0,
                )
            except Exception:
                restricted = None
            if restricted:
                await self.safe_send(
                    chat_id,
                    f"*⛔ این گروه توسط مدیریت محدود شده است.*\nکد پیگیری: `#{restricted['case_code']}`",
                )
                return

        if data.startswith('more:'):
            key = data.split(':', 1)[1]
            if key == 'about':
                about = await self.db.get_setting('about', 'MEGA BOT یک ربات اجتماعی برای بله است.')
                await self.safe_edit(
                    chat_id, message_id, f'*درباره MEGA BOT*\n\n{about}',
                    inline([[('↩️ برگشت', 'more:menu'), ('✖️ بستن', 'close')]]),
                )
            elif key == 'guide':
                await self.show_guide(chat_id, 0, message_id)
            elif key == 'my_cases':
                await self.my_cases(chat_id, uid)
            elif key == 'menu':
                await self.safe_edit(
                    chat_id, message_id, '*منوی بیشتر*\nبخش موردنظرت رو انتخاب کن.',
                    inline([
                        [('ℹ️ درباره ما', 'more:about'), ('📚 راهنمای ربات', 'more:guide')],
                        [('🎫 تیم من / پیگیری', 'more:my_cases')],
                        [('↩️ برگشت', 'home'), ('✖️ بستن', 'close')],
                    ]),
                )
            elif key == 'support':
                await self.safe_edit(
                    chat_id, message_id, '*پشتیبانی*\n' + SUPPORT_TEXT,
                    inline([[('↩️ برگشت', 'home'), ('✖️ بستن', 'close')]]),
                )
            elif key == 'donate':
                await self.show_donation_intro(chat_id, message_id)
            elif key == 'team':
                team = await self.db.get_team()
                body = '*تیم توسعه‌دهنده*\n\n' + (
                    '\n'.join(f"*{m['name']}* — {m['title']}\n{m['description']}" for m in team)
                    if team else 'هنوز عضوی ثبت نشده است.'
                )
                await self.safe_edit(
                    chat_id, message_id, body,
                    inline([[('↩️ برگشت', 'more:menu'), ('✖️ بستن', 'close')]]),
                )
            elif key == 'stats':
                await self.safe_edit(
                    chat_id, message_id, self.stats_text(await self.db.stats()),
                    inline([[('↩️ برگشت', 'home'), ('✖️ بستن', 'close')]]),
                )
            else:
                await self.safe_edit(
                    chat_id, message_id, 'این بخش در دسترس نیست.',
                    inline([[('↩️ برگشت', 'home'), ('✖️ بستن', 'close')]]),
                )
            return

        if data.startswith('guide:item:'):
            try:
                index = int(data.rsplit(':', 1)[1])
            except ValueError:
                await self.safe_edit(chat_id, message_id, 'آیتم نامعتبر.', None)
                return
            entries = self.GUIDE_ENTRIES
            if 0 <= index < len(entries):
                label, body = entries[index]
                await self.safe_edit(
                    chat_id, message_id, f'*{label}*\n\n{body}',
                    inline([[('↩️ برگشت', f'guide:page:{index // 2}'), ('✖️ بستن', 'close')]]),
                )
            else:
                await self.safe_edit(chat_id, message_id, 'آیتم پیدا نشد.', None)
            return

        if data.startswith('guide:page:'):
            try:
                page = int(data.rsplit(':', 1)[1])
            except ValueError:
                page = 0
            await self.show_guide(chat_id, page, message_id)
            return

        if data.startswith('rules:'):
            if data == 'rules:no':
                await self.safe_edit(chat_id, message_id, 'باشه 🌱', None)
                return
            rules = await self.db.get_setting(f'rules:{chat_id}', 'قوانینی ثبت نشده است.')
            await self.safe_edit(
                chat_id, message_id, f'*📜 قوانین گروه*\n\n{rules}',
                inline([[('✖️ بستن', 'close')]]),
            )
            return

        if data.startswith('coin:choose:'):
            parts = data.split(':')
            if len(parts) >= 4:
                gid, choice = parts[2], parts[3]
                game = self.game_cache.get(gid)
                if not game:
                    await self.safe_send(chat_id, 'این بازی دیگر فعال نیست.')
                    return
                game.setdefault('players', {})[uid] = choice
                if len(game['players']) >= 2:
                    vals = list(game['players'].items())
                    a, b = vals[0], vals[1]
                    if a[1] == b[1]:
                        result = 'مساوی شد! 🤝'
                    else:
                        # heads beats nothing special — both chose; random winner among different
                        result = f'انتخاب‌ها ثبت شد: {a[1]} vs {b[1]}'
                    await self.safe_edit(chat_id, message_id, f'*نتیجه شیر یا خط*\n{result}', None)
                    self.game_cache.pop(gid, None)
                else:
                    await self.safe_edit(chat_id, message_id, 'انتخابت ثبت شد؛ منتظر بازیکن دوم باش ⏳', None)
            return

        if data.startswith('game:cancel:'):
            gid = data.rsplit(':', 1)[-1]
            game = self.game_cache.get(gid)
            if game and int(game.get('creator', -1)) != uid and uid != OWNER_ID:
                await self.safe_send(chat_id, 'فقط سازنده می‌تواند بازی را لغو کند.')
            else:
                self.game_cache.pop(gid, None)
                await self.safe_edit(chat_id, message_id, 'بازی لغو شد.', None)
            return

        if data.startswith('font:') or data.startswith('fontp:'):
            await self._handle_font_callback(uid, chat_id, message_id, data, query)
            return
        if data.startswith('ttt:'):
            await self.handle_ttt_callback(query, data)
            return
        if data.startswith('one:'):
            await self.handle_one_callback(query, data)
            return
        if data.startswith('donate:'):
            await self.handle_donation_callback(uid, chat_id, message_id, data, user)
            return
        if data.startswith('donation_review:'):
            await self.handle_donation_review(uid, chat_id, message_id, data)
            return
        if data.startswith('own:don_total:'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ فقط مالک اصلی ربات.', parse_mode=None)
                return
            if data == 'own:don_total:start':
                try:
                    current = await self.db.get_donations_total()
                except Exception:
                    current = 0
                self.states[uid] = {'step': 'set_donation_total', 'chat_id': chat_id}
                await self.safe_edit(
                    chat_id, message_id,
                    f'💚 مبلغ فعلی حمایت: {current:,} تومان\n\nلطفاً مبلغ صحیح را به تومان وارد کن.\nبرای لغو: لغو',
                    None,
                )
                return
            state = self.states.get(uid) or {}
            if data.endswith(':no'):
                self.states.pop(uid, None)
                await self.safe_edit(chat_id, message_id, '❌ اصلاح مبلغ لغو شد. هیچ تغییری ذخیره نشد.', None)
                return
            if not data.endswith(':yes'):
                return
            if state.get('step') != 'set_donation_total_confirm':
                await self.safe_edit(chat_id, message_id, 'این درخواست منقضی شده یا قبلاً انجام شده.', None)
                return
            new_amount = int(state.get('new_amount', -1))
            if new_amount < 0:
                self.states.pop(uid, None)
                await self.safe_edit(chat_id, message_id, '❌ مبلغ نامعتبر بود.', None)
                return
            try:
                prev, new_v = await self.db.set_donations_total(new_amount, uid)
                self._invalidate_stats_cache()
                self.states.pop(uid, None)
            except Exception:
                log.exception('set_donations_total failed')
                await self.safe_edit(chat_id, message_id, '❌ ذخیره ناموفق بود. مبلغ قبلی حفظ شد.', None)
                return
            await self.safe_edit(
                chat_id, message_id,
                f'✅ مبلغ حمایت با موفقیت اصلاح شد.\n\n'
                f'مبلغ قبلی: {prev:,} تومان\n'
                f'مبلغ جدید: {new_v:,} تومان\n\n'
                f'آمار عمومی و اختصاصی به‌روزرسانی شدند.',
                None,
            )
            return

        if data.startswith('own:donation:') or data == 'own:donations':
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ فقط مالک.')
                return
            if data == 'own:donations':
                card = await self.db.get_setting('donation_card', '')
                card_name = await self.db.get_setting('donation_card_name', '')
                username = str(await self.db.get_setting('donation_owner_username', '')).lstrip('@')
                await self.safe_edit(
                    chat_id, message_id,
                    f'*تنظیمات حمایت مالی*\n\nشماره کارت: `{card}`\nصاحب کارت: {card_name}\nآیدی مالک: @{username}',
                    inline([
                        [('💳 تغییر شماره کارت', 'own:donation:card'), ('👤 نام صاحب کارت', 'own:donation:card_name')],
                        [('🔗 آیدی مالک', 'own:donation:username')],
                        [('📝 متن معرفی', 'own:donation:intro'), ('📄 توضیحات پرداخت', 'own:donation:instructions')],
                        [('💰 اصلاح مبلغ کل حمایت', 'own:don_total:start')],
                        [('↩️ برگشت', 'admin:page:0'), ('✖️ بستن', 'close')],
                    ]),
                )
                return
            # own:donation:field — set state
            field = data.split(':', 2)[-1]
            key_map = {
                'card': 'donation_card',
                'card_name': 'donation_card_name',
                'username': 'donation_owner_username',
                'intro': 'donation_intro',
                'instructions': 'donation_instructions',
            }
            setting_key = key_map.get(field, f'donation_{field}')
            self.states[uid] = {
                'step': 'edit_donation_setting',
                'setting_key': setting_key,
                'chat_id': chat_id,
                'message_id': message_id,
            }
            labels = {
                'card': 'شماره کارت جدید را بفرست',
                'card_name': 'نام صاحب کارت را بفرست',
                'username': 'آیدی مالک (بدون @) را بفرست',
                'intro': 'متن معرفی حمایت را بفرست',
                'instructions': 'توضیحات پرداخت را بفرست',
            }
            await self.safe_edit(
                chat_id, message_id, labels.get(field, 'مقدار جدید را بفرست'),
                inline([[('↩️ برگشت', 'own:donations'), ('✖️ بستن', 'close')]]),
            )
            return

        if data == 'owner:open':
            if uid == OWNER_ID:
                await self.show_admin(chat_id, uid, True, message_id)
            else:
                await self.safe_send(chat_id, '⛔ فقط مالک ربات.')
            return

        if data.startswith('admin:page:'):
            if uid == OWNER_ID or await self.db.is_admin(uid):
                try:
                    page = int(data.rsplit(':', 1)[1])
                except ValueError:
                    page = 0
                await self.show_admin(chat_id, uid, uid == OWNER_ID, message_id, page)
            else:
                await self.safe_send(chat_id, '⛔ دسترسی پنل ادمین نداری.')
            return

        if data.startswith('adm:target:'):
            await self.handle_target_select(uid, chat_id, message_id, data)
            return

        if data.startswith('adm:') or data.startswith('own:') or data.startswith('mod:') or data.startswith('team:'):
            await self.handle_admin_callback(uid, chat_id, message_id, data)
            return

        if data.startswith('perm:'):
            await self.handle_permission_callback(uid, chat_id, message_id, data)
            return

        # Group panel / security / VIP / learn / fun callbacks
        if data.startswith('purge:'):
            await self._handle_purge_callback(uid, chat_id, message_id, data)
            return
        if data == 'lock:sch' or data.startswith('sch:'):
            await self._handle_schedule_callback(uid, chat_id, message_id, data)
            return
        if (
            data.startswith('grp:') or data.startswith('learn:') or data.startswith('fun:')
            or data.startswith('lock:') or data.startswith('wel:') or data.startswith('bye:')
        ):
            await self._handle_group_callbacks(uid, chat_id, message_id, data, chat)
            return

        # Unknown callback — always visible feedback
        log.warning('Unhandled callback data=%r uid=%s', data, uid)
        await self.safe_edit(
            chat_id, message_id,
            f'این دکمه دیگر معتبر نیست.\n`{data[:40]}`',
            inline([[('↩️ منوی اصلی', 'home'), ('✖️ بستن', 'close')]]),
        )


    def _is_admin_identity(self, uid: int) -> bool:
        return uid == OWNER_ID

    # (chat_id, user_id) -> (is_admin, expires_at)
    _admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    async def _is_chat_admin(self, chat_id: int, uid: int, *, force: bool = False) -> bool:
        """Check group admin status with a short TTL cache.

        Only ``creator`` / ``administrator`` count. On API failure returns False
        (fail-closed) so non-admins never get elevated access by accident.
        """
        key = (int(chat_id), int(uid))
        now = time.time()
        if not force:
            cached = self._admin_cache.get(key)
            if cached and cached[1] > now:
                return bool(cached[0])
        try:
            result = await asyncio.wait_for(
                self.api.call('getChatMember', {'chat_id': chat_id, 'user_id': uid}),
                timeout=4.0,
            )
            if isinstance(result, dict) and isinstance(result.get('result'), dict):
                result = result['result']
            if not isinstance(result, dict):
                self._admin_cache[key] = (False, now + 15)
                return False
            status = (result.get('status') or '').strip().lower()
            is_admin = status in ('creator', 'administrator')
            # Cache positives longer; negatives shorter so promotions apply faster
            self._admin_cache[key] = (is_admin, now + (60 if is_admin else 20))
            if len(self._admin_cache) > 2000:
                expired = [k for k, v in self._admin_cache.items() if v[1] <= now]
                for k in expired[:500]:
                    self._admin_cache.pop(k, None)
            return is_admin
        except Exception as exc:
            log.warning('getChatMember failed chat=%s user=%s: %s', chat_id, uid, exc)
            # Fail closed — do not grant admin on network errors
            self._admin_cache[key] = (False, now + 10)
            return False


    async def _get_member_status(self, chat_id: int, uid: int) -> str:
        """Return 'creator' | 'administrator' | 'member' | 'left' (fail-closed -> member)."""
        try:
            result = await asyncio.wait_for(
                self.api.call('getChatMember', {'chat_id': chat_id, 'user_id': uid}),
                timeout=4.0,
            )
            if isinstance(result, dict) and isinstance(result.get('result'), dict):
                result = result['result']
            if not isinstance(result, dict):
                return 'member'
            return (result.get('status') or 'member').strip().lower()
        except Exception:
            return 'member'

    async def _is_chat_creator(self, chat_id: int, uid: int) -> bool:
        return (await self._get_member_status(chat_id, uid)) == 'creator'

    async def _require_group_admin(self, chat_id: int, uid: int) -> bool:
        """True only for bot owner or real group admin (NOT VIP).

        Used for destructive / sensitive group tools: آمار, پاک, …
        """
        if not uid:
            return False
        if uid == OWNER_ID:
            return True
        try:
            return bool(await self._is_chat_admin(chat_id, uid, force=True))
        except Exception:
            log.exception('admin check failed chat=%s uid=%s', chat_id, uid)
            return False

    async def _has_group_mod_power(self, chat_id: int, uid: int) -> bool:
        """Owner, group admin, or VIP of this group may run moderation commands."""
        if not uid:
            return False
        if uid == OWNER_ID:
            return True
        try:
            if await self._is_chat_admin(chat_id, uid):
                return True
        except Exception:
            pass
        try:
            if await self.db.is_vip(chat_id, uid):
                return True
        except Exception:
            log.debug('is_vip check failed', exc_info=True)
        return False



    async def _send_bot_join_intro(self, chat_id: int) -> None:
        """Permanent intro when the bot is added to a group (add or invite link)."""
        if not chat_id or self._is_private_chat_id(chat_id):
            return
        # Dedup within 2 minutes (my_chat_member + new_chat_members may both fire)
        key = f'botjoin:{chat_id}'
        now = time.time()
        if not hasattr(self, '_bot_join_sent'):
            self._bot_join_sent = {}
        last = self._bot_join_sent.get(chat_id, 0)
        if now - last < 120:
            return
        self._bot_join_sent[chat_id] = now
        intro = (
            'سلام! 👋\n'
            'مرسی که منو به *گروهت* دعوت کردی تا بتونم کمکت کنم گروه رو مدیریت و شاداب نگه دارم.\n\n'
            'لطفاً *اول از همه من رو ادمین کن* و این دسترسی‌ها رو بده:\n'
            '• حذف پیام‌ها\n'
            '• محدود کردن اعضا (سکوت / اخراج)\n'
            '• پین پیام (اختیاری)\n\n'
            'بعد از ادمین شدن می‌تونی از دستورات `قفل`، `خوشامد`، `آمار`، `پاک` و بقیه امکانات استفاده کنی.\n\n'
            '📚 دستورات کامل من رو داخل *پیوی*، از منوی بیشتر ← راهنمای ربات ببین.'
        )
        try:
            kb = inline([[url_button('💬 رفتن به پیوی ربات', f'https://t.me/{BOT_USERNAME}')]])
            await self.safe_send(chat_id, intro, keyboard=kb, permanent=True)
        except Exception:
            log.exception('bot-join intro failed chat=%s', chat_id)


    async def on_chat_member(self, event: dict) -> None:
        """Other members' status changes (add / join / leave) when Telegram sends chat_member."""
        try:
            chat = event.get('chat') or {}
            chat_id = int(chat.get('id', 0) or 0)
            if not chat_id or self._is_private_chat_id(chat_id):
                return
            new = event.get('new_chat_member') or {}
            old = event.get('old_chat_member') or {}
            if not isinstance(new, dict):
                return
            user = new.get('user') or new
            if not isinstance(user, dict):
                return
            status = (new.get('status') or '').lower()
            old_status = ((old.get('status') if isinstance(old, dict) else '') or '').lower()
            uid = int(user.get('id') or 0)
            if not uid:
                return
            # Bot self handled by my_chat_member
            if self.bot_id and uid == self.bot_id:
                if status in ('member', 'administrator', 'restricted') and old_status in ('', 'left', 'kicked', 'none'):
                    await self._send_bot_join_intro(chat_id)
                return
            if user.get('is_bot'):
                return
            # User joined / was added
            joined = status in ('member', 'restricted') and old_status in (
                '', 'left', 'kicked', 'none',
            )
            left = status in ('left', 'kicked') and old_status in (
                'member', 'administrator', 'restricted', 'creator',
            )
            if joined:
                fake_msg = {'new_chat_members': [user], 'chat': chat}
                await self.welcome_new_members(fake_msg, chat)
            elif left:
                fake_msg = {'left_chat_member': user, 'chat': chat}
                await self.goodbye_left_member(fake_msg, chat)
        except Exception:
            log.exception('on_chat_member failed')


    async def on_my_chat_member(self, event: dict) -> None:
        """Bot membership changes: join intro + status cache."""
        try:
            chat = event.get('chat') or {}
            chat_id = int(chat.get('id', 0) or 0)
            new = event.get('new_chat_member') or {}
            if not isinstance(new, dict):
                new = {}
            status = (new.get('status') or '').lower()
            old = event.get('old_chat_member') or {}
            old_status = ((old.get('status') if isinstance(old, dict) else '') or '').lower()
            log.info(
                'my_chat_member chat=%s old=%s new=%s title=%r',
                chat_id, old_status, status, chat.get('title'),
            )
            # Bot added to group (invite link OR admin add)
            joined = status in ('member', 'administrator', 'restricted') and old_status in (
                '', 'left', 'kicked', 'none',
            )
            if joined and chat_id:
                await self._send_bot_join_intro(chat_id)
            if not hasattr(self, '_bot_member_status'):
                self._bot_member_status = {}
            if chat_id:
                self._bot_member_status[chat_id] = status
                for k in [k for k in list(self._admin_cache.keys()) if k[0] == chat_id]:
                    self._admin_cache.pop(k, None)
                try:
                    await asyncio.wait_for(self.db.upsert_group(chat), timeout=3.0)
                except Exception:
                    log.warning('upsert_group on my_chat_member skipped for %s', chat_id)
        except Exception:
            log.exception('on_my_chat_member failed')



    async def diagnose_group(self, reply_chat_id: int, group_id: int) -> None:
        """Owner-only: report DB restriction + live Telegram API status for a group."""
        lines = [f'🔍 تشخیص گروه `{group_id}`']
        # DB restriction
        try:
            restr = await self.db.is_restricted('group', group_id)
            if restr:
                lines.append(f'⛔ محدودیت دیتابیس فعال — کد `#{restr.get("case_code")}` دلیل: {restr.get("reason")}')
            else:
                lines.append('✅ در دیتابیس محدودیت فعال ندارد')
        except Exception as exc:
            lines.append(f'خطای دیتابیس: {self._short(exc)}')

        # Live API
        try:
            chat = await self.api.call('getChat', {'chat_id': group_id})
            title = chat.get('title') or chat.get('username') or '?'
            ctype = chat.get('type') or '?'
            lines.append(f'✅ getChat: type=`{ctype}` title=`{title}`')
        except Exception as exc:
            lines.append(f'❌ getChat: {self._short(exc)}')
            chat = None

        bot_id = None
        try:
            me = await self.api.call('getMe')
            bot_id = int((me or {}).get('id') or 0) or None
        except Exception as exc:
            lines.append(f'❌ getMe: {self._short(exc)}')

        if bot_id:
            try:
                member = await self.api.call('getChatMember', {'chat_id': group_id, 'user_id': bot_id})
                st = (member or {}).get('status') or '?'
                lines.append(f'وضعیت ربات در گروه: `{st}`')
                # permissions if admin
                if st in ('administrator', 'creator'):
                    can_post = member.get('can_post_messages')
                    can_manage = member.get('can_restrict_members')
                    lines.append(f'can_restrict_members={can_manage} can_post_messages={can_post}')
            except Exception as exc:
                lines.append(f'❌ getChatMember: {self._short(exc)}')

        ping = await self.safe_send(group_id, 'تست ارسال از دستور /checkgroup ✅', parse_mode=None)
        if ping:
            lines.append('✅ sendMessage به گروه موفق بود')
        else:
            lines.append('❌ sendMessage به گروه ناموفق — ربات احتمالاً دسترسی ارسال ندارد یا در گروه نیست')

        await self.safe_send(reply_chat_id, '\n'.join(lines), parse_mode=None)


    @staticmethod
    def _short(exc: Exception) -> str:
        return str(exc).replace('\n',' ')[:180]

    # Rate-limit map for emoji reactions: user_id -> last reaction unix time
    _emoji_last: dict[int, float] = {}

    _EMOJI_RE = re.compile(
        "["
        "\U0001F300-\U0001F9FF"
        "\U0001FA00-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0001F1E0-\U0001F1FF"
        "\U0000200D"
        "\U0000FE0F"
        "\U0001F000-\U0001F02F"
        "]+",
        flags=re.UNICODE,
    )

    _EMOJI_MAP = {
        '😂': ['😂', '😆', '🤣'],
        '😆': ['😆', '😂', '🤣'],
        '🤣': ['🤣', '😂', '😆'],
        '😭': ['😭', '🥹', '🥲'],
        '🥹': ['🥹', '😭', '❤️'],
        '😊': ['😊', '☺️', '💚'],
        '☺️': ['☺️', '😊', '🥰'],
        '😍': ['😍', '🥰', '❤️'],
        '🥰': ['🥰', '❤️', '😍'],
        '🤔': ['🤔', '🧐', '👀'],
        '😎': ['😎', '🔥', '✨'],
        '🗿': ['🗿', '😐', '🗿'],
        '💀': ['💀', '😂', '☠️'],
        '❤️': ['❤️', '🥰', '💚'],
        '💔': ['💔', '🥹', '😢'],
        '💚': ['💚', '👍', '✨'],
        '🔥': ['🔥', '✨', '😎'],
        '✨': ['✨', '🌟', '💚'],
        '🌹': ['🌹', '❤️', '🥰'],
        '👍': ['👍', '💚', '👏'],
        '👎': ['👎', '😐'],
        '🙏': ['🙏', '💚', '✨'],
        '🥲': ['🥲', '🥹', '😊'],
        '😡': ['😮', '😐'],
        '😴': ['😴', '💤'],
        '👏': ['👏', '🎉', '👍'],
        '🎉': ['🎉', '✨', '👏'],
        '😳': ['😳', '🤯', '😮'],
        '🤯': ['🤯', '😳', '😮'],
        '😢': ['🥹', '😭', '💔'],
    }

    def _is_emoji_only(self, text: str) -> bool:
        stripped = (text or '').strip()
        if not stripped:
            return False
        without = self._EMOJI_RE.sub('', stripped)
        without = without.replace(' ', '').replace('\u200c', '').replace('\ufe0f', '')
        return len(without) == 0 and bool(self._EMOJI_RE.search(stripped))

    def _pick_emoji_reaction(self, text: str) -> str | None:
        found = self._EMOJI_RE.findall(text or '')
        if not found:
            return None
        # Prefer the first known mapped emoji; else echo the first emoji.
        for token in found:
            base = token[0] if token else ''
            for key, opts in self._EMOJI_MAP.items():
                if key in token or token in key:
                    return random.choice(opts)
            if base in self._EMOJI_MAP:
                return random.choice(self._EMOJI_MAP[base])
        # fallback: first emoji character
        return found[0][0] if found[0] else found[0]

    @staticmethod
    def _normalize_social(text: str) -> str:
        t = (text or '').strip().lower()
        t = t.replace('ي', 'ی').replace('ك', 'ک').replace('ۀ', 'ه').replace('ة', 'ه')
        t = t.replace('‌', ' ').replace('ـ', '')
        for ch in ('؟', '?', '!', '،', ',', '.', '…', ':', '؛'):
            t = t.replace(ch, ' ')
        t = re.sub(r'(.)\1{2,}', r'\1\1', t)  # سلامممم → سلامم
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    def social_reply(self, text: str, stats: dict) -> str | None:
        low = self._normalize_social(text)
        if not low or len(low) > 80:
            return None

        # Creator
        if low in ('سازنده', 'سازنده کیه', 'سازنده ربات', 'کی ساختت', 'developer', 'creator'):
            return 'سازنده من: @darkknight_studio هست😊'

        # Bot name mentions (exact)
        bot_names = {'مگا', 'بات', 'مگا بات', 'مگابات', 'mega', 'mega bot', 'megabot', 'ربات'}
        if low in bot_names:
            return random.choice([
                'منو میگی؟ جانم درخدمتم دستور چیه؟',
                'جانم 🌱 بگو چی کار برات انجام بدم؟',
                'بله؟ گوش به فرمانم 😄',
                'من اینجام 🍃 دستور بده!',
                'آره منم، بگو چی می‌خوای؟',
            ])

        # Negative boredom guard: «حوصلم سر نرفته» should not trigger boredom
        if re.search(r'حوصله?\s*م?\s*سر\s*نرفت', low) or 'حوصله ندارم نه' in low:
            pass  # fall through to other categories, not boredom
        else:
            boredom_keys = (
                'حوصلم سر رفته', 'خیلی حوصلم سر رفته', 'حوصله ندارم', 'بیکارم',
                'چیکار کنم', 'چی کار کنم', 'یه چیزی بگو', 'سرگرمم کن',
                'بیا بازی کنیم', 'کسی نیست باهاش حرف بزنم', 'کسل شدم', 'خسته‌ام از بیکاری',
            )
            if any(k in low for k in boredom_keys) or low in ('چیکار کنم', 'چی کار کنم', 'بیکارم'):
                return random.choice([
                    '😎 حوصلت سر رفته؟ بیا دوز بازی کنیم!',
                    '🎮 بیا یه بازی شروع کنیم؛ دوز، حدس کلمه یا چیستان؟',
                    '🧠 یه معمای خفن بگم ببین می‌تونی حلش کنی؟ دستور `چالش` رو بزن!',
                    '😂 بیا یه چالش کوتاه شروع کنیم؛ آماده‌ای؟ بنویس چالش',
                    '🌱 حوصله نداری؟ بنویس فکت تا یه چیز جالب بگم!',
                    '🎲 دوز یا حقیقت‌وجرئت؟ تو بگو کدوم!',
                ])

        # Exact / prefix phrase banks
        banks: list[tuple[tuple[str, ...], list[str]]] = [
            (
                ('سلام ربات', 'سلام بات', 'سلام مگا'),
                ['سلام🍃 چخبر؟', 'سلام سلام 😄 خوبی؟', 'درود بهت 🌿 چه خبرا؟'],
            ),
            (
                ('سلام', 'سلامم', 'سلاممم', 'درود', 'دورد', 'درود بر تو', 'درود بر شما', 'سلام علیکم', 'سلام عرض'),
                ['سلاممم 🍃', 'سلام رفیق 😄', 'سلام، خوش اومدی 🌱', 'درود! حالت چطوره؟', 'سلام سلام 🌟'],
            ),
            (
                ('صبح بخیر', 'صبح به‌خیر', 'صبح بخیرر'),
                ['صبح تو هم بخیر ☀️', 'صبح بخیر! روزت پرانرژی باشه 🌱', 'صبح بخیر رفیق 😄'],
            ),
            (
                ('عصر بخیر', 'عصر به‌خیر'),
                ['عصر تو هم بخیر 🌤', 'عصر بخیر! امیدوارم روزت خوب گذشته باشه', 'عصر بخیر 🍃'],
            ),
            (
                ('شب بخیر', 'شب‌خوش', 'شب خوش', 'شب بخیرر'),
                ['شب تو هم خوش 😊', 'خوابای قشنگ ببینی 🌙', 'شب آرومی داشته باشی 🍃', 'شب بخیر، رویای خوش ✨'],
            ),
            (
                ('خوبی', 'خوبی تو', 'چطوری', 'چطوری تو', 'حالت چطوره', 'حالت خوبه', 'چطوره'),
                ['مرسی، خوبم 😄 تو چطوری؟', 'خوبم، تو چه خبر؟ 🌿', 'دارم به پیام‌ها می‌رسم 😊 تو چطوری؟'],
            ),
            (
                ('چه خبر', 'چخبر', 'چه خبرا', 'چهخبر', 'اوضاع چطوره'),
                [
                    '__STATS_GROUPS__',  # filled below from real stats
                    'خبر خاصی نیست، تو بگو 🌱',
                    'همه‌چی آرومه فعلاً 🍃',
                    'سری به گروه‌ها می‌زنم، تو چه خبر؟',
                ],
            ),
            (
                ('مرسی', 'ممنون', 'ممنونم', 'دمت گرم', 'دستت درد نکنه', 'ممنون ازت', 'مرسییی'),
                ['خواهش می‌کنم 🌿', 'قابلی نداشت 😄', 'همیشه درخدمتم 🌱', 'قربان تو 💚'],
            ),
            (
                ('خسته نباشی', 'خسته نباشید', 'خدا قوت'),
                ['ممنون 🌱 تو هم خسته نباشی!', 'قربان انرژی‌ت 😄', 'مرسی رفیق 🍃'],
            ),
            (
                ('خوش اومدی', 'خوش اومدین', 'خوش آمدی'),
                ['مرسی که هستی 🌱', 'خوشحال می‌شم کمکت کنم 😄', 'سلام، خوش اومدی خودت! 🍃'],
            ),
            (
                ('خداحافظ', 'خدافظ', 'بدرود', 'فعلا', 'فعلاً', 'بای', 'بای بای', 'می‌بینمت', 'میبینمت'),
                ['فعلاً، مراقب خودت باش 🌿', 'بای بای 😄', 'به امید دیدار 🍃', 'خداحافظ، روز/شبت خوش ✨'],
            ),
            (
                ('روزت بخیر', 'روز بخیر', 'روز خوبی داشته باشی'),
                ['روز تو هم بخیر ☀️', 'امیدوارم روزت عالی باشه 🌱', 'روز بخیر رفیق!'],
            ),
            (
                ('خسته‌ام', 'خسته ام', 'خیلی خسته‌ام'),
                ['یه کم استراحت کن 🌿', 'انرژی‌ت برمی‌گرده، آروم باش 🍃', 'خستگی‌ت درکنه 🌱'],
            ),
            (
                ('کمک', 'کمک لازم دارم', 'کمک می‌خوام', 'راهنمایی'),
                ['بگو چی لازم داری 🌱', 'دستور `راهنما ربات` رو بزن تا بخش‌ها رو ببینی', 'اینجام، بگو چطور کمکت کنم 😄'],
            ),
            (
                ('تبریک', 'موفق باشی', 'آفرین'),
                ['مبارکه 🎉', 'دمت گرم، ادامه بده 🌱', 'آفرین! همین‌طور ادامه بده ✨'],
            ),
        ]

        # Prefer longer keys first for better match quality
        for keys, replies in banks:
            for key in sorted(keys, key=len, reverse=True):
                if low == key or (len(key) >= 3 and low.startswith(key + ' ')) or (len(key) >= 4 and key in low and len(low) <= len(key) + 8):
                    choice = random.choice(replies)
                    if choice == '__STATS_GROUPS__':
                        return self._social_stats_line(stats)
                    return choice
        return None

    WELCOME_TEMPLATES = [
        '🎉 خوش اومدی {mention} عزیز! به جمع ما خوش اومدی.',
        '🌟 یه عضو جدید به جمعمون اضافه شد! خوش اومدی {mention}!',
        '👋 سلام {mention}! امیدواریم توی گروه بهمون خوش بگذره.',
        '🎊 ورودت مبارک {mention}! به گروه خودت خوش اومدی.',
        '💚 {mention} عزیز، خوشحالیم که به جمعمون پیوستی!',
        '😎 به جمع ما خوش اومدی {mention}! امیدواریم لحظات خوبی داشته باشی.',
        '🌱 سلام {mention}! خوش اومدی به *{group}* ✨',
        '🙌 {mention} اومد وسط جمع! خوش اومدی رفیق 🌿',
        '✨ ورودت به *{group}* رو خوش‌آمد می‌گیم {mention}!',
        '🥳 یه‌نفس تازه توی گروه! خوش اومدی {mention} 🍃',
    ]

    GOODBYE_TEMPLATES = [
        '👋 خداحافظ {mention}! امیدواریم دوباره ببینیمت.',
        '💔 {mention} از جمعمون رفت. مراقب خودت باش!',
        '🌙 فعلاً خداحافظ {mention}! جای تو توی گروه خالی می‌مونه.',
        '🍀 {mention} عزیز، برات آرزوی موفقیت داریم!',
        '🚪 {mention} از گروه خارج شد. به امید دیدار دوباره!',
        '💚 خداحافظ {mention}! امیدواریم دوباره به جمعمون برگردی.',
        '🌊 فعلاً {mention}، مسیرت سبز باشه 🌱',
        '✈️ {mention} رفت؛ هر جا هستی موفق باشی!',
        '🤍 خداحافظ {mention}. در رو باز می‌ذاریم برات.',
        '🍃 {mention} عزیز، به امید دیدار دوباره در *{group}*.',
    ]

    # (chat_id, user_id, kind) -> timestamp — anti-duplicate join/leave messages
    _member_event_dedupe: dict[tuple[int, int, str], float] = {}

    @staticmethod
    def _md_escape_name(name: str) -> str:
        if not name:
            return 'دوست'
        out = []
        for ch in str(name):
            if ch in r'_*[]()`':
                out.append('\\' + ch)
            else:
                out.append(ch)
        return ''.join(out)[:64] or 'دوست'

    def _format_member_mention(self, member: dict, *, mention: bool = True) -> tuple[str, int, str]:
        """Return (display_name, uid, mention_or_plain)."""
        uid = int(member.get('id') or 0)
        raw_name = (member.get('first_name') or '').strip() or 'دوست'
        safe = self._md_escape_name(raw_name)
        if mention and uid:
            link = f'[{safe}](uid:{uid})'
        else:
            link = safe
        return raw_name, uid, link

    def _member_event_ok(self, chat_id: int, user_id: int, kind: str) -> bool:
        key = (int(chat_id), int(user_id), kind)
        now = time.time()
        last = self._member_event_dedupe.get(key, 0)
        if now - last < 8.0:
            return False
        self._member_event_dedupe[key] = now
        if len(self._member_event_dedupe) > 3000:
            cutoff = now - 60
            self._member_event_dedupe = {k: v for k, v in self._member_event_dedupe.items() if v >= cutoff}
        return True

    def _render_member_text(
        self,
        template: str,
        *,
        name: str,
        uid: int,
        group: str,
        mention: str,
        use_mention: bool,
    ) -> str:
        t = template or ''
        # Support both {name}/{mention}/{group}/{uid} and plain replacements
        plain = name if not use_mention else mention
        t = t.replace('{mention}', mention if use_mention else name)
        t = t.replace('{name}', plain)
        t = t.replace('{group}', group)
        t = t.replace('{uid}', str(uid or ''))
        return t

    async def welcome_new_members(self, msg: dict, chat: dict) -> None:
        members = msg.get('new_chat_members') or []
        chat_id = int(chat.get('id') or 0)
        if not chat_id or not members:
            return
        try:
            await self.db.record_member_join(chat_id, len(members[:10]))
        except Exception:
            pass
        gs = await self.db.get_group_settings(chat_id)
        sec = gs.get('security') or {}
        mod = gs.get('moderation') or {}

        # Process join restriction first (may kick before welcome)
        for member in members[:10]:
            try:
                await self.db.upsert_user(member)
            except Exception:
                pass
            muid = int(member.get('id') or 0)
            if not muid:
                continue
            # Our bot added via new_chat_members (common on Telegram when admin adds bot)
            if self.bot_id and muid == self.bot_id:
                await self._send_bot_join_intro(chat_id)
                continue
            if member.get('is_bot'):
                continue
            # Admins / owner are not subject to join-restriction, but still get welcome
            try:
                is_adm = (muid == OWNER_ID) or await self._is_chat_admin(chat_id, muid)
            except Exception:
                is_adm = muid == OWNER_ID
            if sec.get('joinrestrict') and not is_adm:
                blocked = await self._apply_join_restriction(chat_id, muid, member, mod)
                if blocked:
                    continue  # kicked / handled — skip welcome for this user

            if not gs.get('welcome_enabled', True):
                continue
            if not self._member_event_ok(chat_id, muid, 'join'):
                continue
            name, _, mention = self._format_member_mention(member, mention=bool(gs.get('welcome_mention', True)))
            use_mention = bool(gs.get('welcome_mention', True))
            custom = (gs.get('welcome_text') or '').strip()
            varied = bool(gs.get('welcome_varied', True))
            title = chat.get('title') or 'گروه'
            if custom:
                text = self._render_member_text(
                    custom, name=name, uid=muid, group=title, mention=mention, use_mention=use_mention,
                )
            elif varied:
                tmpl = random.choice(self.WELCOME_TEMPLATES)
                text = self._render_member_text(
                    tmpl, name=name, uid=muid, group=title, mention=mention, use_mention=use_mention,
                )
            else:
                text = self._render_member_text(
                    'سلام {mention} عزیز! خوش اومدی به *{group}* ✨',
                    name=name, uid=muid, group=title, mention=mention, use_mention=use_mention,
                )
            rules = await self.db.get_setting(f'rules:{chat_id}')
            keyboard = inline([[('📜 قوانین گروه', 'rules:show')]]) if rules else None
            try:
                await self.safe_send(chat_id, text, keyboard, permanent=True)
            except Exception:
                log.exception('welcome send failed chat=%s', chat_id)

    async def _apply_join_restriction(self, chat_id: int, user_id: int, member: dict, mod: dict) -> bool:
        """Timed join restriction. Returns True if user was blocked (kicked).

        Free slots join and open a time window. Further joins inside the window
        are kicked (or warned) until the window expires; then a new period can start.
        """
        if not self._member_event_ok(chat_id, user_id, 'joinrestrict'):
            return False  # duplicate event — don't double-act

        minutes = int(mod.get('join_restrict_minutes') or 30)
        if minutes < 1:
            minutes = 1
        if minutes > 24 * 60:
            minutes = 24 * 60
        free_slots = int(mod.get('join_restrict_free_slots') or 1)
        free_slots = max(1, min(free_slots, 50))
        action = (mod.get('join_restrict_action') or 'kick').lower()
        if action not in ('kick', 'warn'):
            action = 'kick'

        now = time.time()
        window_start = float(mod.get('join_window_start') or 0)
        window_count = int(mod.get('join_window_count') or 0)
        window_sec = minutes * 60

        # Expired or no window → start new period with this join as first free slot
        if window_start <= 0 or (now - window_start) >= window_sec:
            await self.db.update_moderation(
                chat_id,
                join_window_start=int(now),
                join_window_count=1,
            )
            return False

        # Active window
        new_count = window_count + 1
        await self.db.update_moderation(chat_id, join_window_count=new_count)

        if new_count <= free_slots:
            return False  # still within free slots

        # Restricted join
        name = (member.get('first_name') or '').strip() or str(user_id)
        if action == 'warn':
            try:
                warns = await self.db.add_warn(chat_id, user_id, reason='محدودیت عضویت', message_id=None)
                limit = int(mod.get('warn_limit') or 5)
                await self.safe_send(
                    chat_id,
                    f'⚠️ ورود در دوره محدودیت عضویت.\nکاربر {name} یک اخطار گرفت ({warns}/{limit}).',
                    parse_mode=None,
                )
                if warns >= limit and mod.get('auto_punish', True):
                    await self._execute_auto_punish(chat_id, user_id, warns, mod)
            except Exception:
                log.exception('joinrestrict warn failed chat=%s', chat_id)
            return False  # still allowed to stay (unless auto_punish kicked)

        # Default: kick
        kicked = False
        try:
            await self.api.ban_chat_member(chat_id, user_id, int(time.time()) + 45)
            await self.api.unban_chat_member(chat_id, user_id)
            kicked = True
        except Exception as exc:
            log.info('joinrestrict kick failed chat=%s uid=%s: %s', chat_id, user_id, type(exc).__name__)
            await self.safe_send(
                chat_id,
                '❌ محدودیت عضویت فعال است ولی ربات نتوانست کاربر را اخراج کند. دسترسی Ban Members را بررسی کن.',
                parse_mode=None,
            )
            return False

        if kicked:
            remain = max(0, int(window_sec - (now - window_start)))
            remain_m = max(1, remain // 60)
            await self.safe_send(
                chat_id,
                f'🚷 محدودیت عضویت فعال است.\nکاربر {name} اخراج شد.\nحدود {remain_m} دقیقه تا پایان دوره باقی مانده.',
                parse_mode=None,
            )
            try:
                await self.db.log_punish(chat_id, user_id, 'join_kick', None, 0, True, 'joinrestrict')
            except Exception:
                pass
        return kicked

    async def goodbye_left_member(self, msg: dict, chat: dict) -> None:
        left = msg.get('left_chat_member') or {}
        chat_id = int(chat.get('id') or 0)
        if not chat_id or not left:
            return
        if left.get('is_bot'):
            return
        # Skip if the leaver is the bot itself
        if self.bot_id and int(left.get('id') or 0) == self.bot_id:
            return
        gs = await self.db.get_group_settings(chat_id)
        if not gs.get('goodbye_enabled', True):
            return
        name, muid, mention = self._format_member_mention(left, mention=True)
        if not muid or not self._member_event_ok(chat_id, muid, 'leave'):
            return
        try:
            await self.db.upsert_user(left)
        except Exception:
            pass
        custom = (gs.get('goodbye_text') or '').strip()
        varied = bool(gs.get('goodbye_varied', True))
        title = chat.get('title') or 'گروه'
        # Prefer uid mention; if markdown fails, safe_send has fallbacks without parse_mode
        if custom:
            text = self._render_member_text(
                custom, name=name, uid=muid, group=title, mention=mention, use_mention=True,
            )
        elif varied:
            tmpl = random.choice(self.GOODBYE_TEMPLATES)
            text = self._render_member_text(
                tmpl, name=name, uid=muid, group=title, mention=mention, use_mention=True,
            )
        else:
            text = self._render_member_text(
                '👋 خداحافظ {mention}! به امید دیدار.',
                name=name, uid=muid, group=title, mention=mention, use_mention=True,
            )
        try:
            await self.safe_send(chat_id, text, permanent=True)
        except Exception:
            log.exception('goodbye send failed chat=%s', chat_id)
            try:
                await self.safe_send(chat_id, f'👋 خداحافظ {name}! به امید دیدار.', parse_mode=None)
            except Exception:
                pass


    async def _cmd_font(self, chat_id: int, text: str, msg: dict) -> None:
        body = text[4:].strip() if text.startswith('فونت') else ''
        if not body:
            replied = msg.get('reply_to_message') or {}
            body = (replied.get('text') or replied.get('caption') or '').strip()
        if not body:
            await self.safe_send(
                chat_id,
                '✨ بعد از «فونت» متن را بنویس.\nمثال: `فونت آرتین`\nیا روی یک پیام ریپلای کن و بنویس: فونت',
                reply_to=msg.get('message_id'),
                parse_mode='Markdown',
            )
            return
        try:
            caption, rows, _tok = font_mod.build_panel(body, page=0, extra=False)
            await self.safe_send(chat_id, caption, keyboard=inline(rows), reply_to=msg.get('message_id'))
        except Exception:
            log.exception('font panel failed')
            await self.safe_send(chat_id, '❌ ساخت فونت‌ساز ناموفق بود.', parse_mode=None)

    async def _handle_font_callback(self, uid: int, chat_id: int, message_id: int, data: str, query: dict) -> None:
        callback_id = query.get('id')
        try:
            if data.startswith('fontp:'):
                # fontp:token:page:extra
                parts = data.split(':')
                if len(parts) < 4:
                    return
                tok, page_s, extra_s = parts[1], parts[2], parts[3]
                page = int(page_s)
                extra = extra_s == '1'
                resolved = font_mod.resolve_page(tok, page, extra)
                if not resolved:
                    try:
                        await self.api.answer_callback(callback_id, '⏰ منقضی شده؛ دوباره «فونت …» بفرست', show_alert=True)
                    except Exception:
                        pass
                    return
                cap, rows = resolved
                await self.safe_edit(chat_id, message_id, cap, inline(rows))
                try:
                    await self.api.answer_callback(callback_id)
                except Exception:
                    pass
                return

            if data.startswith('font:'):
                # font:token:style_id
                parts = data.split(':')
                if len(parts) < 3:
                    return
                tok, sid_s = parts[1], parts[2]
                sid = int(sid_s)
                styled = font_mod.resolve_selection(tok, sid)
                if styled is None:
                    try:
                        await self.api.answer_callback(callback_id, '⏰ منقضی شده؛ دوباره «فونت …» بفرست', show_alert=True)
                    except Exception:
                        pass
                    return
                # Send as plain copyable text (no markdown corruption)
                await self.safe_send(chat_id, styled, parse_mode=None)
                try:
                    await self.api.answer_callback(callback_id, '✅ ارسال شد — می‌تونی کپی کنی')
                except Exception:
                    pass
                return
        except Exception:
            log.exception('font callback failed')
            try:
                await self.api.answer_callback(callback_id, 'خطا در فونت‌ساز', show_alert=True)
            except Exception:
                pass

    async def _cmd_time_story(self, chat_id: int, msg: dict) -> None:
        try:
            story = time_mod.format_time_story()
            await self.safe_send(chat_id, story, reply_to=msg.get('message_id'), parse_mode='Markdown')
        except Exception:
            log.exception('time story failed')
            await self.safe_send(chat_id, '❌ خواندن ساعت ممکن نشد.', parse_mode=None)


    async def _cmd_set_nickname(self, chat_id: int, uid: int, text: str, msg: dict) -> None:
        """Set nickname via reply. Owner: anyone; admin: only normal members."""
        replied = msg.get('reply_to_message') or {}
        target = replied.get('from') or {}
        tid = int(target.get('id') or 0)
        if not tid:
            await self.safe_send(
                chat_id,
                '❓ برای تنظیم لقب، روی پیام کاربر ریپلای کن و بنویس:\n`لقب نام‌مستعار`\nیا `تنظیم لقب نام‌مستعار`',
                reply_to=msg.get('message_id'),
            )
            return
        if text.startswith('تنظیم لقب '):
            nick = text[len('تنظیم لقب '):].strip()
        else:
            nick = text[len('لقب '):].strip()
        if not nick or len(nick) > 64:
            await self.safe_send(chat_id, '❓ لقب باید بین ۱ تا ۶۴ کاراکتر باشد.', reply_to=msg.get('message_id'), parse_mode=None)
            return

        actor_st = await self._get_member_status(chat_id, uid)
        is_owner = actor_st == 'creator' or uid == OWNER_ID
        is_admin = actor_st in ('creator', 'administrator') or uid == OWNER_ID
        if not is_admin:
            await self.safe_send(chat_id, '⛔ فقط مالک یا ادمین گروه می‌تواند لقب تنظیم کند.', reply_to=msg.get('message_id'), parse_mode=None)
            return

        target_st = await self._get_member_status(chat_id, tid)
        target_is_staff = target_st in ('creator', 'administrator')
        if target_is_staff and not is_owner:
            await self.safe_send(
                chat_id,
                '⛔ ادمین فقط می‌تواند لقب اعضای عادی را تغییر دهد، نه ادمین‌ها یا مالک.',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return
        try:
            await self.db.set_nickname(chat_id, tid, nick, set_by=uid)
        except Exception:
            log.exception('set_nickname failed')
            await self.safe_send(chat_id, '❌ ذخیره لقب ناموفق بود.', parse_mode=None)
            return
        tname = (target.get('first_name') or str(tid))[:40]
        await self.safe_send(
            chat_id,
            f'✅ لقب *{tname}* تنظیم شد:\n🏷️ `{nick}`',
            reply_to=msg.get('message_id'),
        )

    def _hafez_day_bucket(self):
        """Fortune day resets at 12:00 Asia/Tehran. Return (bucket_date, next_reset_dt)."""
        from datetime import datetime, timedelta, time as dtime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('Asia/Tehran')
        now = datetime.now(tz)
        noon = dtime(12, 0)
        if now.time() < noon:
            bucket = (now.date() - timedelta(days=1))
            next_reset = datetime.combine(now.date(), noon, tzinfo=tz)
        else:
            bucket = now.date()
            next_reset = datetime.combine(now.date() + timedelta(days=1), noon, tzinfo=tz)
        return bucket, next_reset

    def _format_hafez_message(self, fortune: dict) -> str:
        return (
            f'📜 *فال حافظ*\n'
            f'━━━━━━━━━━━━━━\n'
            f'*{fortune.get("title") or "غزل"}*\n\n'
            f'{fortune["poem"]}\n\n'
            f'━━━━━━━━━━━━━━\n'
            f'🔮 *تعبیر:*\n{fortune["meaning"]}'
        )

    async def _cmd_hafez(self, chat_id: int, uid: int, user: dict, msg: dict, *, is_group: bool) -> None:
        bucket, next_reset = self._hafez_day_bucket()
        try:
            existing = await self.db.get_hafez_assignment(uid, bucket)
        except Exception:
            log.exception('get_hafez_assignment failed')
            await self.safe_send(chat_id, '❌ خواندن سهمیه فال ممکن نشد.', parse_mode=None)
            return

        if existing:
            fortune = hafez_mod.get_by_id(int(existing['fortune_id']))
            if not fortune:
                await self.safe_send(chat_id, '❌ فال ذخیره‌شده پیدا نشد.', parse_mode=None)
                return
            body = self._format_hafez_message(fortune)
            body += f'\n\n⏳ سهمیه امروزت استفاده شده. فال بعدی بعد از ساعت ۱۲ ظهر تهران:\n`{next_reset.strftime("%Y/%m/%d %H:%M")}`'
            await self.safe_send(chat_id, body, reply_to=msg.get('message_id'))
            return

        try:
            fid = await self.db.hafez_pick_unused(hafez_mod.all_ids())
            ok = await self.db.assign_hafez_fortune(uid, bucket, fid)
            if not ok:
                # race: re-read
                existing = await self.db.get_hafez_assignment(uid, bucket)
                fid = int(existing['fortune_id']) if existing else fid
            fortune = hafez_mod.get_by_id(fid)
        except Exception:
            log.exception('assign hafez failed')
            await self.safe_send(chat_id, '❌ تخصیص فال ناموفق بود.', parse_mode=None)
            return
        if not fortune:
            await self.safe_send(chat_id, '❌ فال در دسترس نیست.', parse_mode=None)
            return

        body = self._format_hafez_message(fortune)
        await self.safe_send(chat_id, body, reply_to=msg.get('message_id'))

        if is_group:
            # Try DM; if fails, store pending + button
            try:
                await self.api.send_message(uid, body, None, None, 'Markdown', fast=True)
            except Exception:
                try:
                    await self.db.set_hafez_pending_dm(uid, int(fortune['id']), bucket)
                except Exception:
                    log.debug('pending dm store failed', exc_info=True)
                uname = (getattr(self, 'bot_username', None) or BOT_USERNAME).lstrip('@')
                kb = inline([[url_button('📩 دریافت فال در پیوی', f'https://t.me/{uname}')]])
                await self.safe_send(
                    chat_id,
                    'ℹ️ نتونستم فال رو تو پیوی بفرستم. یک‌بار پیوی ربات رو باز کن تا همان فال برات ارسال بشه.',
                    keyboard=kb,
                    parse_mode=None,
                )

    async def _deliver_pending_hafez_dm(self, chat_id: int, uid: int) -> None:
        pending = await self.db.pop_hafez_pending_dm(uid)
        if not pending:
            return
        fortune = hafez_mod.get_by_id(int(pending['fortune_id']))
        if not fortune:
            return
        body = self._format_hafez_message(fortune)
        body += '\n\n✅ این همان فال ذخیره‌شدهٔ توست (سهمیهٔ جدید مصرف نشد).'
        await self.safe_send(chat_id, body)

    async def _cmd_joke(self, chat_id: int, msg: dict) -> None:
        try:
            jid = await self.db.joke_pick(jokes_mod.all_ids())
            joke = jokes_mod.get_by_id(jid)
            if not joke:
                await self.safe_send(chat_id, '😂 جوکی در دسترس نیست.', parse_mode=None)
                return
            text = f'😂 *جوک {joke.get("cat") or ""}*\n\n{joke["text"]}'.strip()
            await self.safe_send(chat_id, text, reply_to=msg.get('message_id'))
        except Exception:
            log.exception('joke failed')
            await self.safe_send(chat_id, '❌ ارسال جوک ناموفق بود.', parse_mode=None)

    GUIDE_ENTRIES = [
        ('📌 دستورات عمومی',
         '• کیبورد پایین: پشتیبانی، حمایت مالی، تیم توسعه، آمار، منوی بیشتر\n'
         '• `/start` — شروع و نمایش منو\n'
         '• `راهنما ربات` یا دکمه 📚 راهنمای ربات\n'
         '• `درباره ما` — معرفی ربات\n'
         '• `تیم من` — پرونده محدودیت شخصی\n'
         'دسترسی: همه کاربران'),
        ('🧩 گفت‌وگو و واکنش',
         '• سلام / درود / صبح‌بخیر / شب‌خوش / خدافظ / بدرود\n'
         '• خوبی / چطوری / چه خبر / مرسی / خسته نباشی\n'
         '• حوصلم سر رفته / چیکار کنم؟ → پیشنهاد سرگرمی\n'
         '• `مگا` یا `بات` — پاسخ‌های متنوع\n'
         '• `سازنده` — @darkknight_studio\n'
         '• پیام فقط‌ایموجی → واکنش ایموجی\n'
         'دسترسی: همه کاربران'),
        ('🤫 نجوا',
         'ریپلای روی پیام کاربر + `نجوا متن پیام`\n'
         'ربات پیام را حذف می‌کند و فقط به گیرنده با دکمه «دیدن نجوا» نشان می‌دهد.\n'
         'در اعلان عمومی فقط اسم فرستنده دیده می‌شود (بدون آیدی عددی).\n'
         'دسترسی: همه اعضای گروه'),
        ('🧠 یادگیری کلمات',
         '• `یاد بگیر عبارت = پاسخ` یا `یاد بگیر عبارت پاسخ`\n'
         '• `گروهی یاد بگیر` سپس جفت‌ها در خطوط جدا (کلمه / جواب / کلمه / جواب…)\n'
         '• `لیست یادگیری` — مشاهده، حذف تکی، 🗑️ حذف تمام کلمات گروه\n'
         '• `حذف کلمه عبارت` — حذف از حافظه همین محیط\n'
         '• حافظه هر گروه و چت خصوصی جداست\n'
         'دسترسی در گروه: فقط ادمین گروه / مالک ربات'),
        ('🏅 لول و پروفایل (گروه)',
         '• `لول` — پروفایل فعالیت در همین گروه (رتبه، لول، XP، نوار پیشرفت)\n'
         '• `پروفایل` — همان کارت کامل فعالیت\n'
         '• XP با چت کردن: تقریباً هر ۲ پیام = ۱ XP (با ضداسپم)\n'
         '• لول‌آپ اعلام می‌شود؛ هر گروه مستقل است\n'
         'دسترسی: همه اعضای گروه'),
        ('🎤 ویس (متن به صدا)',
         '• `ویس متن دلخواه` — تبدیل متن به فایل صوتی\n'
         '• فارسی: صدای عصبی آفلاین (Piper / Amir)\n'
         '• انگلیسی: صدای Google Translate\n'
         '• متن طولانی پشتیبانی می‌شود\n'
         'دسترسی: همه کاربران'),
                        ('📜 فال حافظ',
         '• `فال` — یک غزل واقعی حافظ با تعبیر\n'
         '• هر کاربر روزی یک فال جدید (ریست ۱۲ ظهر تهران)\n'
         'دسترسی: همه کاربران'),
        ('😂 جوک',
         '• `جوک` — جوک فارسی تصادفی بدون تکرار تا حد امکان\n'
         'دسترسی: همه کاربران'),
        ('🏷️ لقب',
         '• ریپلای + `لقب متن` — تنظیم لقب در گروه\n'
         '• مالک: همه | ادمین: فقط عضو عادی\n'
         'دسترسی: ادمین/مالک گروه'),
('✨ فونت‌ساز',
         '• `فونت متن دلخواه` — استایل‌های فارسی و انگلیسی\n'
         '• ریپلای + `فونت` — فونت‌سازی روی همان پیام\n'
         '• صفحه‌بندی ۶تایی، قبلی/بعدی، فونت‌های بیشتر\n'
         'دسترسی: همه کاربران'),
        ('📜 تایم ادبی',
         '• `تایم` یا `ساعت` — حکایت زمان با تقویم شمسی و ساعت تهران\n'
         '• منطقه زمانی: Asia/Tehran\n'
         'دسترسی: همه کاربران'),
('🎲 سرگرمی',
         '• `چالش` — مأموریت/سؤال سرگرم‌کننده\n'
         '• `فکت` — حقیقت جالب\n'
         '• `حقیقت` — سؤال حقیقت (قابل رد)\n'
         '• `جرئت` — مأموریت امن و قابل انجام\n'
         'دسترسی: همه (ترجیحاً گروه)'),
        ('💱 قیمت‌ها (زنده)',
         '• `دلار` — نرخ بازار آزاد دلار به تومان\n'
         '• `ارز` — چند ارز منتخب\n'
         '• `بیتکوین` / `بیت‌کوین`\n'
         '• `طلا` — طلا و سکه\n'
         'منبع: api.priceto.day'),
        ('🎮 بازی‌ها (گروه)',
         '• `دوز` / `بازی دوز`\n'
         '• `شیر یا خط`\n'
         '• `هرکی تک بیاره`\n'
         'دسترسی: همه اعضای گروه'),
        ('🖼 تبدیل به گیف',
         'روی عکس/ویدئو ریپلای کن:\n'
         '• `گیف` — بدون متن\n'
         '• `گیف متن دلخواه` — نوشته روی تصویر (فونت بولد، داخل کادر)\n'
         'دسترسی: همه کاربران'),
        ('👋 خوشامد و خداحافظی',
         '• `خوشامد` / `خوشامدها` — پنل خوشامدگویی\n'
         '• `خداحافظی` / `بدرقه` / `پنل بدرقه` — پنل خداحافظی\n'
         '• روشن/خاموش، متن سفارشی، پیش‌نمایش، نام کاربر\n'
         '• هر گروه تنظیم مستقل دارد\n'
         'دسترسی: ادمین همان گروه'),
        ('📌 پین و قوانین',
         '• ریپلای + `پین` — پین پیام (ادمین)\n'
         '• `تنظیم قوانین متن...` / `قوانین`\n'
         '• `تنظیمات گروه` / `پنل گروه` — پنل کلی مدیر\n'
         'دسترسی: ادمین همان گروه'),
        ('🔐 قفل‌ها و ضداسپم',
         'دستور: `قفل` / `قفل‌ها` / `ضداسپم`\n'
         'قفل لینک، فوروارد، عکس/ویدیو/فایل/ویس/استیکر/گیف، منشن انبوه، پیام تکراری، Flood و…\n'
         'اخطار قابل تنظیم + مجازات خودکار (سکوت / اخراج / بن)\n'
         '• 🚷 محدودیت عضویت زمان‌دار (ورود چند نفر در بازه → کیک)\n'
         '• ⏰ قفل زمان‌بندی‌شده گروه (ساعت شروع/پایان و روزها)\n'
         'هر گروه مستقل است. ربات باید حذف پیام و محدودسازی عضو داشته باشد.\n'
         'دسترسی: فقط ادمین گروه / مالک'),
        ('⚠️ اخطار، بن و پاک‌سازی',
         'با ریپلای یا `@یوزرنیم`:\n'
         '• `اخطار` / `اخطار ۳` — افزودن اخطار\n'
         '• `حذف اخطار` — کم کردن یک اخطار\n'
         '• `حذف تمام اخطارها`\n'
         '• `بن` / `آنبن` / `حذف بن` / `انبن`\n'
         '• `پاک ۵` — حذف N پیام آخر (حداکثر ۱۵۰)\n'
         '• ریپلای + `پاک` — از آن پیام به پایین\n'
         'عملیات سنگین با تأیید دکمه بله/خیر\n'
         'دسترسی: ادمین گروه / ویژه با قدرت مدیریت / مالک'),
        ('📊 آمار گروه (داشبورد تصویری)',
         '• در گروه: `آمار` یا `امار` یا `آمار گروه`\n'
         '• خروجی: تصویر داشبورد ۲۴ ساعته (پیام‌ها، فعال، ورود/خروج، نمودار ساعتی، رسانه، کاربران برتر)\n'
         '• فقط مدیران گروه می‌توانند این دستور را بزنند\n'
         '• آمار سراسری ربات: دکمه 📊 آمار و اطلاعات در چت خصوصی\n'
         'توجه: شمارش از زمان ثبت رویدادها توسط ربات است'),
        ('⭐ کاربر ویژه',
         '• ریپلای + `ویژه` — افزودن (ادمین گروه)\n'
         '• ریپلای + `حذف ویژه` — حذف\n'
         '• `لیست ویژه‌ها` — فهرست همان گروه\n'
         'کاربر ویژه می‌تواند دستورات مدیریتی گروه را اجرا کند'),
        ('🔇 مدیریت با Reply',
         'ریپلای روی پیام کاربر + یکی از:\n'
         '`سکوت` / `حذف سکوت` / `بن` / `حذف بن` / `کیک`\n'
         'دسترسی: ادمین گروه، کاربر ویژه، یا مالک ربات'),
        ('🛡️ پنل ادمین و مالک',
         '• `/admin` — پنل ادمین ربات\n'
         '• `/owner` — پنل مالک + مانیتورینگ واقعی\n'
         '• `تنظیم مبلغ حمایت` — فقط مالک\n'
         '• تبلیغات و پیام همگانی فقط از پنل مجاز\n'
         'دسترسی: ادمین ربات / مالک'),
        ('💚 حمایت مالی',
         'دکمه 💚 حمایت مالی — ثبت درخواست\n'
         'تأیید/رد توسط مالک؛ مبلغ به آمار اضافه می‌شود\n'
         'دسترسی: همه برای درخواست؛ مالک برای تأیید'),
    ]

    async def show_guide(self, chat_id: int, page: int, edit_id: int | None = None) -> None:
        entries = self.GUIDE_ENTRIES
        page_size = 2
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        text = (
            f'*📚 راهنمای MEGA BOT* — صفحه {page + 1}/{total_pages}\n'
            'یک بخش را انتخاب کن تا دستور دقیق و سطح دسترسی را ببینی.'
        )
        rows = [[(label, f'guide:item:{start + i}')] for i, (label, _) in enumerate(entries[start:start + page_size])]
        controls = []
        if page > 0:
            controls.append(('⬅️ قبلی', f'guide:page:{page - 1}'))
        if start + page_size < len(entries):
            controls.append(('بعدی ➡️', f'guide:page:{page + 1}'))
        if controls:
            rows.append(controls)
        rows.append([('↩️ برگشت', 'home'), ('✖️ بستن', 'close')])
        kb = inline(rows)
        if edit_id:
            await self.safe_edit(chat_id, edit_id, text, kb)
        else:
            await self.safe_send(chat_id, text, kb)

    @staticmethod
    def _md_escape(text: str) -> str:
        """Escape characters that break Telegram Markdown in display names."""
        if not text:
            return ''
        out = []
        for ch in str(text):
            if ch in r'_*[]()`':
                out.append('\\' + ch)
            else:
                out.append(ch)
        return ''.join(out)

    def build_donation_announcement(self, donation: dict) -> str:
        """Public announcement for an approved donation (private users only).

        Named:  [Name](uid:USER_ID) مارو حمایت کرد🥹✨️
                💰 مبلغ حمایت: AMOUNT تومان
        Anonymous: یه نفر مارو حمایت کرد🥹✨️
                   💰 مبلغ حمایت: AMOUNT تومان
        """
        amount = int(donation['amount'])
        amount_line = f'💰 مبلغ حمایت: {amount:,} تومان'
        if donation.get('show_name'):
            name = (donation.get('user_name') or 'کاربر').strip() or 'کاربر'
            safe_name = self._md_escape(name)
            user_id = int(donation['user_id'])
            return f'[{safe_name}](uid:{user_id}) مارو حمایت کرد🥹✨️\n{amount_line}'
        return f'یه نفر مارو حمایت کرد🥹✨️\n{amount_line}'

    def donation_broadcast_keyboard(self) -> dict:
        """Inline button under every public donation announcement."""
        return inline([[('توهم حمایت کن 🌱', 'donate:start')]])

    async def show_donation_intro(self, chat_id: int, edit_id: int | None = None) -> None:
        intro = await self.db.get_setting(
            'donation_intro',
            'میتونی به‌صورت دلخواه از تیم ما حمایت کنی 🌱😭💞\n\nواقعاً دوست داری حمایتمون کنی؟ 😆',
        )
        text = f'*حمایت مالی MEGA BOT*\n\n{intro}'
        keyboard = inline([
            [('💚 بله، حمایت می‌کنم', 'donate:yes'), ('نه، ممنون', 'donate:cancel')],
            [('↩️ برگشت', 'home'), ('✖️ بستن', 'close')],
        ])
        if edit_id:
            await self.safe_edit(chat_id, edit_id, text, keyboard)
        else:
            await self.safe_send(chat_id, text, keyboard)

    async def handle_donation_callback(self, uid: int, chat_id: int, message_id: int, data: str, user: dict) -> None:
        # Public announcement button — open a fresh independent donation flow.
        if data == 'donate:start':
            self.states.pop(uid, None)
            await self.show_donation_intro(chat_id, message_id)
            return
        if data == 'donate:yes':
            self.states[uid] = {'step': 'donation_amount', 'chat_id': chat_id, 'user': user}
            await self.safe_edit(
                chat_id,
                message_id,
                'مقدار حمایت رو به تومان وارد کن؛ فقط عدد 😊\nمثال: `2000` یا `2.000`',
                inline([[('↩️ انصراف', 'donate:cancel'), ('✖️ بستن', 'close')]]),
            )
            return
        if data == 'donate:cancel':
            self.states.pop(uid, None)
            await self.safe_edit(chat_id, message_id, 'باشه، هر وقت خواستی می‌تونی برگردی 💚', None)
            await self.safe_send(chat_id, '*منوی اصلی MEGA BOT*', USER_KEYBOARD)
            return
        if data == 'donate:paid':
            state = self.states.get(uid) or {}
            if state.get('step') != 'donation_confirm_paid' or int(state.get('chat_id', 0)) != chat_id:
                await self.safe_send(
                    chat_id,
                    'این مرحله منقضی شده؛ اگر هنوز مایل هستی از منوی حمایت مالی دوباره شروع کن.',
                    USER_KEYBOARD,
                )
                return
            state['step'] = 'donation_visibility'
            await self.safe_edit(
                chat_id,
                message_id,
                '*حمایتت رو ثبت کنیم؟* 🌿\nاسمت رو همراه مبلغ به بقیه کاربران نمایش بدیم؟',
                inline([
                    [('بله، اسمم نمایش داده شود', 'donate:visibility:yes')],
                    [('خیر، ناشناس باشد', 'donate:visibility:no')],
                    [('↩️ پشیمان شدم / اتمام', 'donate:cancel')],
                ]),
            )
            return
        if data.startswith('donate:visibility:'):
            state = self.states.get(uid) or {}
            if state.get('step') != 'donation_visibility' or int(state.get('chat_id', 0)) != chat_id:
                await self.safe_send(
                    chat_id,
                    'این درخواست منقضی شده؛ از منوی حمایت مالی دوباره شروع کن.',
                    USER_KEYBOARD,
                )
                return
            show_name = data.endswith(':yes')
            amount = int(state['amount'])
            u = state.get('user') or user
            display_name = (u.get('first_name') or u.get('name') or 'کاربر').strip() or 'کاربر'
            donation_id = await self.db.create_donation(uid, display_name, amount, show_name)
            self.states.pop(uid, None)
            visibility_label = 'بله' if show_name else 'خیر، ناشناس'
            owner_text = (
                f'درخواست تأیید حمایت مالی\n\n'
                f'کاربر {display_name} با شناسه عددی {uid} ادعا می‌کند مبلغ {amount:,} تومان را حمایت کرده است.\n'
                f'نمایش نام در پیام همگانی: {visibility_label}\n'
                f'شناسه درخواست: #{donation_id}\n\n'
                f'آیا درست می‌گوید؟'
            )
            owner_notice = await self.safe_send(
                OWNER_ID,
                owner_text,
                inline([
                    [('✅ آره، تأیید و اعلام', 'donation_review:approve:' + str(donation_id)),
                     ('❌ خیر، رد درخواست', 'donation_review:reject:' + str(donation_id))],
                ]),
                parse_mode=None,
            )
            if not owner_notice:
                await self.db.review_donation(donation_id, False, OWNER_ID)
                await self.safe_edit(
                    chat_id,
                    message_id,
                    'ارسال درخواست به پیوی مالک انجام نشد. لطفاً بعداً دوباره تلاش کن یا از پشتیبانی کمک بگیر.',
                    None,
                )
                return
            await self.safe_edit(
                chat_id,
                message_id,
                'درخواستت برای بررسی به مالک ارسال شد 🌱\nبعد از بررسی، اگر تأیید شود طبق انتخابت اعلام می‌شود.',
                None,
            )
            return

    async def handle_donation_review(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if int(uid) != int(OWNER_ID):
            await self.safe_send(chat_id, '⛔ فقط مالک ربات می‌تواند درخواست حمایت مالی را تأیید کند.')
            return
        try:
            # data = donation_review:approve:123  or  donation_review:reject:123
            parts = data.split(':')
            if len(parts) < 3:
                await self.safe_edit(chat_id, message_id, '❌ داده دکمه نامعتبر است.', None)
                return
            action = parts[1]  # approve | reject
            donation_id = int(parts[2])
        except (ValueError, TypeError, IndexError):
            await self.safe_edit(chat_id, message_id, '❌ شناسه درخواست نامعتبر است.', None)
            return
        approve = action == 'approve'

        # Immediate UI feedback so the first click never looks dead.
        try:
            await self.safe_edit(
                chat_id, message_id,
                '⏳ در حال پردازش درخواست حمایت… لطفاً صبر کن.',
                None,
            )
        except Exception:
            log.exception('donation_review: could not edit to processing state')

        try:
            donation = await self.db.review_donation(donation_id, approve, uid)
        except Exception:
            log.exception('review_donation failed id=%s', donation_id)
            await self.safe_edit(
                chat_id, message_id,
                '❌ خطای دیتابیس هنگام بررسی درخواست. دوباره تلاش کن؛ اگر تکرار شد به لاگ سرور نگاه کن.',
                None,
            )
            return

        if not donation:
            await self.safe_edit(chat_id, message_id, 'این درخواست قبلاً بررسی شده یا پیدا نشد.', None)
            return

        try:
            await self.db.log(
                uid,
                'donation_' + ('approved' if approve else 'rejected'),
                'donation',
                donation_id,
                {'amount': int(donation['amount']), 'show_name': bool(donation['show_name'])},
            )
        except Exception:
            log.exception('donation review log failed')

        if not approve:
            await self.safe_edit(
                chat_id,
                message_id,
                f'❌ درخواست حمایت `#{donation_id}` رد شد. هیچ پیام همگانی ارسال نشد و آمار تغییر نکرد.',
                None,
            )
            return

        # Approved — show intermediate status before potentially long broadcast
        amount = int(donation['amount'])
        await self.safe_edit(
            chat_id,
            message_id,
            f'✅ حمایت `#{donation_id}` تأیید شد ({amount:,} تومان).\n⏳ در حال ارسال اعلام به کاربران…',
            None,
        )

        announcement = self.build_donation_announcement(donation)
        keyboard = self.donation_broadcast_keyboard()
        sent = failed = 0
        try:
            targets = await self.db.private_user_ids()
        except Exception:
            log.exception('private_user_ids failed')
            targets = []

        for target_id in targets:
            try:
                await self.api.send_message(
                    target_id,
                    announcement,
                    reply_markup=keyboard,
                    parse_mode='Markdown',
                )
                sent += 1
            except Exception as exc:
                failed += 1
                log.debug('Donation announcement DM failed for %s: %s', target_id, type(exc).__name__)
            # light yield only; no long cooldown
            if (sent + failed) % 30 == 0:
                await asyncio.sleep(0.05)

        try:
            await self.db.set_donation_broadcast_counts(donation_id, sent, failed)
            self._invalidate_stats_cache()
        except Exception:
            log.exception('set_donation_broadcast_counts failed')

        await self.safe_edit(
            chat_id,
            message_id,
            f'✅ حمایت `#{donation_id}` تأیید شد.\n'
            f'مبلغ {amount:,} تومان به آمار اضافه شد.\n'
            f'ارسال خصوصی: {sent} موفق، {failed} ناموفق.',
            None,
        )
        # Always notify the donor on successful approval.
        try:
            await self.safe_send(
                int(donation['user_id']),
                '💚 حمایت مالی‌ات تأیید شد و مبلغ به آمار ربات اضافه شد! از همراهی‌ت ممنونیم 🌱😊',
                USER_KEYBOARD,
            )
        except Exception as exc:
            log.debug('Donor success notice failed for %s: %s', donation.get('user_id'), type(exc).__name__)

    def stats_text(self, s: dict) -> str:
        return (f"*📊 آمار MEGA BOT*\n\n*👤 کل کاربران:* {s['users']}\n*🆕 کاربران جدید امروز:* {s['new_today']}\n"
                f"*👥 گروه‌های ثبت‌شده:* {s['groups']}\n*🙋 کاربران استفاده‌کننده در گروه‌ها:* {s['group_users']}\n"
                f"*⛔ محدودیت فعال کاربران:* {s['banned_users']}\n*🚫 گروه‌های محدود:* {s['banned_groups']}\n"
                f"*💚 مجموع حمایت مالی:* {s['donations']} تومان\n\n*ℹ️ نسخه {BOT_VERSION}*")





    def _is_xp_spam(self, chat_id: int, user_id: int) -> bool:
        """True if user flooded the group beyond SPAM_MSG_LIMIT in SPAM_WINDOW_SEC."""
        now = time.time()
        key = (int(chat_id), int(user_id))
        window = float(level_mod.SPAM_WINDOW_SEC)
        limit = int(level_mod.SPAM_MSG_LIMIT)
        bucket = self._xp_msg_times.get(key) or []
        bucket = [t for t in bucket if now - t < window]
        bucket.append(now)
        self._xp_msg_times[key] = bucket
        # Bound memory
        if len(self._xp_msg_times) > 5000:
            stale = [k for k, v in self._xp_msg_times.items() if not v or now - v[-1] > window * 2]
            for k in stale[:1000]:
                self._xp_msg_times.pop(k, None)
        return len(bucket) >= limit

    def _mention_user(self, user_id: int, name: str) -> str:
        safe = (name or 'کاربر').replace('[', '').replace(']', '').replace('(', '').replace(')', '')
        safe = safe.replace('*', '').replace('`', '').replace('_', ' ')[:40] or 'کاربر'
        return f'[{safe}](uid:{int(user_id)})'

    async def _announce_level_up(
        self, chat_id: int, user_id: int, name: str, new_level: int, reply_to: int | None = None,
    ) -> None:
        mention = self._mention_user(user_id, name)
        body = f'🎉 ایول {mention}!\nفعالیتت عالی بود، رسیدی به لول *{new_level}* 🚀'
        try:
            await self.safe_send(chat_id, body, reply_to=reply_to)
        except Exception:
            log.debug('level-up announce failed', exc_info=True)


    async def _cmd_voice_tts(self, chat_id: int, uid: int, text: str, msg: dict) -> None:
        """Free offline TTS: «ویس متن…» or reply+«ویس». Supports long text."""
        body = text[3:].strip() if text.startswith('ویس') else ''
        # Exact «ویس تست» keeps legacy short test
        if text.strip() == 'ویس تست':
            body = 'تست'
        if not body:
            replied = msg.get('reply_to_message') or {}
            body = (replied.get('text') or replied.get('caption') or '').strip()
        if not body:
            await self.safe_send(
                chat_id,
                '🎙 بعد از «ویس» متن را بنویس.\n'
                'مثال: ویس سلام خوبی؟\n'
                'یا روی یک پیام ریپلای کن و فقط بنویس: ویس',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return

        body = tts_mod.normalize_tts_text(body)
        if not body:
            await self.safe_send(chat_id, '❌ متن خالی است.', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if len(body) > tts_mod.MAX_CHARS:
            body = body[: tts_mod.MAX_CHARS]
            note = f' (متن خیلی بلند بود؛ تا {tts_mod.MAX_CHARS} کاراکتر پردازش شد)'
        else:
            note = ''

        est = tts_mod.estimate_duration_sec(body)
        await self.safe_send(
            chat_id,
            f'🎙 در حال ساخت ویس… حدود {est} ثانیه{note}',
            reply_to=msg.get('message_id'),
            parse_mode=None,
        )
        try:
            data, filename = await tts_mod.synthesize_persian(body)
        except Exception as exc:
            log.exception('TTS synthesize failed')
            await self.safe_send(
                chat_id,
                f'❌ ساخت ویس ناموفق بود: {self._short(exc)}',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return

        caption = '🎤 ویس MEGA BOT'
        try:
            # Long audio: prefer sendAudio (more reliable for multi-minute)
            if len(data) > 900_000 or est >= 60:
                await self.api.send_audio_bytes(chat_id, data, filename=filename, caption=caption)
            else:
                await self.api.send_voice_bytes(
                    chat_id, data, filename=filename, caption=caption, duration=est,
                )
        except Exception as exc:
            log.exception('send voice failed')
            try:
                await self.api.send_audio_bytes(chat_id, data, filename=filename or 'voice.ogg', caption=caption)
            except Exception as exc2:
                log.exception('send audio fallback failed')
                await self.safe_send(
                    chat_id,
                    f'❌ ارسال ویس ناموفق بود: {self._short(exc2)}',
                    reply_to=msg.get('message_id'),
                    parse_mode=None,
                )


    async def _cmd_level_profile(
        self, chat_id: int, uid: int, user: dict, msg: dict, *, mode: str = 'level',
    ) -> None:
        """Show activity level card or full profile for this group."""
        name = (user.get('first_name') or user.get('name') or 'کاربر')[:60]
        username = (user.get('username') or '').lstrip('@')
        mention = self._mention_user(uid, name)

        try:
            act = await self.db.get_activity(chat_id, uid)
            rank = await self.db.get_activity_rank(chat_id, uid)
            neighbors = await self.db.activity_neighbors(chat_id, uid, window=2)
            total_msgs = int(act.get('total_msgs') or 0)
            if total_msgs <= 0:
                total_msgs = await self.db.total_messages_in_group(chat_id, uid)
        except Exception:
            log.exception('level profile db failed')
            await self.safe_send(
                chat_id, '❌ خواندن آمار فعالیت ممکن نشد. کمی بعد دوباره تلاش کن.',
                reply_to=msg.get('message_id'), parse_mode=None,
            )
            return

        prog = level_mod.progress_in_level(int(act.get('xp') or 0))
        level = prog['level']
        bar = level_mod.progress_bar(prog['percent'])
        pct = prog['percent']
        rank_s = f'#{rank}' if rank else '—'

        if mode == 'profile':
            uname_line = f'@{username}' if username else None
            nick = None
            try:
                nick = await self.db.get_nickname(chat_id, uid)
            except Exception:
                log.debug('get_nickname failed', exc_info=True)
            role_line = ''
            try:
                st = await self._get_member_status(chat_id, uid)
                if st == 'creator':
                    role_line = '\n\n👑 مالک این گروه'
                elif st == 'administrator':
                    role_line = '\n\n🛡️ ادمین این گروه'
            except Exception:
                pass
            lines = [
                f'👤 اطلاعات {mention}:',
                '',
                '➖➖➖➖➖➖➖➖➖➖➖➖',
                '',
                f'🟢 نام : {name}',
            ]
            if nick:
                lines += ['', f'🏷️ لقب : {nick}']
            if uname_line:
                lines += ['', f'🟢 ایدی : {uname_line}']
            lines += [
                '',
                f'🟢 ایدی عددی : `{uid}`',
                '',
                f'🟢 مجموع پیام‌ها در این گروه: *{total_msgs:,}*',
                '',
                f'⭐ لول: *{level}* | امتیاز: *{prog["total_xp"]:,}*',
                '',
                '➖➖➖➖➖➖➖➖➖➖➖➖',
            ]
            body = '\n'.join(lines) + role_line
            await self.safe_send(chat_id, body, reply_to=msg.get('message_id'))
            return

        # mode == level
        if prog['maxed']:
            progress_line = f'🔥 پیشرفت:\n{bar} ۱۰۰٪\n\n🏆 به سقف لول {level_mod.MAX_LEVEL} رسیدی!'
        else:
            progress_line = (
                f'🔥 پیشرفت:\n{bar} {pct:.0f}%\n\n'
                f'⏳ *{prog["remaining"]:,}* امتیاز تا لول بعدی'
            )

        near_lines = []
        if neighbors:
            for n in neighbors:
                nname = (n.get('display_name') or f'کاربر {n["user_id"]}')[:28]
                nname = nname.replace('*', '').replace('`', '').replace('_', ' ')
                mark = '👉 ' if int(n['user_id']) == uid else ''
                near_lines.append(
                    f'{mark}#{n["rank"]} {nname} | Lv {n["level"]}'
                )
        near_block = '\n'.join(near_lines) if near_lines else 'هنوز رتبه‌بندی برای این گروه شکل نگرفته.'

        body = (
            f'🏅 پروفایل فعالیت\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'👤 {mention}\n'
            f'🏆 رتبه: {rank_s}\n'
            f'⭐ لول: *{level}*\n'
            f'📊 امتیاز: *{prog["xp_in_level"]:,}*/*{prog["xp_need"]:,}*\n'
            f'{progress_line}\n\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'📊 کاربران نزدیک به شما\n\n'
            f'{near_block}'
            f'\n\n💡 با چت کردن XP بدست بیارید — هر ۲ پیام = ۱ XP'
        )
        await self.safe_send(chat_id, body, reply_to=msg.get('message_id'))

    async def _show_owner_monitoring(self, chat_id: int, message_id: int, data: str = 'own:mon') -> None:
        """Owner live monitoring — all numbers from DB/events, never invented."""
        from zoneinfo import ZoneInfo
        started = getattr(self, '_started_at', None)
        uptime_s = int(time.time() - started) if started else None
        if uptime_s is None:
            uptime_txt = 'نامشخص (قبل از ثبت شروع)'
        else:
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)
            uptime_txt = f'{h}س {m}د {s}ث'

        try:
            act = await self.db.activity_stats()
        except Exception:
            log.exception('activity_stats failed')
            act = None

        keys = [
            'msg_in', 'msg_out_ok', 'msg_out_fail', 'msg_in_private', 'msg_in_group',
            'msg_in_answered', 'msg_in_unanswered', 'err_total', 'bot_restarts',
            'last_in_ts', 'last_out_ts', 'last_err_ts', 'last_start_ts',
        ]
        try:
            totals = await self.db.metrics_get_many(keys)
        except Exception:
            log.exception('metrics_get_many failed')
            totals = {}

        try:
            day = await self.db.tehran_today()
            daily = await self.db.metrics_day(day)
        except Exception:
            day = None
            daily = {}

        def fmt_ts(key: str) -> str:
            v = totals.get(key)
            if not v:
                return 'هنوز ثبت نشده'
            try:
                return datetime.fromtimestamp(int(v), tz=timezone.utc).astimezone(
                    ZoneInfo('Asia/Tehran')
                ).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                return 'نامعتبر'

        def n(key: str, src: dict | None = None) -> str:
            src = src if src is not None else totals
            if key not in src:
                return '—'
            return f'{int(src[key]):,}'

        if act is None:
            act_block = '⚠️ آمار فعالیت از دیتابیس خوانده نشد.'
        else:
            act_block = (
                f"گروه ثبت‌شده: *{act['groups_total']:,}*\n"
                f"گروه فعال ۲۴س: *{act['groups_active_24h']:,}* | ۷روز: *{act['groups_active_7d']:,}*\n"
                f"کاربر ثبت‌شده: *{act['users_total']:,}*\n"
                f"کاربر فعال ۲۴س: *{act['users_active_24h']:,}* | ۷روز: *{act['users_active_7d']:,}*\n"
                f"چت خصوصی فعال ۲۴س: *{act['private_chats_active_24h']:,}*"
            )

        day_label = str(day) if day else 'نامشخص'
        body = (
            f'📡 *مانیتورینگ زنده MEGA BOT*\n'
            f'منطقه زمانی روز: Asia/Tehran | روز جاری: `{day_label}`\n\n'
            f'*پیام‌ها (از شروع جمع‌آوری)*\n'
            f"ورودی کل: *{n('msg_in')}* (خصوصی {n('msg_in_private')} / گروه {n('msg_in_group')})\n"
            f"خروجی موفق: *{n('msg_out_ok')}* | ناموفق: *{n('msg_out_fail')}*\n\n"
            f'*امروز (تهران)*\n'
            f"ورودی: *{n('msg_in', daily)}* | خروجی موفق: *{n('msg_out_ok', daily)}* | خطا: *{n('err_total', daily)}*\n\n"
            f'*کاربران و گروه‌ها*\n{act_block}\n\n'
            f'*وضعیت سرویس*\n'
            f'Uptime فرایند: *{uptime_txt}*\n'
            f"آخرین ورودی: {fmt_ts('last_in_ts')}\n"
            f"آخرین خروجی موفق: {fmt_ts('last_out_ts')}\n"
            f"آخرین خطا: {fmt_ts('last_err_ts')}\n"
            f"شماره ری‌استارت‌ها: *{n('bot_restarts')}*\n\n"
            f'_اعداد «—» یعنی هنوز داده جمع نشده یا خوانده نشد؛ صفر واقعی فقط وقتی شمارنده وجود دارد._'
        )
        kb = inline([
            [('🔄 تازه‌سازی', 'own:mon'), ('📊 آمار کلاسیک', 'adm:stats')],
            [('↩️ برگشت', 'admin:page:0'), ('✖️ بستن', 'close')],
        ])
        await self.safe_edit(chat_id, message_id, body, kb)

    async def show_admin(self, chat_id: int, uid: int, owner: bool, edit_id: int | None = None, page: int = 0) -> None:
        if not owner:
            admin = await self.db.get_admin(uid)
            perms = admin['permissions'] if admin else {}
            items = []
            if perms.get('stats'): items.append(('📊 آمار و گزارش‌ها','adm:stats'))
            if perms.get('search'): items += [('👤 جست‌وجوی کاربر','adm:search:user'),('👥 جست‌وجوی گروه','adm:search:group')]
            if perms.get('case_lookup'): items.append(('🎫 بررسی کد پیگیری','adm:case'))
            if perms.get('ban_user') or perms.get('unban_user'): items.append(('👤 محدودیت کاربران','adm:search:user'))
            if perms.get('ban_group') or perms.get('unban_group'): items.append(('👥 محدودیت گروه‌ها','adm:search:group'))
            if perms.get('edit_about'): items.append(('📝 ویرایش درباره ما','own:about'))
            if perms.get('edit_team'): items.append(('👨‍💻 تیم توسعه','own:team'))
            if perms.get('settings'):
                items += [('📢 افزودن دکمه تبلیغ','adm:ad'),('📨 پیام همگانی','adm:broadcast')]
            if not items: items = [('📊 آمار و گزارش‌ها','adm:stats')]
        else:
            items = [
                ('📊 آمار و گزارش‌ها','adm:stats'),('👤 جست‌وجوی کاربر','adm:search:user'),('👥 جست‌وجوی گروه','adm:search:group'),
                ('🎫 بررسی کد پیگیری','adm:case'),('🧾 گزارش فعالیت‌ها','adm:logs'),('🛡️ مدیریت ادمین‌ها','own:admins'),
                ('📝 ویرایش درباره ما','own:about'),('👨‍💻 تیم توسعه‌دهنده','own:team'),('💚 تنظیمات حمایت مالی','own:donations'),
                ('📢 افزودن دکمه تبلیغ','adm:ad'),('📨 پیام همگانی','adm:broadcast'),
                ('📡 مانیتورینگ زنده','own:mon'),
                ('⚙️ تنظیمات مالک','own:settings')]
        # Exactly three content items per page, plus optional previous/next and back/close.
        start = page*2
        rows = [[(label,data)] for label,data in items[start:start+2]]
        controls=[]
        if page>0: controls.append(('⬅️ قبلی',f'admin:page:{page-1}'))
        if start+2<len(items): controls.append(('بعدی ➡️',f'admin:page:{page+1}'))
        if controls: rows.append(controls)
        rows.append([('↩️ برگشت','home'),('✖️ بستن','close')])
        text = '*پنل مالک MEGA BOT*\nیک بخش رو انتخاب کن.' if owner else '*پنل ادمین MEGA BOT*\nدسترسی‌ها بر اساس مجوزهای تو نمایش داده می‌شوند.'
        if edit_id: await self.safe_edit(chat_id,edit_id,text,inline(rows))
        else: await self.safe_send(chat_id,text,inline(rows))

    async def has_perm(self, uid: int, perm: str) -> bool:
        if uid == OWNER_ID: return True
        admin = await self.db.get_admin(uid)
        return bool(admin and (admin.get('permissions') or {}).get(perm, False))

    async def handle_admin_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        owner = uid == OWNER_ID
        if not owner and not await self.db.is_admin(uid):
            await self.safe_send(chat_id,'⛔ دسترسی مدیریت نداری.')
            return
        # --- Ad button management ---
        if data == 'adm:ad' or data.startswith('adm:ad:'):
            await self._handle_ad_callback(uid, chat_id, message_id, data)
            return
        # --- Broadcast ---
        if data == 'adm:broadcast' or data.startswith('adm:bc:'):
            await self._handle_broadcast_callback(uid, chat_id, message_id, data)
            return
        if data == 'adm:stats':
            if not await self.has_perm(uid,'stats'): return await self.safe_send(chat_id,'⛔ مجوز مشاهده آمار نداری.')
            await self.safe_edit(chat_id,message_id,self.stats_text(await self.db.stats()),inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data in ('adm:search:user','adm:search:group'):
            if not await self.has_perm(uid,'search'): return await self.safe_send(chat_id,'⛔ مجوز جست‌وجو نداری.')
            target_type = data.rsplit(':',1)[1]
            self.states[uid]={'step':'search','target_type':target_type,'chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,f"*جست‌وجوی {'کاربر' if target_type=='user' else 'گروه'}*\nشناسه عددی یا `@username` رو در همین گفتگو بفرست.",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'adm:case':
            if not await self.has_perm(uid,'case_lookup'): return await self.safe_send(chat_id,'⛔ مجوز بررسی پرونده نداری.')
            self.states[uid]={'step':'case_lookup','chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,'کد پیگیری رو بفرست؛ مثال: `#5453`',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'adm:logs':
            if not owner and not await self.has_perm(uid,'settings'): return await self.safe_send(chat_id,'⛔ مشاهده گزارش فعالیت‌ها فقط برای مالک یا ادمین مجاز است.')
            async with self.db._pool().acquire() as c:
                rows = await c.fetch('SELECT actor_id,action,target_type,target_id,created_at FROM audit_logs ORDER BY id DESC LIMIT 10')
            body = '*آخرین فعالیت‌ها*\n\n' + ('\n'.join(f"• `{r['actor_id']}` — {r['action']} — {r['target_type'] or '-'} `{r['target_id'] or '-'}`" for r in rows) if rows else 'هنوز فعالیتی ثبت نشده.')
            await self.safe_edit(chat_id,message_id,body,inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'own:about':
            if not await self.has_perm(uid,'edit_about'): return await self.safe_send(chat_id,'⛔ مجوز ویرایش درباره ما نداری.')
            self.states[uid]={'step':'edit_about','chat_id':chat_id,'message_id':message_id}
            current = await self.db.get_setting('about','')
            await self.safe_edit(chat_id,message_id,f"*ویرایش درباره ما*\nمتن فعلی:\n{current}\n\nحالا متن جدید رو در همین گفتگو بفرست.",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'own:team':
            if not await self.has_perm(uid,'edit_team'): return await self.safe_send(chat_id,'⛔ مجوز مدیریت تیم نداری.')
            team = await self.db.get_team()
            rows = [[('➕ افزودن عضو','team:add')]]
            rows += [[(f"👤 {m['name'][:25]}",f"team:member:{m['id']}")] for m in team[:3]]
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            text='*مدیریت تیم توسعه‌دهنده*\n\n'+('\n'.join(f"`{m['id']}`. *{m['name']}* — {m['title']}" for m in team) if team else 'اعضای تیم هنوز ثبت نشده‌اند.')
            await self.safe_edit(chat_id,message_id,text,inline(rows)); return
        if data.startswith('team:member:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            team=await self.db.get_team(); member=next((m for m in team if int(m['id'])==member_id),None)
            if not member: return await self.safe_send(chat_id,'عضو تیم پیدا نشد.')
            await self.safe_edit(chat_id,message_id,f"*{member['name']}*\nعنوان: {member['title']}\nتوضیح: {member['description'] or 'ندارد'}\nآیدی: @{member['username'] or 'ندارد'}",inline([[('✏️ ویرایش عضو',f'team:edit:{member_id}')],[('🗑️ حذف عضو',f'team:del:{member_id}')],[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data.startswith('team:edit:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            team=await self.db.get_team(); member=next((m for m in team if int(m['id'])==member_id),None)
            if not member: return await self.safe_send(chat_id,'عضو تیم پیدا نشد.')
            self.states[uid]={'step':'team_edit_name','chat_id':chat_id,'message_id':message_id,'member_id':member_id,'team_data':{}}
            await self.safe_send(chat_id,f"نام جدید عضو را بفرست. نام فعلی: {member['name']}")
            return
        if data == 'team:add':
            if not await self.has_perm(uid,'edit_team'): return
            self.states[uid]={'step':'team_add_name','chat_id':chat_id,'message_id':message_id,'team_data':{}}
            await self.safe_edit(chat_id,message_id,'نام عضو جدید تیم را بفرست.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data.startswith('team:del:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            ok=await self.db.delete_team_member(member_id,uid)
            await self.safe_edit(chat_id,message_id,'عضو حذف شد.' if ok else 'این عضو پیدا نشد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data == 'own:admins':
            if not owner: return await self.safe_send(chat_id,'⛔ فقط مالک می‌تواند ادمین‌ها را مدیریت کند.')
            admins=await self.db.list_admins(10,0)
            rows=[[('➕ افزودن ادمین','own:admin_add')]]
            for a in admins[:2]: rows.append([(f"⚙️ {a['display_name'] or a['user_id']}",f"own:admin:{a['user_id']}")])
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            text='*مدیریت ادمین‌ها*\n'+('\n'.join(f"• `{a['user_id']}` — {'فعال' if a['active'] else 'غیرفعال'}" for a in admins) if admins else 'هنوز ادمینی ثبت نشده.')
            await self.safe_edit(chat_id,message_id,text,inline(rows)); return
        if data == 'own:admin_add':
            if not owner: return
            self.states[uid]={'step':'admin_add_id','chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,'شناسه عددی ادمین جدید رو بفرست.',inline([[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return
        if data.startswith('own:admin:'):
            if not owner: return
            target=int(data.rsplit(':',1)[1])
            async with self.db._pool().acquire() as c:
                row=await c.fetchrow('SELECT * FROM admins WHERE user_id=$1',target)
            if not row: return await self.safe_send(chat_id,'ادمین پیدا نشد.')
            perms=row['permissions'] or {}
            rows=[[('🗑️ حذف ادمین',f'own:admin_del:{target}')]]
            for key,label in list(PERMISSION_LABELS.items())[:2]: rows.append([( ('✅ ' if perms.get(key) else '▫️ ')+label, f'perm:toggle:{target}:{key}')])
            rows.append([('بعدی ➡️',f'own:admin_perms:{target}:1')])
            rows.append([('↩️ برگشت','own:admins'),('✖️ بستن','close')])
            await self.safe_edit(chat_id,message_id,f"*تنظیم ادمین* `{target}`\nروی هر مجوز بزن تا روشن/خاموش شود.",inline(rows)); return
        if data.startswith('own:admin_perms:'):
            if not owner: return
            _,_,target,page=data.split(':')
            target=int(target); page=int(page)
            keys=list(PERMISSION_LABELS.items()); chunk=keys[page*2:(page+1)*2]
            async with self.db._pool().acquire() as c: perms=await c.fetchval('SELECT permissions FROM admins WHERE user_id=$1',target) or {}
            rows=[[(('✅ ' if perms.get(k) else '▫️ ')+label,f'perm:toggle:{target}:{k}')] for k,label in chunk]
            if page>0: rows.append([('⬅️ قبلی',f'own:admin_perms:{target}:{page-1}')])
            if (page+1)*2 < len(keys): rows.append([('بعدی ➡️',f'own:admin_perms:{target}:{page+1}')])
            rows.append([('↩️ برگشت',f'own:admin:{target}'),('✖️ بستن','close')])
            await self.safe_edit(chat_id,message_id,f'*دسترسی‌های ادمین* `{target}`',inline(rows)); return
        if data.startswith('own:admin_del:'):
            if not owner: return
            target=int(data.rsplit(':',1)[1])
            async with self.db._pool().acquire() as c: await c.execute('DELETE FROM admins WHERE user_id=$1',target)
            await self.db.log(uid,'admin_removed','user',target)
            await self.safe_edit(chat_id,message_id,'ادمین حذف شد.',inline([[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return
        if data == 'own:mon' or data.startswith('own:mon:'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ فقط مالک.', parse_mode=None)
                return
            await self._show_owner_monitoring(chat_id, message_id, data)
            return
        if data == 'own:settings':
            if not owner:
                return
            maint = await self._is_maintenance()
            maint_lbl = '🟢 روشن — خاموش کردن' if maint else '🔴 خاموش — روشن کردن'
            maint_state = 'فعال' if maint else 'غیرفعال'
            body = (
                f'*تنظیمات مالک*\n'
                f'نسخه: `{BOT_VERSION}`\n'
                f'شناسه مالک: `{OWNER_ID}`\n\n'
                f'🛠️ حالت تعمیرات: *{maint_state}*\n'
                'با روشن بودن، تمام دستورات و تعامل برای کاربران قطع می‌شود '
                '(فقط مالک می‌تواند ربات را کنترل کند).'
            )
            await self.safe_edit(
                chat_id,
                message_id,
                body,
                inline([
                    [(f'🛠️ تعمیرات: {maint_lbl}', 'own:maint:toggle')],
                    [('👨‍💻 تیم توسعه', 'own:team'), ('📝 درباره ما', 'own:about')],
                    [('💚 حمایت مالی', 'own:donations')],
                    [('↩️ برگشت', 'admin:page:0'), ('✖️ بستن', 'close')],
                ]),
            )
            return
        if data == 'own:maint:toggle':
            if not owner:
                return
            cur = await self._is_maintenance()
            new_v = not cur
            await self.db.set_setting('maintenance_mode', new_v, uid)
            self._maintenance_cache = (new_v, time.time() + 5.0)
            if new_v:
                msg = (
                    '*حالت تعمیرات روشن شد* 🛠️\n\n'
                    'از این لحظه کاربران عادی نمی‌توانند از دستورات استفاده کنند.'
                )
            else:
                msg = (
                    '*حالت تعمیرات خاموش شد* ✅\n\n'
                    'ربات دوباره برای همه فعال است.'
                )
            await self.safe_edit(
                chat_id,
                message_id,
                msg,
                inline([
                    [('⚙️ بازگشت به تنظیمات', 'own:settings')],
                    [('✖️ بستن', 'close')],
                ]),
            )
            return
        if data.startswith('mod:'):
            await self.handle_moderation_callback(uid,chat_id,message_id,data)

    async def handle_permission_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID: return
        _,action,target,key=data.split(':',3)
        target=int(target)
        async with self.db._pool().acquire() as c:
            row=await c.fetchrow('SELECT permissions FROM admins WHERE user_id=$1',target)
            if not row: return await self.safe_send(chat_id,'ادمین پیدا نشد.')
            perms=row['permissions'] or DEFAULT_PERMS.copy()
            perms[key]=not bool(perms.get(key,False))
            await c.execute('UPDATE admins SET permissions=$2 WHERE user_id=$1',target,perms)
        await self.db.log(uid,'admin_permission_changed','user',target,{'permission':key,'enabled':perms[key]})
        # Refresh the first permissions screen.
        await self.handle_admin_callback(uid,chat_id,message_id,f'own:admin:{target}')

    async def handle_target_select(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID and not await self.db.is_admin(uid):
            return await self.safe_send(chat_id, '⛔ دسترسی مدیریت نداری.')
        _, _, target_type, target_id = data.split(':', 3)
        target_id = int(target_id)
        if not await self.has_perm(uid, 'search'):
            return await self.safe_send(chat_id, '⛔ مجوز جست‌وجو نداری.')
        rows = await self.db.search_target(str(target_id), target_type)
        target = rows[0] if rows else {'id': target_id, 'name': str(target_id), 'username': None}
        can_ban = await self.has_perm(uid, 'ban_user' if target_type == 'user' else 'ban_group')
        text = f"*جزئیات {'کاربر' if target_type == 'user' else 'گروه'}*\nنام: {target.get('name') or 'نامشخص'}\nشناسه: `{target_id}`\nآیدی: @{target.get('username') or 'ندارد'}"
        await self.safe_edit(chat_id, message_id, text, user_actions(target_type, target_id, can_ban))

    async def handle_moderation_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        _, action, target_type, target_id = data.split(':',3)
        target_id=int(target_id)
        perm = ('ban_' if action=='ban' else 'unban_')+target_type
        if not await self.has_perm(uid,perm):
            await self.safe_send(chat_id,'⛔ این دسترسی برای حساب تو فعال نیست.')
            return
        if action == 'unban':
            case=await self.db.lift_case(target_type,target_id,uid)
            if not case:
                await self.safe_edit(chat_id,message_id,'برای این مورد محدودیت فعالی ثبت نشده است.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
            await self.safe_edit(chat_id,message_id,f"✅ محدودیت برداشته شد.\nکد پرونده: `#{case['case_code']}`",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        self.states[uid]={'step':'ban_reason','target_type':target_type,'target_id':target_id,'chat_id':chat_id,'message_id':message_id}
        await self.safe_edit(chat_id,message_id,'دلیل محدودیت را بفرست.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))

    # ─── Ad button system ───────────────────────────────────────────────

    _TG_LINK_RE = re.compile(
        r'^(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/)?([A-Za-z0-9_]+)/?$',
        re.IGNORECASE,
    )

    def _validate_tg_link(self, raw: str) -> str | None:
        """Return normalized https://t.me/... URL or None if invalid."""
        text = (raw or '').strip()
        if not text or len(text) > 200:
            return None
        # Accept t.me/... with or without scheme
        m = self._TG_LINK_RE.match(text)
        if not m:
            # Also accept plain t.me/xxx pasted with extra spaces
            low = text.lower().replace('https://', '').replace('http://', '')
            if low.startswith('t.me/') or low.startswith('telegram.me/'):
                m = self._TG_LINK_RE.match(low)
            if not m:
                return None
        slug = m.group(1)
        if not slug or len(slug) < 2:
            return None
        return f'https://t.me/{slug}'

    async def _handle_ad_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID and not await self.has_perm(uid, 'settings'):
            await self.safe_send(chat_id, '⛔ فقط مالک یا ادمین با مجوز تنظیمات می‌تواند تبلیغ را مدیریت کند.')
            return
        enabled, url, title = await self._get_ad_config()
        if data == 'adm:ad':
            status = '🟢 فعال' if enabled and url and title else '🔴 غیرفعال'
            body = (
                f'*📢 مدیریت دکمه تبلیغ*\n\n'
                f'وضعیت: {status}\n'
                f'عنوان: `{title or "—"}`\n'
                f'لینک: `{url or "—"}`\n\n'
                f'با فعال‌بودن، دکمه زیر پیام‌های معمولی ربات (به‌جز GIF) نمایش داده می‌شود.'
            )
            rows = []
            if enabled and url and title:
                rows.append([('🔴 خاموش کردن', 'adm:ad:off')])
                rows.append([('✏️ تغییر لینک', 'adm:ad:set_url'), ('✏️ تغییر عنوان', 'adm:ad:set_title')])
            else:
                rows.append([('🟢 فعال‌سازی / تنظیم', 'adm:ad:setup')])
            if url and title and not enabled:
                rows.append([('🟢 روشن کردن', 'adm:ad:on')])
            rows.append([('↩️ برگشت', 'admin:page:0'), ('✖️ بستن', 'close')])
            await self.safe_edit(chat_id, message_id, body, inline(rows))
            return
        if data == 'adm:ad:off':
            await self.db.set_setting('ad_enabled', False, uid)
            self._invalidate_ad_cache()
            await self.db.log(uid, 'ad_disabled')
            await self.safe_edit(chat_id, message_id, '🔴 تبلیغ خاموش شد. پیام‌های جدید دکمه تبلیغ نخواهند داشت.',
                                 inline([[('↩️ برگشت', 'adm:ad'), ('✖️ بستن', 'close')]]))
            return
        if data == 'adm:ad:on':
            if not url or not title:
                await self.safe_edit(chat_id, message_id, 'ابتدا لینک و عنوان را تنظیم کن.',
                                     inline([[('🟢 فعال‌سازی', 'adm:ad:setup'), ('↩️ برگشت', 'adm:ad')]]))
                return
            await self.db.set_setting('ad_enabled', True, uid)
            self._invalidate_ad_cache()
            await self.db.log(uid, 'ad_enabled')
            preview = url_button(title, url)
            await self.safe_edit(chat_id, message_id, '🟢 تبلیغ فعال شد.\nپیش‌نمایش دکمه:', preview)
            await self.safe_send(chat_id, 'برای بازگشت:', inline([[('↩️ مدیریت تبلیغ', 'adm:ad'), ('✖️ بستن', 'close')]]), skip_ad=True)
            return
        if data in ('adm:ad:setup', 'adm:ad:set_url'):
            self.states[uid] = {'step': 'ad_url', 'chat_id': chat_id, 'message_id': message_id}
            await self.safe_edit(
                chat_id, message_id,
                'لینک کانال، گروه یا ربات موردنظرت رو بفرست. لینک باید با "t.me/..." شروع بشه.',
                inline([[('↩️ لغو', 'adm:ad'), ('✖️ بستن', 'close')]]),
            )
            return
        if data == 'adm:ad:set_title':
            if not url:
                await self.safe_edit(chat_id, message_id, 'ابتدا لینک را تنظیم کن.',
                                     inline([[('✏️ تنظیم لینک', 'adm:ad:set_url'), ('↩️ برگشت', 'adm:ad')]]))
                return
            self.states[uid] = {'step': 'ad_title', 'chat_id': chat_id, 'message_id': message_id, 'ad_url': url}
            await self.safe_edit(
                chat_id, message_id,
                'عنوان دکمه‌ی تبلیغ رو وارد کن؛ این متن روی دکمه نمایش داده می‌شه.',
                inline([[('↩️ لغو', 'adm:ad'), ('✖️ بستن', 'close')]]),
            )
            return

    # ─── Broadcast system ───────────────────────────────────────────────

    async def _handle_broadcast_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID and not await self.has_perm(uid, 'settings'):
            await self.safe_send(chat_id, '⛔ فقط مالک یا ادمین با مجوز تنظیمات می‌تواند پیام همگانی بفرستد.')
            return
        if data == 'adm:broadcast':
            if self._broadcast_running:
                await self.safe_edit(
                    chat_id, message_id,
                    '⏳ یک کمپین ارسال هم‌اکنون در حال اجراست.\nمی‌توانی آن را لغو کنی یا صبر کنی.',
                    inline([[('🛑 لغو کمپین', 'adm:bc:cancel'), ('↩️ برگشت', 'admin:page:0')]]),
                )
                return
            await self.safe_edit(
                chat_id, message_id,
                '*📨 پیام همگانی*\nمخاطبان هدف را انتخاب کن:',
                inline([
                    [('👤 کاربران خصوصی', 'adm:bc:users')],
                    [('👥 گروه‌ها', 'adm:bc:groups')],
                    [('🌐 همه (کاربران + گروه‌ها)', 'adm:bc:all')],
                    [('↩️ برگشت', 'admin:page:0'), ('✖️ بستن', 'close')],
                ]),
            )
            return
        if data == 'adm:bc:cancel':
            self._broadcast_cancel.add(uid)
            await self.safe_edit(chat_id, message_id, 'درخواست لغو ثبت شد. کمپین پس از اتمام مقصد فعلی متوقف می‌شود.',
                                 inline([[('↩️ برگشت', 'admin:page:0')]]))
            return
        if data in ('adm:bc:users', 'adm:bc:groups', 'adm:bc:all'):
            target = data.rsplit(':', 1)[1]
            self.states[uid] = {
                'step': 'broadcast_await_msg',
                'chat_id': chat_id,
                'message_id': message_id,
                'bc_target': target,
            }
            label = {'users': 'کاربران خصوصی', 'groups': 'گروه‌ها', 'all': 'همه مخاطبان'}[target]
            await self.safe_edit(
                chat_id, message_id,
                f'*📨 ارسال به: {label}*\n\nپیام موردنظر را بفرست (متن، عکس، ویدئو، فایل یا GIF با کپشن).',
                inline([[('↩️ لغو', 'adm:broadcast'), ('✖️ بستن', 'close')]]),
            )
            return
        if data == 'adm:bc:confirm':
            state = self.states.get(uid)
            if not state or state.get('step') != 'broadcast_confirm':
                await self.safe_edit(chat_id, message_id, 'جلسه ارسال منقضی شده. دوباره شروع کن.',
                                     inline([[('↩️ برگشت', 'adm:broadcast')]]))
                return
            if self._broadcast_running:
                await self.safe_edit(chat_id, message_id, 'کمپین دیگری در حال اجراست.',
                                     inline([[('↩️ برگشت', 'admin:page:0')]]))
                return
            self.states.pop(uid, None)
            task = asyncio.create_task(self._run_broadcast(uid, chat_id, state), name=f'bc-{uid}')
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            await self.safe_edit(chat_id, message_id, '🚀 ارسال آغاز شد. گزارش در پایان ارسال می‌شود.',
                                 inline([[('🛑 لغو', 'adm:bc:cancel')]]))
            return
        if data == 'adm:bc:abort':
            self.states.pop(uid, None)
            await self.safe_edit(chat_id, message_id, 'ارسال لغو شد.',
                                 inline([[('↩️ برگشت', 'adm:broadcast'), ('✖️ بستن', 'close')]]))
            return

    async def _run_broadcast(self, admin_uid: int, report_chat: int, state: dict) -> None:
        self._broadcast_running = True
        self._broadcast_cancel.discard(admin_uid)
        target = state.get('bc_target', 'users')
        payload = state.get('bc_payload') or {}
        try:
            if target == 'users':
                ids = await self.db.private_user_ids()
            elif target == 'groups':
                ids = await self.db.group_chat_ids()
            else:
                ids = list(dict.fromkeys(
                    (await self.db.private_user_ids()) + (await self.db.group_chat_ids())
                ))
            # Exclude the admin and the bot itself
            bot_id = getattr(self, 'bot_id', None)
            ids = [i for i in ids if i != admin_uid and i != bot_id]
            total = len(ids)
            sent = failed = blocked = 0
            kind = payload.get('kind', 'text')
            for idx, dest in enumerate(ids):
                if admin_uid in self._broadcast_cancel:
                    remaining = total - idx
                    await self.safe_send(
                        report_chat,
                        f'🛑 کمپین لغو شد.\n✅ موفق: {sent}\n❌ ناموفق: {failed}\n🚫 مسدود/غیرقابل‌دسترسی: {blocked}\n⏭ باقی‌مانده: {remaining}',
                        skip_ad=True,
                    )
                    return
                try:
                    ok = await self._broadcast_one(dest, payload)
                    if ok:
                        sent += 1
                    else:
                        failed += 1
                except Exception as exc:
                    failed += 1
                    err = str(exc).lower()
                    if 'blocked' in err or 'forbidden' in err or 'chat not found' in err or 'have no rights' in err:
                        blocked += 1
                    log.warning('broadcast dest=%s err=%s', dest, type(exc).__name__)
                # Rate limit: ~15 msg/sec max, stay conservative
                if idx % 10 == 9:
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.08)
            await self.safe_send(
                report_chat,
                f'✅ *گزارش پیام همگانی*\nمخاطبان هدف: {total}\nموفق: {sent}\nناموفق: {failed}\nمسدود/غیرقابل‌دسترسی: {blocked}\nباقی‌مانده: 0',
                skip_ad=True,
            )
            await self.db.log(admin_uid, 'broadcast_done', details={
                'target': target, 'total': total, 'sent': sent, 'failed': failed, 'blocked': blocked, 'kind': kind,
            })
        except Exception:
            log.exception('broadcast campaign crashed')
            try:
                await self.safe_send(report_chat, '⚠️ کمپین با خطا متوقف شد. جزئیات در لاگ سرور.', skip_ad=True)
            except Exception:
                pass
        finally:
            self._broadcast_running = False
            self._broadcast_cancel.discard(admin_uid)

    async def _broadcast_one(self, dest: int, payload: dict) -> bool:
        kind = payload.get('kind', 'text')
        kb = None
        # Attach ad unless GIF
        if kind != 'animation':
            kb = await self._with_ad_keyboard(None)
        try:
            if kind == 'text':
                result = await self.api.send_message(dest, payload['text'], kb, None, payload.get('parse_mode'), fast=True)
            elif kind == 'photo':
                result = await self.api.send_photo(dest, payload['file_id'], payload.get('caption'), kb, payload.get('parse_mode'))
            elif kind == 'video':
                result = await self.api.send_video(dest, payload['file_id'], payload.get('caption'), kb, payload.get('parse_mode'))
            elif kind == 'document':
                result = await self.api.send_document(dest, payload['file_id'], payload.get('caption'), kb, payload.get('parse_mode'))
            elif kind == 'animation':
                result = await self.api.send_animation_file_id(dest, payload['file_id'], payload.get('caption'), None, payload.get('parse_mode'))
            elif kind == 'copy':
                result = await self.api.copy_message(dest, payload['from_chat_id'], payload['message_id'], kb)
            else:
                return False
            return result is not None
        except TelegramAPIError as exc:
            err = str(exc).lower()
            if any(x in err for x in ('blocked', 'forbidden', 'chat not found', 'have no rights', 'bot was kicked', 'user is deactivated')):
                return False
            raise

    # ─── Learned phrases helpers ────────────────────────────────────────

    def _extract_broadcast_payload(self, msg: dict, text: str) -> dict | None:
        """Build a serialisable payload from an admin message for later re-send."""
        caption = (msg.get('caption') or '').strip() or None
        if msg.get('photo'):
            photos = msg['photo']
            best = photos[-1] if isinstance(photos, list) else photos
            fid = best.get('file_id')
            if fid:
                return {'kind': 'photo', 'file_id': fid, 'caption': caption or text or None, 'parse_mode': None}
        if msg.get('video'):
            fid = msg['video'].get('file_id')
            if fid:
                return {'kind': 'video', 'file_id': fid, 'caption': caption or text or None, 'parse_mode': None}
        if msg.get('animation'):
            fid = msg['animation'].get('file_id')
            if fid:
                return {'kind': 'animation', 'file_id': fid, 'caption': caption or text or None, 'parse_mode': None}
        if msg.get('document'):
            fid = msg['document'].get('file_id')
            if fid:
                return {'kind': 'document', 'file_id': fid, 'caption': caption or text or None, 'parse_mode': None}
        if text and text.strip():
            return {'kind': 'text', 'text': text.strip(), 'parse_mode': None}
        # Fallback: copy original message
        mid = msg.get('message_id')
        from_chat = (msg.get('chat') or {}).get('id')
        if mid and from_chat:
            return {'kind': 'copy', 'from_chat_id': int(from_chat), 'message_id': int(mid)}
        return None

    @staticmethod
    def _normalize_trigger(text: str) -> str:
        """Normalize Persian letters and whitespace for stable lookup."""
        t = (text or '').strip()
        t = t.replace('ي', 'ی').replace('ك', 'ک')
        t = re.sub(r'\s+', ' ', t)
        return t.lower()

    def _parse_learn_command(self, text: str) -> tuple[str, str] | None:
        """Parse 'یاد بگیر X = Y' or 'یاد بگیر X Y'. Returns (trigger, response) or None."""
        t = (text or '').strip()
        if not t.startswith('یاد بگیر') or t.startswith('گروهی یاد بگیر'):
            return None
        rest = t[len('یاد بگیر'):].strip()
        if not rest:
            return None
        if '=' in rest:
            left, _, right = rest.partition('=')
            trigger, response = left.strip(), right.strip()
        else:
            parts = rest.split(None, 1)
            if len(parts) < 2:
                return None
            trigger, response = parts[0].strip(), parts[1].strip()
        if not trigger or not response:
            return None
        if len(trigger) > 200 or len(response) > 2000:
            return None
        return trigger, response

    def _parse_group_learn_pairs(self, text: str) -> tuple[list[tuple[str, str]] | None, str | None]:
        """Parse multi-line group learn.
        Expected format:
          گروهی یاد بگیر
          trigger1
          response1
          trigger2
          response2
          ...
        Returns (pairs, error_message). pairs is None on validation error.
        """
        raw = (text or '').replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.strip() for ln in raw.split('\n')]
        # Drop empty lines at start/end but keep internal empties for strict pairing
        while lines and lines[0] == '':
            lines.pop(0)
        while lines and lines[-1] == '':
            lines.pop()
        if not lines:
            return None, '❌ انجام نشد به دلیل: فرمت ورودی معتبر نیست.'
        first = lines[0]
        if not first.startswith('گروهی یاد بگیر'):
            return None, '❌ انجام نشد به دلیل: فرمت ورودی معتبر نیست.'
        # Anything after the command on the same line is ignored / treated as invalid start
        body = lines[1:]
        # Remove pure empty lines from body so they don't shift pairs
        body = [ln for ln in body if ln != '']
        if not body:
            return None, '❌ انجام نشد به دلیل: هیچ جفت کلمه و جوابی بعد از دستور نوشته نشده است.'
        if len(body) % 2 != 0:
            return None, '❌ انجام نشد به دلیل: تعداد خطوط ورودی زوج نیست و یک کلمه بدون جواب باقی مانده است.'
        pairs: list[tuple[str, str]] = []
        for i in range(0, len(body), 2):
            trigger = body[i].strip()
            response = body[i + 1].strip()
            if not trigger or not response:
                return None, '❌ انجام نشد به دلیل: یک جفت ناقص (کلمه یا جواب خالی) وجود دارد.'
            if len(trigger) > 200 or len(response) > 2000:
                return None, '❌ انجام نشد به دلیل: طول کلمه یا جواب بیش از حد مجاز است.'
            pairs.append((trigger, response))
        if not pairs:
            return None, '❌ انجام نشد به دلیل: هیچ جفت معتبری پیدا نشد.'
        return pairs, None

    def _whisper_rate_ok(self, uid: int) -> bool:
        now = time.time()
        bucket = self._whisper_sends.setdefault(uid, [])
        self._whisper_sends[uid] = [t for t in bucket if now - t < WHISPER_RATE_WINDOW]
        if len(self._whisper_sends[uid]) >= WHISPER_RATE_LIMIT:
            return False
        self._whisper_sends[uid].append(now)
        return True

    async def _handle_whisper(self, chat_id: int, uid: int, text: str, msg: dict, replied: dict) -> None:
        """Private whisper via reply: «نجوا متن...»"""
        body = text[len('نجوا'):].strip()
        if not body:
            await self.safe_send(
                chat_id,
                '❓ دستور کامل نیست! روی پیام کاربر ریپلای کن و بنویس: نجوا متن پیام',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return
        if len(body) > 1000:
            await self.safe_send(chat_id, '❌ متن نجوا خیلی بلنده (حداکثر ۱۰۰۰ کاراکتر).', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if not replied:
            await self.safe_send(
                chat_id,
                '❓ برای نجوا باید روی پیام کاربر هدف *ریپلای* کنی.',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return
        target = replied.get('from') or replied.get('sender') or {}
        recipient_id = int(target.get('id', 0) or 0)
        if not recipient_id or target.get('is_bot'):
            await self.safe_send(chat_id, '❌ فقط می‌تونی برای یک کاربر واقعی نجوا بفرستی.', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if self.bot_id and recipient_id == self.bot_id:
            await self.safe_send(chat_id, '😂 نمی‌تونی برای خود ربات نجوا بفرستی!', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if recipient_id == uid:
            await self.safe_send(chat_id, '🤔 نمی‌تونی برای خودت نجوا بفرستی!', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if not self._whisper_rate_ok(uid):
            await self.safe_send(chat_id, '⏳ کمی صبر کن؛ تعداد نجواهات در دقیقه زیاد شده.', reply_to=msg.get('message_id'), parse_mode=None)
            return

        sender_user = msg.get('from') or msg.get('sender') or {}
        sender_name = (sender_user.get('first_name') or '').strip() or 'ناشناس'

        try:
            whisper_id = await self.db.create_whisper(chat_id, uid, recipient_id, body, sender_name=sender_name)
        except Exception:
            log.exception('create_whisper failed chat=%s', chat_id)
            await self.safe_send(chat_id, '❌ انجام نشد به دلیل: ذخیره اطلاعات در دیتابیس ناموفق بود.', reply_to=msg.get('message_id'), parse_mode=None)
            return

        # Delete original message (best-effort; never leak body on failure)
        mid = msg.get('message_id')
        if mid:
            try:
                await self.api.delete_message(chat_id, int(mid))
            except Exception as exc:
                log.info('whisper delete_message failed chat=%s: %s', chat_id, type(exc).__name__)

        # Public announcement: reply to recipient message, name only (no numeric id)
        try:
            recipient_msg_id = replied.get('message_id')
            await self.safe_send(
                chat_id,
                f'🤫 نجوا داری از طرف {sender_name}',
                keyboard=inline([[('👁️ دیدن نجوا', f'wh:view:{whisper_id}')]]),
                reply_to=int(recipient_msg_id) if recipient_msg_id else None,
                parse_mode=None,
                skip_ad=True,
            )
        except Exception:
            log.exception('whisper announce failed')
            # Do not fall back to posting the body in the group
            await self.safe_send(chat_id, '❌ اعلان نجوا ارسال نشد. بعداً دوباره تلاش کن.', parse_mode=None)

    async def _handle_whisper_callback(
        self, uid: int, chat_id: int, message_id: int, data: str, callback_id: str | None,
    ) -> bool:
        """Handle wh:view / wh:ok. Returns True if handled (caller should skip default ack)."""
        if data.startswith('wh:view:'):
            try:
                wid = int(data.rsplit(':', 1)[1])
            except ValueError:
                if callback_id:
                    await self.api.answer_callback(callback_id, 'داده نامعتبر.', show_alert=True)
                return True
            try:
                row = await self.db.get_whisper_for_recipient(wid, uid, chat_id)
            except Exception:
                log.exception('get_whisper failed')
                if callback_id:
                    await self.api.answer_callback(callback_id, 'خطا در دریافت نجوا.', show_alert=True)
                return True
            if not row:
                if callback_id:
                    await self.api.answer_callback(
                        callback_id,
                        '⛔ این نجوا برای تو نیست یا منقضی شده.',
                        show_alert=True,
                    )
                return True
            sender_id = row.get('sender_id')
            sender_name = (row.get('sender_name') or '').strip() or 'ناشناس'
            body_full = row.get('body') or ''
            # Alert text is short; name only (no numeric id)
            alert_text = f'از طرف {sender_name}:\n{body_full}'
            if len(alert_text) > 190:
                alert_text = alert_text[:187] + '...'
            # Prefer private alert so group never sees body
            if callback_id:
                try:
                    await self.api.answer_callback(callback_id, alert_text or '(خالی)', show_alert=True)
                except Exception:
                    # Fallback: try DM to recipient only
                    try:
                        await self.api.send_message(
                            uid,
                            f'🤫 نجوا از طرف {sender_name}:\n{body_full}',
                            parse_mode=None,
                        )
                        await self.api.answer_callback(callback_id, 'نجوا در پیوی برات ارسال شد.')
                    except Exception:
                        await self.api.answer_callback(
                            callback_id,
                            'نتونستم نجوا رو نشون بدم. پیوی ربات رو باز کن و استارت بزن.',
                            show_alert=True,
                        )
            # Offer dismiss on the group announcement (still no body)
            try:
                await self.safe_edit(
                    chat_id,
                    message_id,
                    '🤫 نجوا باز شد.',
                    inline([[('باشه 👌', f'wh:ok:{wid}')]]),
                )
            except Exception:
                pass
            return True
        if data.startswith('wh:ok:'):
            if callback_id:
                try:
                    await self.api.answer_callback(callback_id)
                except Exception:
                    pass
            try:
                await self.safe_edit(chat_id, message_id, '👌', None)
            except Exception:
                pass
            return True
        return False

    async def _handle_group_learn(self, chat_id: int, uid: int, is_group: bool, text: str, msg: dict) -> None:
        """Handle «گروهی یاد بگیر» multi-pair learning."""
        pairs, err = self._parse_group_learn_pairs(text)
        if err or pairs is None:
            await self.safe_send(chat_id, err or '❌ انجام نشد به دلیل: فرمت ورودی معتبر نیست.', reply_to=msg.get('message_id'), parse_mode=None)
            return
        if is_group:
            scope_type, scope_id = 'group', chat_id
        else:
            scope_type, scope_id = 'private', uid
        saved = 0
        inserted = 0
        try:
            for trigger_raw, response in pairs:
                trigger_norm = self._normalize_trigger(trigger_raw)
                if not trigger_norm:
                    continue
                result = await self.db.upsert_learned(scope_type, scope_id, trigger_norm, trigger_raw, response, uid)
                saved += 1
                if result == 'inserted':
                    inserted += 1
        except Exception:
            log.exception('group_learn upsert failed scope=%s id=%s', scope_type, scope_id)
            await self.safe_send(
                chat_id,
                '❌ انجام نشد به دلیل: ذخیره اطلاعات در دیتابیس ناموفق بود.',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return
        if saved == 0:
            await self.safe_send(
                chat_id,
                '❌ انجام نشد به دلیل: هیچ جفت معتبری برای ذخیره پیدا نشد.',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return
        points_line = ''
        if is_group and inserted > 0:
            try:
                pts = inserted * LEARN_POINTS
                total_pts = await self.db.add_learn_points(chat_id, uid, pts)
                points_line = f'\n🏆 {pts} امتیاز اضافه شد (برای {inserted} کلمه جدید). مجموع: {total_pts}'
            except Exception:
                log.exception('add_learn_points failed')
        await self.safe_send(
            chat_id,
            f'✅ یادگیری گروهی با موفقیت انجام شد!\n🔤 تعداد کلمات ثبت‌شده: {saved}\n📌 هر کلمه به‌صورت مستقل قابل استفاده است.{points_line}',
            reply_to=msg.get('message_id'),
            parse_mode=None,
        )

    # ── Group security / fun / stats helpers ─────────────────────────────

    _FLOOD_WINDOW: dict[tuple[int, int], list[float]] = {}

    # Lock keys shown in panel (label, security key)
    PURGE_MAX = 150
    PURGE_CONFIRM_THRESHOLD = 50

    def _track_group_mid(self, chat_id: int, mid: int) -> None:
        if not mid:
            return
        buf = self._recent_mids.get(chat_id)
        if buf is None:
            buf = deque(maxlen=200)
            self._recent_mids[chat_id] = buf
        if not buf or buf[-1] != mid:
            buf.append(int(mid))

    @staticmethod
    def _fa_digits_to_en(s: str) -> str:
        table = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
        return (s or '').translate(table)

    def _collect_purge_mids_count(self, chat_id: int, count: int, current_mid: int | None) -> list[int]:
        """Last N message ids from buffer; fill gaps by counting back from current if needed."""
        count = max(1, min(int(count), self.PURGE_MAX))
        buf = list(self._recent_mids.get(chat_id) or [])
        mids: list[int] = []
        if buf:
            mids = buf[-count:]
        # Also try sequential range from current_mid backward (Telegram ids are usually sequential)
        if current_mid and len(mids) < count:
            start = max(1, int(current_mid) - count + 1)
            seq = list(range(start, int(current_mid) + 1))
            mids = sorted(set(mids) | set(seq))[-count:]
        if current_mid and current_mid not in mids:
            mids.append(int(current_mid))
        return sorted(set(int(x) for x in mids if x))

    def _collect_purge_mids_from_reply(
        self, chat_id: int, from_mid: int, current_mid: int | None,
    ) -> list[int]:
        """Delete replied message and all newer ones we know of (mid >= from_mid)."""
        from_mid = int(from_mid)
        buf = list(self._recent_mids.get(chat_id) or [])
        mids = [m for m in buf if m >= from_mid]
        if current_mid:
            # sequential fill between from and current
            hi = int(current_mid)
            if hi >= from_mid:
                mids.extend(range(from_mid, hi + 1))
        mids.append(from_mid)
        mids = sorted(set(int(x) for x in mids if x))
        if len(mids) > self.PURGE_MAX:
            mids = mids[-self.PURGE_MAX:]
        return mids

    async def _delete_mids(self, chat_id: int, mids: list[int]) -> tuple[int, int]:
        """Delete as fast as possible. On any single failure, skip and continue.
        No cooldown. Always returns (ok_count, fail_count).
        """
        if not mids:
            return 0, 0
        unique = sorted(set(int(m) for m in mids if m))
        sem = asyncio.Semaphore(25)

        async def _one(mid: int) -> bool:
            async with sem:
                try:
                    await self.api.delete_message(chat_id, mid)
                    return True
                except Exception:
                    return False  # skip and continue others

        results = await asyncio.gather(*(_one(m) for m in unique), return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        fail = len(unique) - ok
        # prune successfully deleted from buffer; keep failed for possible retry
        buf = self._recent_mids.get(chat_id)
        if buf is not None:
            failed_set = {m for m, r in zip(unique, results) if r is not True}
            attempted = set(unique)
            self._recent_mids[chat_id] = deque(
                (m for m in buf if m not in attempted or m in failed_set),
                maxlen=200,
            )
        return ok, fail

    async def _handle_purge_command(
        self, chat_id: int, uid: int, text: str, msg: dict, replied: dict,
    ) -> bool:
        t = self._fa_digits_to_en((text or '').strip())
        t = re.sub(r'\s+', ' ', t)
        if not (t == 'پاک' or t.startswith('پاک ')):
            return False

        if not await self._require_group_admin(chat_id, uid):
            await self.safe_send(
                chat_id,
                '⛔ فقط مدیران گروه می‌توانند از دستور پاک استفاده کنند!',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return True

        current_mid = int(msg.get('message_id') or 0) or None
        mids: list[int] = []

        if t == 'پاک' and replied and replied.get('message_id'):
            from_mid = int(replied['message_id'])
            mids = self._collect_purge_mids_from_reply(chat_id, from_mid, current_mid)
            # include the admin's «پاک» command message
            if current_mid:
                mids = sorted(set(mids) | {current_mid})
        elif t.startswith('پاک '):
            rest = t[len('پاک '):].strip()
            if not rest.isdigit():
                await self.safe_send(
                    chat_id,
                    '❓ فرمت: `پاک ۵` یا ریپلای روی پیام + `پاک`\nحداکثر ۱۵۰ پیام.',
                    reply_to=msg.get('message_id'),
                    parse_mode=None,
                )
                return True
            n = int(rest)
            if n < 1:
                await self.safe_send(chat_id, '❌ عدد باید حداقل ۱ باشد.', reply_to=msg.get('message_id'), parse_mode=None)
                return True
            if n > self.PURGE_MAX:
                await self.safe_send(chat_id, f'❌ حداکثر {self.PURGE_MAX} پیام آخر قابل حذف است.', reply_to=msg.get('message_id'), parse_mode=None)
                return True
            mids = self._collect_purge_mids_count(chat_id, n, current_mid)
        else:
            # bare «پاک» without reply
            await self.safe_send(
                chat_id,
                '❓ برای پاک کردن:\n• `پاک ۵` — آخرین پیام‌ها\n• ریپلای روی یک پیام + `پاک` — همان پیام و پیام‌های بعدش',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return True

        if not mids:
            await self.safe_send(chat_id, '🔎 پیامی برای حذف پیدا نشد.', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        count = len(mids)
        if count >= self.PURGE_CONFIRM_THRESHOLD:
            token = secrets.token_hex(4)
            self._purge_pending[token] = {
                'chat_id': chat_id,
                'uid': uid,
                'mids': mids,
                'expires': time.time() + 120,
            }
            await self.safe_send(
                chat_id,
                f'⚠️ آیا از انجام دادن این عملیات مطمئن هستید؟\n🗑 حدود *{count}* پیام حذف می‌شود.',
                keyboard=inline([
                    [('✅ بله', f'purge:yes:{token}'), ('❌ خیر', f'purge:no:{token}')],
                ]),
                parse_mode=None,
            )
            return True

        ok, fail = await self._delete_mids(chat_id, mids)
        total = ok + fail
        await self.safe_send(
            chat_id,
            f'✅ نتیجه پاک‌سازی\n🗑 پاک شد: {ok}\n⚠️ پاک نشد: {fail}\n📌 مجموع تلاش: {total}',
            parse_mode=None,
        )
        return True

    async def _handle_purge_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        parts = data.split(':', 2)
        if len(parts) < 3:
            return
        _, decision, token = parts
        pending = self._purge_pending.get(token)
        if not pending or pending.get('expires', 0) < time.time():
            self._purge_pending.pop(token, None)
            await self.safe_edit(chat_id, message_id, '⌛ این درخواست منقضی شده.', None)
            return
        if int(pending.get('chat_id') or 0) != chat_id:
            await self.safe_send(chat_id, '⛔ این عملیات مربوط به این گروه نیست.', parse_mode=None)
            return
        if int(pending.get('uid') or 0) != uid and uid != OWNER_ID:
            await self.safe_send(chat_id, '⛔ فقط کسی که درخواست داده می‌تواند تأیید کند.', parse_mode=None)
            return
        if not await self._require_group_admin(chat_id, uid):
            await self.safe_send(chat_id, '⛔ برای این کار باید مدیر گروه یا کاربر ویژه باشی!', parse_mode=None)
            return
        if decision == 'no':
            self._purge_pending.pop(token, None)
            await self.safe_edit(chat_id, message_id, '❌ عملیات پاک‌سازی لغو شد.', None)
            return
        if decision != 'yes':
            return
        mids = list(pending.get('mids') or [])
        self._purge_pending.pop(token, None)
        await self.safe_edit(chat_id, message_id, '⏳ در حال پاک‌سازی...', None)
        ok, fail = await self._delete_mids(chat_id, mids)
        total = ok + fail
        await self.safe_send(
            chat_id,
            f'✅ نتیجه پاک‌سازی\n🗑 پاک شد: {ok}\n⚠️ پاک نشد: {fail}\n📌 مجموع تلاش: {total}',
            parse_mode=None,
        )

    async def _resolve_mod_target(
        self, chat_id: int, text_arg: str, replied: dict, entities: list | None = None,
    ) -> tuple[int | None, str, str | None]:
        """Resolve target from reply, @username, text_mention entity, or numeric id.
        Returns (user_id, display_name, error_message).
        """
        # 1) Reply
        if replied:
            tu = replied.get('from') or replied.get('sender') or {}
            tid = int(tu.get('id') or 0)
            if tid:
                return tid, (tu.get('first_name') or str(tid)).strip(), None

        # 2) text_mention / mention entities
        if entities:
            for e in entities:
                et = (e.get('type') or '').lower()
                if et == 'text_mention' and e.get('user'):
                    u = e['user']
                    tid = int(u.get('id') or 0)
                    if tid:
                        return tid, (u.get('first_name') or str(tid)).strip(), None

        arg = (text_arg or '').strip()
        if not arg:
            return None, '', '❓ کاربر هدف مشخص نیست. روی پیامش ریپلای کن یا با @یوزرنیم / آیدی بنویس.'

        # 3) @username or bare username / numeric id
        term = arg.lstrip('@').strip()
        if term.isdigit() or (term.startswith('-') and term[1:].isdigit()):
            tid = int(term)
            try:
                found = await self.db.search_target(str(tid), 'user')
                name = found[0]['name'] if found else str(tid)
            except Exception:
                name = str(tid)
            return tid, str(name), None

        try:
            found = await self.db.search_target(term, 'user')
        except Exception:
            found = []
        if not found:
            return None, '', f'❌ کاربر «{term}» پیدا نشد. باید قبلاً با ربات چت کرده باشه یا آیدی عددی بفرستی.'
        tid = int(found[0]['id'])
        name = found[0].get('name') or found[0].get('username') or str(tid)
        return tid, str(name), None

    def _parse_mod_command(self, text: str) -> tuple[str, int, str] | None:
        """Parse moderation/warn commands.
        Returns (action, amount, rest_arg) or None.
        actions: mute unmute ban unban kick warn remove_warn clear_warns_all clear_warns_user
        """
        raw = (text or '').replace('‌', '').replace('ـ', '').strip()
        raw = raw.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ي', 'ی').replace('ك', 'ک')
        # normalize multi spaces
        raw = re.sub(r'\s+', ' ', raw)
        low = raw

        # Clear all warns in group (no target)
        clear_all_phrases = (
            'حذف تمام اخطار ها', 'حذف تمام اخطارها', 'حذف همه اخطار ها', 'حذف همه اخطارها',
            'حذف احطار ها', 'حذف احطارها', 'حذف تمام احطار ها', 'حذف تمام احطارها',
            'پاک کردن تمام اخطارها', 'پاک کردن همه اخطارها',
        )
        if low in clear_all_phrases or low.startswith('حذف تمام اخطار') or low.startswith('حذف همه اخطار') or low.startswith('حذف احطار'):
            return 'clear_warns_all', 0, ''

        # remove one warn: حذف اخطار [@user]
        if low == 'حذف اخطار' or low.startswith('حذف اخطار '):
            rest = low[len('حذف اخطار'):].strip()
            return 'remove_warn', 1, rest

        # add warns: اخطار / اخطار ۳ / اخطار 3 @user / اخظار ۳
        m = re.match(r'^(?:اخطار|اخظار)(?:\s+(\d+))?(?:\s+(.+))?$', low)
        if m:
            amount = int(m.group(1) or 1)
            rest = (m.group(2) or '').strip()
            amount = max(1, min(amount, 50))
            return 'warn', amount, rest

        # Classic mod: بن @user / بن / آن بن @x
        exact_map = {
            'سکوت': 'mute',
            'حذف سکوت': 'unmute',
            'بن': 'ban',
            'حذف بن': 'unban',
            'انبن': 'unban',
            'آنبن': 'unban',
            'آن بن': 'unban',
            'ان بن': 'unban',
            'کیک': 'kick',
        }
        if low in exact_map:
            return exact_map[low], 0, ''
        for phrase, action in sorted(exact_map.items(), key=lambda x: -len(x[0])):
            if low.startswith(phrase + ' '):
                return action, 0, low[len(phrase):].strip()
        return None

    async def _handle_group_mod_text(
        self, chat_id: int, uid: int, text: str, msg: dict, replied: dict,
    ) -> bool:
        """Handle group mod/warn commands. Returns True if handled."""
        parsed = self._parse_mod_command(text)
        if not parsed:
            return False
        action, amount, rest = parsed

        is_admin = await self._has_group_mod_power(chat_id, uid)
        if not is_admin:
            await self.safe_send(chat_id, '⛔ برای این کار باید مدیر گروه یا کاربر ویژه باشی!', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        # Clear ALL warns in group — no target needed
        if action == 'clear_warns_all':
            try:
                n = await self.db.clear_all_group_warns(chat_id)
                await self.safe_send(
                    chat_id,
                    f'✅ تمام اخطارهای گروه پاک شد. ({n} کاربر)',
                    reply_to=msg.get('message_id'),
                    parse_mode=None,
                )
            except Exception:
                log.exception('clear_all_group_warns failed')
                await self.safe_send(chat_id, '❌ پاک‌سازی اخطارها ناموفق بود.', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        entities = msg.get('entities') or []
        target_uid, target_name, err = await self._resolve_mod_target(chat_id, rest, replied, entities)
        if err or not target_uid:
            await self.safe_send(chat_id, err or '❓ کاربر هدف پیدا نشد.', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        if self.bot_id and target_uid == self.bot_id:
            await self.safe_send(
                chat_id,
                '😂 منو می‌خوای بن کنی؟! داداش من خودم رباتم و نمی‌تونم خودمو بن کنم!',
                reply_to=msg.get('message_id'),
                parse_mode=None,
            )
            return True
        if target_uid == OWNER_ID:
            await self.safe_send(chat_id, '⛔ مالک ربات قابل مدیریت با این دستور نیست.', reply_to=msg.get('message_id'), parse_mode=None)
            return True
        if target_uid == uid and action in ('ban', 'kick', 'mute', 'warn'):
            await self.safe_send(chat_id, '🤔 نمی‌تونم این عملیات رو روی خودت انجام بدم!', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        # Warn ops
        if action == 'warn':
            try:
                total = await self.db.add_warns(chat_id, target_uid, amount, reason='دستور ادمین')
                gs = await self.db.get_group_settings(chat_id)
                limit = int((gs.get('moderation') or {}).get('warn_limit') or gs.get('warn_limit') or 5)
                await self.safe_send(
                    chat_id,
                    f'⚠️ به کاربر {target_name} *{amount}* اخطار داده شد.\nمجموع اخطار: {total}/{limit}',
                    reply_to=msg.get('message_id'),
                    parse_mode=None,
                )
                if total >= limit and (gs.get('moderation') or {}).get('auto_punish', True):
                    await self._execute_auto_punish(chat_id, target_uid, total, gs.get('moderation') or {})
            except Exception:
                log.exception('manual warn failed')
                await self.safe_send(chat_id, '❌ ثبت اخطار ناموفق بود.', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        if action == 'remove_warn':
            try:
                before = await self.db.get_warns(chat_id, target_uid)
                if before <= 0:
                    await self.safe_send(
                        chat_id,
                        f'ℹ️ کاربر {target_name} اخطار فعالی ندارد.',
                        reply_to=msg.get('message_id'),
                        parse_mode=None,
                    )
                    return True
                after = await self.db.remove_warn(chat_id, target_uid, 1)
                await self.safe_send(
                    chat_id,
                    f'✅ یک اخطار از {target_name} حذف شد.\nاخطار باقی‌مانده: {after}',
                    reply_to=msg.get('message_id'),
                    parse_mode=None,
                )
            except Exception:
                log.exception('remove_warn failed')
                await self.safe_send(chat_id, '❌ حذف اخطار ناموفق بود.', reply_to=msg.get('message_id'), parse_mode=None)
            return True

        # API actions: mute/unmute/ban/unban/kick
        try:
            if action == 'mute':
                await self.api.restrict_chat_member(
                    chat_id, target_uid,
                    {
                        'can_send_messages': False,
                        'can_send_media_messages': False,
                        'can_send_other_messages': False,
                        'can_add_web_page_previews': False,
                    },
                )
                await self.safe_send(chat_id, f'✅ انجام شد! 🔇 کاربر {target_name} سکوت شد.', reply_to=msg.get('message_id'), parse_mode=None)
                await self.db.log(uid, 'group_mute', 'user', target_uid, {'chat_id': chat_id})
            elif action == 'unmute':
                await self.api.restrict_chat_member(
                    chat_id, target_uid,
                    {
                        'can_send_messages': True,
                        'can_send_media_messages': True,
                        'can_send_other_messages': True,
                        'can_add_web_page_previews': True,
                    },
                )
                await self.safe_send(chat_id, f'✅ انجام شد! 🔊 سکوت کاربر {target_name} برداشته شد.', reply_to=msg.get('message_id'), parse_mode=None)
                await self.db.log(uid, 'group_unmute', 'user', target_uid, {'chat_id': chat_id})
            elif action == 'ban':
                await self.api.ban_chat_member(chat_id, target_uid)
                await self.safe_send(chat_id, f'✅ انجام شد! ⛔ کاربر {target_name} از گروه بن شد.', reply_to=msg.get('message_id'), parse_mode=None)
                await self.db.log(uid, 'group_ban', 'user', target_uid, {'chat_id': chat_id})
            elif action == 'unban':
                await self.api.unban_chat_member(chat_id, target_uid)
                await self.safe_send(chat_id, f'✅ انجام شد! بن کاربر {target_name} برداشته شد.', reply_to=msg.get('message_id'), parse_mode=None)
                await self.db.log(uid, 'group_unban', 'user', target_uid, {'chat_id': chat_id})
            elif action == 'kick':
                await self.api.ban_chat_member(chat_id, target_uid, int(time.time()) + 45)
                await self.api.unban_chat_member(chat_id, target_uid)
                await self.safe_send(chat_id, f'✅ انجام شد! 👢 کاربر {target_name} از گروه اخراج شد.', reply_to=msg.get('message_id'), parse_mode=None)
                await self.db.log(uid, 'group_kick', 'user', target_uid, {'chat_id': chat_id})
        except Exception as exc:
            err_l = str(exc).lower()
            if any(x in err_l for x in ('not enough rights', 'permission', "can't", 'cannot', 'forbidden', 'have no rights', 'not an admin', 'chat_admin_required')):
                user_msg = '⚠️ برای انجام این کار، من به دسترسی مدیریتی مناسب نیاز دارم. لطفاً دسترسی‌های من رو بررسی کن.'
            elif any(x in err_l for x in ('bot is not', 'not a member', 'kicked', 'deactivated')):
                user_msg = '🛡️ اول منو مدیر گروه کن و دسترسی‌های لازم رو بهم بده، بعد دوباره امتحان کن!'
            else:
                user_msg = '⚠️ عملیات انجام نشد. دسترسی‌های مدیریتی ربات در گروه را بررسی کن.'
                log.warning('mod action failed chat=%s action=%s: %s', chat_id, action, type(exc).__name__)
            await self.safe_send(chat_id, user_msg, reply_to=msg.get('message_id'), parse_mode=None)
        return True

    LOCK_LABELS: list[tuple[str, str]] = [
        ('antispam', '🚫 ضداسپم'),
        ('antilink', '🔗 قفل لینک'),
        ('antirepeat', '🔁 قفل پیام تکراری'),
        ('antimention', '📢 قفل منشن انبوه'),
        ('antiforward', '📄 قفل فوروارد'),
        ('antiphoto', '🖼️ قفل عکس'),
        ('antivideo', '🎬 قفل ویدیو'),
        ('antidoc', '📁 قفل فایل'),
        ('antiaudio', '🎵 قفل آهنگ'),
        ('antivoice', '🎤 قفل ویس'),
        ('antisticker', '😀 قفل استیکر'),
        ('antigif', '✨ قفل گیف'),
        ('antipoll', '🗳️ قفل نظرسنجی'),
        ('antilong', '📝 قفل پیام بلند'),
        ('antiflood', '⚡ ضد Flood'),
        ('antiad', '📣 ضدتبلیغ'),
        ('antiprofane', '🤬 ضد فحش'),
        ('anticontact', '👤 قفل مخاطب'),
        ('joinrestrict', '🚷 محدودیت عضویت'),
    ]


    async def _group_time_locked(self, chat_id: int) -> bool:
        """True if manual or schedule lock should silence non-admins right now."""
        now = time.time()
        cached = self._time_lock_cache.get(chat_id)
        if cached and cached[1] > now:
            return cached[0]
        locked = False
        try:
            gs = await self.db.get_group_settings(chat_id)
            mod = dict(gs.get('moderation') or {})
            if mod.get('manual_group_lock'):
                locked = True
            else:
                # Live evaluate schedules (not only stale is_active_now flag)
                try:
                    locked = await self._desired_schedule_lock(chat_id)
                except Exception:
                    locked = bool(mod.get('schedule_group_lock')) or await self.db.any_schedule_active_for_chat(chat_id)
        except Exception:
            log.debug('group_time_locked check failed chat=%s', chat_id, exc_info=True)
            locked = False
        self._time_lock_cache[chat_id] = (locked, now + 3.0)  # short TTL
        return locked

    def _invalidate_time_lock_cache(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._time_lock_cache.clear()
        else:
            self._time_lock_cache.pop(int(chat_id), None)

    async def _active_schedule_windows_text(self, chat_id: int) -> str:
        """Human text of currently active enabled schedules for announcements."""
        try:
            rows = await self.db.list_lock_schedules(chat_id)
        except Exception:
            return ''
        parts = []
        for r in rows:
            if not r.get('enabled'):
                continue
            if not r.get('is_active_now'):
                continue
            start = r.get('start_hm') or '?'
            end = r.get('end_hm') or '?'
            parts.append(f'{start} تا {end}')
        if not parts:
            # fallback: any enabled schedule description
            for r in rows:
                if r.get('enabled'):
                    start = r.get('start_hm') or '?'
                    end = r.get('end_hm') or '?'
                    parts.append(f'{start} تا {end}')
                    break
        return '، '.join(parts) if parts else 'بازه تعیین‌شده'

    async def _enforce_group_time_lock(
        self, chat_id: int, uid: int, msg: dict,
    ) -> bool:
        """If group is time-locked, delete non-admin messages immediately. Returns True if blocked."""
        if not uid or uid == OWNER_ID:
            return False
        try:
            locked = await self._group_time_locked(chat_id)
        except Exception:
            return False
        if not locked:
            return False
        try:
            if await self._has_group_mod_power(chat_id, uid):
                return False
        except Exception:
            # If check fails, still enforce lock for safety of members
            pass

        mid = msg.get('message_id')
        deleted = False
        if mid:
            try:
                await self.api.delete_message(chat_id, int(mid))
                deleted = True
            except Exception as exc:
                log.info(
                    'time-lock delete failed chat=%s mid=%s: %s',
                    chat_id, mid, type(exc).__name__,
                )
        # Optional soft notice to user — rate limited so chat is not flooded
        now = time.time()
        notice_key = f'tl_notice:{chat_id}'
        last = float(self.game_cache.get(notice_key, {}).get('ts', 0) if isinstance(self.game_cache.get(notice_key), dict) else 0)
        if now - last >= 45:
            self.game_cache[notice_key] = {'ts': now}
            try:
                window = await self._active_schedule_windows_text(chat_id)
                await self.safe_send(
                    chat_id,
                    f'🔒 گروه در بازه قفل زمانی است ({window}).\n'
                    f'فقط مدیران می‌توانند پیام بفرستند؛ پیام اعضای عادی حذف می‌شود.',
                    parse_mode=None,
                )
            except Exception:
                pass
        log.info('time-lock blocked msg chat=%s uid=%s deleted=%s', chat_id, uid, deleted)
        return True

    async def _announce_schedule_lock(self, chat_id: int, locking: bool) -> None:
        """Announce when schedule lock engages or ends (not on every scheduler tick)."""
        now = time.time()
        # prevent double announce within 20s
        last = self._lock_announce_cooldown.get(chat_id, 0)
        if now - last < 20:
            return
        self._lock_announce_cooldown[chat_id] = now
        try:
            if locking:
                window = await self._active_schedule_windows_text(chat_id)
                text = (
                    f'🔒 گروه توسط مدیریت قفل شد.\n'
                    f'در بازه زمانی {window} هیچ‌کس (به‌جز مدیران) حق ارسال پیام ندارد.\n'
                    f'پیام‌های اعضا در این مدت حذف می‌شوند.'
                )
            else:
                text = (
                    '🔓 قفل زمانی گروه پایان یافت.\n'
                    'اعضا دوباره می‌توانند پیام ارسال کنند.'
                )
            await self.safe_send(chat_id, text, parse_mode=None)
        except Exception:
            log.exception('schedule lock announce failed chat=%s', chat_id)

    async def _apply_security_filters(self, chat_id: int, uid: int, msg: dict, text: str) -> bool:
        """Return True if message was blocked (caller should stop processing).

        At most one warn is recorded per message even if multiple rules match.
        """
        gs = await self.db.get_group_settings(chat_id)
        sec = gs.get('security') or {}
        mod = gs.get('moderation') or {}
        mid = msg.get('message_id')
        reason = None

        # Media / content locks
        if sec.get('antiphoto') and msg.get('photo'):
            reason = 'عکس'
        elif sec.get('antivideo') and msg.get('video'):
            reason = 'ویدئو'
        elif sec.get('antivoice') and msg.get('voice'):
            reason = 'ویس'
        elif sec.get('antiaudio') and msg.get('audio'):
            reason = 'فایل صوتی'
        elif sec.get('antigif') and msg.get('animation'):
            reason = 'گیف'
        elif sec.get('antisticker') and msg.get('sticker'):
            reason = 'استیکر'
        elif sec.get('antidoc') and msg.get('document') and not msg.get('animation'):
            reason = 'فایل'
        elif sec.get('anticontact') and msg.get('contact'):
            reason = 'مخاطب'
        elif sec.get('antipoll') and msg.get('poll'):
            reason = 'نظرسنجی'
        elif sec.get('antiforward') and (msg.get('forward_from') or msg.get('forward_from_chat') or msg.get('forward_date') or msg.get('forward_origin')):
            reason = 'فوروارد'

        body = text or (msg.get('caption') or '')
        entities = msg.get('entities') or msg.get('caption_entities') or []

        if not reason and sec.get('antilink'):
            if self._message_has_link(body, entities):
                reason = 'لینک'
        if not reason and sec.get('antimention'):
            mention_limit = int(mod.get('mention_limit') or 5)
            if self._count_mentions(body, entities) >= mention_limit:
                reason = 'منشن انبوه'
        if not reason and sec.get('antiad'):
            if re.search(r'(تبلیغ|کانال ما|عضویت اجباری|فالو کن|follow|subscribe)', body, re.I):
                reason = 'تبلیغ'
        if not reason and sec.get('antiprofane'):
            extra = await self.db.get_profanity_words(chat_id)
            if contains_profanity(body, extra):
                reason = 'فحش'
        if not reason and sec.get('antilong'):
            limit_chars = int(mod.get('long_msg_chars') or 800)
            if body and len(body) > limit_chars:
                reason = 'پیام بسیار طولانی'
        if not reason and sec.get('antiflood'):
            window = float(mod.get('flood_window_sec') or 8)
            limit = int(mod.get('flood_count') or 6)
            key = (chat_id, uid)
            now = time.time()
            bucket = self._FLOOD_WINDOW.setdefault(key, [])
            bucket.append(now)
            self._FLOOD_WINDOW[key] = [t for t in bucket if now - t < window]
            if len(self._FLOOD_WINDOW[key]) >= limit:
                reason = 'ارسال سریع (Flood)'
        if not reason and sec.get('antirepeat') and body:
            rk = f'repeat:{chat_id}:{uid}'
            prev = self.game_cache.get(rk)
            if prev and prev.get('text') == body and (time.time() - prev.get('ts', 0)) < 30:
                reason = 'پیام تکراری'
            self.game_cache[rk] = {'text': body, 'ts': time.time()}
        if not reason and sec.get('antispam') and body:
            # Heavy spam heuristics (not ordinary chat)
            if len(body) > 1200 or body.count('\n') > 20:
                reason = 'اسپم'
            elif len(set(body)) <= 3 and len(body) >= 30:
                reason = 'اسپم کاراکتر تکراری'
            elif re.search(r'(.)\1{12,}', body):
                reason = 'اسپم کاراکتر تکراری'

        if not reason:
            return False

        # Delete first
        deleted = False
        if mid:
            try:
                await self.api.delete_message(chat_id, int(mid))
                deleted = True
                try:
                    await self.db.record_bot_delete(chat_id)
                except Exception:
                    pass
            except Exception as exc:
                log.info('security delete failed chat=%s: %s', chat_id, type(exc).__name__)

        # Expire old warns if TTL configured
        ttl_h = int(mod.get('warn_ttl_hours') or 0)
        if ttl_h > 0:
            try:
                await self.db.expire_warns(chat_id, ttl_h)
            except Exception:
                pass

        warns = await self.db.add_warn(chat_id, uid, reason=reason, message_id=int(mid) if mid else None)
        limit = int(mod.get('warn_limit') or gs.get('warn_limit') or 5)

        if deleted:
            note = f'⚠️ پیام متخلف حذف شد و یک اخطار ثبت شد.\nعلت: «{reason}»\nاخطار: {warns}/{limit}'
        else:
            note = (
                f'❌ ربات اجازه حذف پیام را ندارد. لطفاً دسترسی‌های مدیریتی ربات را بررسی کنید.\n'
                f'علت شناسایی‌شده: «{reason}»\nاخطار: {warns}/{limit}'
            )
        await self.safe_send(chat_id, note, parse_mode=None)
        try:
            await self.db.log(0, 'security_action', 'user', uid, {
                'chat_id': chat_id, 'reason': reason, 'deleted': deleted, 'warns': warns,
            })
        except Exception:
            pass

        if warns >= limit and mod.get('auto_punish', True):
            await self._execute_auto_punish(chat_id, uid, warns, mod)
        return True

    @staticmethod
    def _message_has_link(body: str, entities: list | None) -> bool:
        if entities:
            for e in entities:
                t = (e.get('type') or '').lower()
                if t in ('url', 'text_link'):
                    return True
        if not body:
            return False
        # Real URL patterns — avoid treating "file.txt" as link
        if re.search(r'(https?://|www\.)\S+', body, re.I):
            return True
        if re.search(r'(?:^|[\s(])(?:ble\.ir|t\.me|telegram\.me)/\S+', body, re.I):
            return True
        return False

    @staticmethod
    def _count_mentions(body: str, entities: list | None) -> int:
        n = 0
        if entities:
            for e in entities:
                t = (e.get('type') or '').lower()
                if t in ('mention', 'text_mention'):
                    n += 1
        if body:
            n = max(n, len(re.findall(r'(?<!\w)@[A-Za-z0-9_]{3,}', body)))
        return n

    async def _execute_auto_punish(self, chat_id: int, uid: int, warns: int, mod: dict) -> None:
        """Apply configured punishment once when warn threshold is reached."""
        ptype = (mod.get('punish_type') or 'mute').lower()
        duration = int(mod.get('punish_duration_sec') or 3600)
        success = False
        detail = ''
        try:
            if ptype == 'warn_only':
                await self.safe_send(chat_id, '🚨 تعداد اخطارهای کاربر به حد تعیین‌شده رسید.', parse_mode=None)
                success = True
                detail = 'warn_only'
            elif ptype == 'mute':
                until = int(time.time()) + max(60, duration) if duration > 0 else None
                perms = {
                    'can_send_messages': False,
                    'can_send_media_messages': False,
                    'can_send_other_messages': False,
                    'can_add_web_page_previews': False,
                }
                await self.api.restrict_chat_member(chat_id, uid, perms, until_date=until)
                mins = max(1, duration // 60)
                await self.safe_send(
                    chat_id,
                    f'🚨 تعداد اخطارها به حد رسید.\n🤐 کاربر طبق تنظیمات گروه برای حدود {mins} دقیقه سکوت شد.',
                    parse_mode=None,
                )
                success = True
            elif ptype == 'kick':
                # Temporary ban then unban = kick (can rejoin)
                await self.api.ban_chat_member(chat_id, uid, int(time.time()) + 45)
                await self.api.unban_chat_member(chat_id, uid)
                await self.safe_send(chat_id, '🚨 تعداد اخطارها به حد رسید.\n👢 کاربر طبق تنظیمات گروه از گروه اخراج شد.', parse_mode=None)
                success = True
            elif ptype == 'ban':
                await self.api.ban_chat_member(chat_id, uid)
                await self.safe_send(chat_id, '🚨 تعداد اخطارها به حد رسید.\n⛔ کاربر طبق تنظیمات گروه مسدود شد.', parse_mode=None)
                success = True
            else:
                detail = f'unknown type {ptype}'
        except Exception as exc:
            detail = type(exc).__name__
            log.info('auto_punish failed chat=%s uid=%s type=%s: %s', chat_id, uid, ptype, detail)
            await self.safe_send(
                chat_id,
                '❌ اجرای مجازات ممکن نشد؛ تنظیمات و دسترسی‌های ربات را بررسی کنید.',
                parse_mode=None,
            )
        try:
            await self.db.log_punish(chat_id, uid, ptype, duration if ptype == 'mute' else None, warns, success, detail)
        except Exception:
            pass
        if success and mod.get('reset_warns_on_punish', True) and ptype != 'warn_only':
            try:
                await self.db.clear_warns(chat_id, uid)
            except Exception:
                pass

    async def _handle_fun(self, chat_id: int, kind: str, msg: dict) -> None:
        import hashlib
        pool_map = {
            'چالش': fun_content.CHALLENGES,
            'فکت': fun_content.FACTS,
            'حقیقت': fun_content.TRUTHS,
            'جرئت': fun_content.DARES,
        }
        pool = pool_map.get(kind) or fun_content.CHALLENGES
        recent_hashes = await self.db.recent_fun_hashes(chat_id, kind)
        candidates = [x for x in pool if hashlib.sha256(x.encode()).hexdigest()[:24] not in recent_hashes]
        if not candidates:
            candidates = list(pool)
        item = random.choice(candidates)
        await self.db.push_recent_fun(chat_id, kind, item)
        prefix = {'چالش': '🎯 *چالش*', 'فکت': '', 'حقیقت': '🤔 *حقیقت*', 'جرئت': '🔥 *جرئت*'}
        body = item if kind == 'فکت' else f"{prefix.get(kind, '')}\n{item}"
        kb = None
        if kind in ('حقیقت', 'جرئت'):
            kb = inline([[('🎲 یکی دیگه', f'fun:again:{kind}'), ('⏭ رد کن', 'fun:skip')]])
        await self.safe_send(chat_id, body, kb, reply_to=msg.get('message_id'))


    async def _fetch_user_avatars(self, user_ids: list[int], limit: int = 5) -> dict:
        """Load avatars for dashboard cards.

        Priority:
        1) disk cache
        2) Telegram getUserProfilePhotos + getFile (when platform supports it)
        3) free ui-avatars.com generated portrait (always works, no API key)
        """
        from io import BytesIO
        from pathlib import Path
        from PIL import Image
        import urllib.parse

        cache_dir = Path('/tmp/mega_avatars')
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            cache_dir = Path('/tmp')

        # names for generated avatars
        name_map: dict[int, str] = {}
        # caller may pass only ids; names filled later if present in stats — optional
        out: dict = {}

        async def download_url(url: str, timeout: float = 10.0) -> bytes | None:
            try:
                session = await self.api._ensure_session()
                import aiohttp
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout, connect=6)) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.read()
                    return data if data and len(data) > 100 else None
            except Exception:
                return None

        async def from_telegram(uid: int) -> Image.Image | None:
            try:
                res = await asyncio.wait_for(
                    self.api.call('getUserProfilePhotos', {'user_id': uid, 'limit': 1, 'offset': 0}, retries=0),
                    timeout=5.0,
                )
            except Exception as exc:
                log.debug('getUserProfilePhotos uid=%s %s', uid, type(exc).__name__)
                return None
            photos = None
            if isinstance(res, dict):
                photos = res.get('photos')
                if photos is None and isinstance(res.get('result'), dict):
                    photos = res['result'].get('photos')
            if not photos:
                return None
            first = photos[0]
            sizes = first if isinstance(first, list) else [first]
            file_id = None
            best_area = -1
            for s in sizes:
                if not isinstance(s, dict) or not s.get('file_id'):
                    continue
                area = int(s.get('width') or 0) * int(s.get('height') or 0)
                if area >= best_area:
                    best_area = area
                    file_id = s['file_id']
            if not file_id:
                return None
            try:
                data, _ = await asyncio.wait_for(self.api.get_file_bytes(str(file_id), max_bytes=2_000_000), timeout=12.0)
                return Image.open(BytesIO(data)).convert('RGBA')
            except Exception as exc:
                log.debug('profile file uid=%s %s', uid, type(exc).__name__)
                return None

        async def from_ui_avatars(uid: int, name: str) -> Image.Image | None:
            """Free generated avatar — looks like a profile picture with initial."""
            label = (name or str(uid) or 'U')[:20]
            # color from uid for stability
            colors = ['7c3aed', '06b6d4', 'ec4899', 'f59e0b', '10b981', '3b82f6', '8b5cf6']
            bg = colors[abs(uid) % len(colors)]
            q = urllib.parse.urlencode({
                'name': label,
                'background': bg,
                'color': 'fff',
                'size': '128',
                'bold': 'true',
                'format': 'png',
                'length': '2',
            })
            url = f'https://ui-avatars.com/api/?{q}'
            data = await download_url(url)
            if not data:
                # dicebear fallback
                seed = urllib.parse.quote(label)
                data = await download_url(f'https://api.dicebear.com/7.x/initials/png?seed={seed}&size=128')
            if not data:
                return None
            try:
                return Image.open(BytesIO(data)).convert('RGBA')
            except Exception:
                return None

        async def one(uid: int, name: str = ''):
            uid = int(uid)
            cache_path = cache_dir / f'{uid}.png'
            try:
                if cache_path.is_file() and cache_path.stat().st_size > 200:
                    out[uid] = Image.open(cache_path).convert('RGBA')
                    return
            except Exception:
                pass

            img = await from_telegram(uid)
            source = 'telegram'
            if img is None:
                img = await from_ui_avatars(uid, name or str(uid))
                source = 'ui-avatars'
            if img is None:
                return
            try:
                img.save(cache_path, format='PNG')
            except Exception:
                pass
            out[uid] = img
            log.info('avatar uid=%s source=%s', uid, source)

        # user_ids may be list[int] or we accept parallel names via optional second approach
        tasks = []
        for item in user_ids[:limit]:
            if isinstance(item, dict):
                tasks.append(one(int(item['user_id']), str(item.get('first_name') or item.get('username') or '')))
            else:
                tasks.append(one(int(item), name_map.get(int(item), '')))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return out


    async def _cache_sender_avatar(self, user: dict) -> None:
        """Opportunistically cache profile photo when a user is active (rate-limited)."""
        if not user or not user.get('id'):
            return
        uid = int(user['id'])
        # simple in-memory throttle: once per process per user session window
        if not hasattr(self, '_avatar_tried'):
            self._avatar_tried: set[int] = set()
        if uid in self._avatar_tried:
            return
        self._avatar_tried.add(uid)
        if len(self._avatar_tried) > 5000:
            self._avatar_tried = set(list(self._avatar_tried)[-2000:])
        try:
            await self._fetch_user_avatars([uid], limit=1)
        except Exception:
            pass


    async def _send_group_stats(self, chat_id: int, chat: dict) -> None:
        """Professional image dashboard for the group (replaces text-only stats)."""
        title = chat.get('title') or 'گروه'
        try:
            stats = await self.db.group_stats_24h(chat_id)
        except Exception:
            log.exception('group_stats_24h failed')
            try:
                stats = await self.db.group_stats_today(chat_id)
                # adapt legacy shape
                stats = {
                    'counts': stats.get('counts') or {},
                    'hourly': [],
                    'top_users': stats.get('top_users') or [],
                    'active_users': len(stats.get('top_users') or []),
                    'total_msgs': sum(int(u.get('msg_count') or 0) for u in (stats.get('top_users') or [])),
                    'window': 'today',
                }
            except Exception:
                await self.safe_send(chat_id, '❌ خواندن آمار گروه ممکن نشد.', parse_mode=None)
                return

        members_count = None
        try:
            cnt = await asyncio.wait_for(
                self.api.call('getChatMembersCount', {'chat_id': chat_id}, retries=0),
                timeout=5.0,
            )
            if isinstance(cnt, int):
                members_count = cnt
            elif isinstance(cnt, dict):
                members_count = cnt.get('result', cnt) if not isinstance(cnt.get('result'), dict) else None
                if isinstance(members_count, dict):
                    members_count = None
        except Exception:
            pass

        avatars = {}
        try:
            tops = [u for u in (stats.get('top_users') or []) if u.get('user_id')]
            if tops:
                avatars = await self._fetch_user_avatars(tops)
        except Exception:
            log.debug('avatar fetch skipped', exc_info=True)

        try:
            png = await asyncio.to_thread(
                dash.render_group_dashboard,
                title=title,
                stats=stats,
                members_count=members_count if isinstance(members_count, int) else None,
                avatars=avatars or None,
            )
            photo_res = await self.api.send_photo_bytes(
                chat_id,
                png,
                filename='group-stats.png',
                caption=f'📊 داشبورد تحلیلی — {title}',
            )
            self._maybe_ephemeral_result(chat_id, photo_res if isinstance(photo_res, dict) else None, permanent=False)
        except Exception as exc:
            log.exception('stats dashboard render/send failed')
            # Fallback minimal text so command never dies silently
            c = stats.get('counts') or {}
            await self.safe_send(
                chat_id,
                f"📊 آمار گروه (پشتیبان متنی)\n"
                f"پیام‌ها: {stats.get('total_msgs', 0)} | فعال: {stats.get('active_users', 0)}\n"
                f"عکس: {c.get('photo_count', 0)} | ویدیو: {c.get('video_count', 0)} | ویس: {c.get('voice_count', 0)}\n"
                f"❌ تصویر: {self._short(exc)}",
                parse_mode=None,
            )


    async def _show_group_panel(self, chat_id: int, uid: int) -> None:
        gs = await self.db.get_group_settings(chat_id)
        wel = '🟢' if gs.get('welcome_enabled', True) else '🔴'
        await self.safe_send(
            chat_id,
            f"*⚙️ تنظیمات گروه*\nخوشامدگویی: {wel}\n"
            f"متن سفارشی: {'دارد' if gs.get('welcome_text') else 'پیش‌فرض'}\n"
            f"سقف اخطار: {gs.get('warn_limit', 3)}",
            inline([
                [('🔐 قفل‌ها و ضداسپم', 'lock:panel'), ('⭐ لیست ویژه‌ها', 'grp:vips:0')],
                [('👋 پنل خوشامد', 'wel:panel'), ('🚪 پنل خداحافظی', 'bye:panel')],
                [('🧠 لیست یادگیری', 'learn:list:0'), ('✖️ بستن', 'close')],
            ]),
        )

    async def _show_vip_list(self, chat_id: int, page: int) -> None:
        page = max(0, page)
        total = await self.db.count_vips(chat_id)
        rows_data = await self.db.list_vips(chat_id, limit=5, offset=page * 5)
        lines = [f'⭐ *لیست ویژه‌ها* ({total} نفر)\n']
        buttons = []
        for v in rows_data:
            name = v.get('first_name') or v.get('username') or str(v['user_id'])
            lines.append(f"• {name} (`{v['user_id']}`)")
            buttons.append([(f'🗑 حذف {str(name)[:12]}', f'grp:vipdel:{v["user_id"]}')])
        if page > 0:
            buttons.append([('⬅️ قبلی', f'grp:vips:{page-1}')])
        if (page + 1) * 5 < total:
            buttons.append([('بعدی ➡️', f'grp:vips:{page+1}')])
        buttons.append([('✖️ بستن', 'close')])
        await self.safe_send(chat_id, '\n'.join(lines) if rows_data else 'هنوز ویژه‌ای ثبت نشده.', inline(buttons))

    async def _show_learned_list(
        self,
        chat_id: int,
        scope_type: str,
        scope_id: int,
        page: int,
        search: str | None = None,
        requester_uid: int | None = None,
    ) -> None:
        page = max(0, page)
        total = await self.db.count_learned(scope_type, scope_id, search)
        rows = await self.db.list_learned(scope_type, scope_id, limit=5, offset=page * 5, search=search)
        label = 'گروه' if scope_type == 'group' else 'خصوصی'
        lines = [f'🧠 *لیست یادگیری ({label})* — کل: {total}\n']
        buttons = []
        for r in rows:
            tr = (r.get('trigger_raw') or '')[:40]
            resp = (r.get('response') or '')[:40]
            lines.append(f'• «{tr}» → «{resp}»')
            norm = self._normalize_trigger(tr)[:60]
            buttons.append([(f'🗑 {tr[:16]}', f'learn:del:{scope_type}:{scope_id}:{norm}')])
        nav = []
        if page > 0:
            nav.append(('⬅️ قبلی', f'learn:list:{page-1}'))
        if (page + 1) * 5 < total:
            nav.append(('بعدی ➡️', f'learn:list:{page+1}'))
        if nav:
            buttons.append(nav)
        # Wipe-all only for group memory (admin-only UI; callback re-checks permission)
        if scope_type == 'group':
            buttons.append([('🗑️ حذف تمام کلمات یادگیری گروه', f'learn:wipe_ask:{scope_id}')])
        buttons.append([('✖️ بستن', 'close')])
        await self.safe_send(chat_id, '\n'.join(lines) if rows else f'حافظه‌ی {label} خالی است.', inline(buttons))


    async def _handle_group_callbacks(self, uid: int, chat_id: int, message_id: int, data: str, chat: dict) -> None:
        is_group = self._is_group_chat(chat)
        if data.startswith('fun:'):
            if data == 'fun:skip':
                await self.safe_edit(chat_id, message_id, 'رد شد 🌱', None)
                return
            if data.startswith('fun:again:'):
                kind = data.split(':', 2)[2]
                fake_msg = {'message_id': message_id}
                await self._handle_fun(chat_id, kind, fake_msg)
                return
            return
        if data.startswith('learn:list:'):
            try:
                page = int(data.rsplit(':', 1)[1])
            except ValueError:
                page = 0
            if is_group and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', parse_mode=None)
                return
            scope_type, scope_id = ('group', chat_id) if is_group else ('private', uid)
            await self._show_learned_list(chat_id, scope_type, scope_id, page, requester_uid=uid)
            return
        if data.startswith('learn:wipe_ask:'):
            # learn:wipe_ask:<scope_id> — only valid for current group chat
            try:
                asked_scope = int(data.rsplit(':', 1)[1])
            except ValueError:
                await self.safe_send(chat_id, '❌ داده نامعتبر.', parse_mode=None)
                return
            if not is_group or asked_scope != chat_id:
                await self.safe_send(chat_id, '⛔ فقط حافظه همین گروه.', parse_mode=None)
                return
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', parse_mode=None)
                return
            await self.safe_edit(
                chat_id,
                message_id,
                '⚠️ مطمئنی می‌خوای تمام کلمات و پاسخ‌های یادگرفته‌شده این گروه حذف بشن؟ این کار قابل برگشت نیست.',
                inline([
                    [('✅ بله، همه رو حذف کن', f'learn:wipe_yes:{chat_id}'), ('❌ انصراف', 'learn:wipe_no')],
                ]),
            )
            return
        if data == 'learn:wipe_no':
            await self.safe_edit(chat_id, message_id, '❌ عملیات حذف لغو شد.', None)
            return
        if data.startswith('learn:wipe_yes:'):
            try:
                target_scope = int(data.rsplit(':', 1)[1])
            except ValueError:
                await self.safe_send(chat_id, '❌ داده نامعتبر.', parse_mode=None)
                return
            if not is_group or target_scope != chat_id:
                await self.safe_send(chat_id, '⛔ فقط حافظه همین گروه.', parse_mode=None)
                return
            # Re-check admin on confirm (security)
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', parse_mode=None)
                return
            try:
                deleted = await self.db.clear_learned('group', chat_id)
            except Exception:
                log.exception('clear_learned failed chat=%s', chat_id)
                await self.safe_send(chat_id, '❌ انجام نشد به دلیل: ذخیره اطلاعات در دیتابیس ناموفق بود.', parse_mode=None)
                return
            if deleted == 0:
                await self.safe_edit(chat_id, message_id, 'ℹ️ حافظه یادگیری این گروه از قبل خالی بود.', None)
            else:
                await self.safe_edit(chat_id, message_id, '✅ تمام کلمات و پاسخ‌های یادگیری این گروه پاک شدند. 🧹🧠', None)
            return
        if data.startswith('learn:del:'):
            # learn:del:scope_type:scope_id:trigger_norm
            parts = data.split(':', 4)
            if len(parts) < 5:
                await self.safe_send(chat_id, 'داده نامعتبر.', parse_mode=None)
                return
            _, _, scope_type, scope_id_s, trigger_norm = parts
            # Only allow deleting own private scope or group scope of this chat
            if scope_type == 'private' and int(scope_id_s) != uid:
                await self.safe_send(chat_id, '⛔ فقط حافظه خودت.', parse_mode=None)
                return
            if scope_type == 'group' and int(scope_id_s) != chat_id:
                await self.safe_send(chat_id, '⛔ فقط حافظه همین گروه.', parse_mode=None)
                return
            if scope_type == 'group' and not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ این قابلیت فقط برای مدیران گروه فعاله!', parse_mode=None)
                return
            ok = await self.db.delete_learned(scope_type, int(scope_id_s), trigger_norm)
            await self.safe_send(chat_id, '✅ انجام شد! 🗑 حذف شد.' if ok else '🔎 پیام موردنظر پیدا نشد یا امکان حذفش وجود نداره.', parse_mode=None)
            return
        # grp:* requires group admin
        if not is_group:
            await self.safe_send(chat_id, 'این بخش فقط در گروه است.', parse_mode=None)
            return
        if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
            await self.safe_send(chat_id, '⛔ فقط ادمین گروه.', parse_mode=None)
            return
        if data == 'grp:panel':
            await self._show_group_panel(chat_id, uid)
            return
        if data == 'grp:sec' or data == 'lock:panel':
            await self._show_locks_panel(chat_id, message_id=message_id)
            return
        if data.startswith('grp:sectog:') or data.startswith('lock:tog:'):
            key = data.rsplit(':', 1)[-1]
            if key not in self.db.DEFAULT_SECURITY:
                await self.safe_send(chat_id, '❌ قفل نامعتبر.', parse_mode=None)
                return
            new_state = await self.db.toggle_security(chat_id, key)
            # Turning off join restriction clears active window
            if key == 'joinrestrict' and not new_state:
                await self.db.update_moderation(chat_id, join_window_start=0, join_window_count=0)
            label = next((t for k, t in self.LOCK_LABELS if k == key), key)
            mark = 'فعال شد ✅' if new_state else 'غیرفعال شد 🔓'
            await self._show_locks_panel(
                chat_id,
                message_id=message_id,
                header=f'🔐 پنل قفل‌های گروه\n{label}: {mark}',
            )
            return
        if data == 'lock:joincfg':
            await self._show_join_restrict_config(chat_id, message_id)
            return
        if data == 'lock:sch' or data.startswith('sch:'):
            await self._handle_schedule_callback(uid, chat_id, message_id, data)
            return
        if data.startswith('lock:joinmin:'):
            try:
                m = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            if m < 1 or m > 24 * 60:
                await self.safe_send(chat_id, '❌ مدت باید بین ۱ تا ۱۴۴۰ دقیقه باشد.', parse_mode=None)
                return
            await self.db.update_moderation(chat_id, join_restrict_minutes=m)
            await self._show_join_restrict_config(chat_id, message_id)
            return
        if data.startswith('lock:joinslots:'):
            try:
                n = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            if n < 1 or n > 50:
                await self.safe_send(chat_id, '❌ تعداد ورود آزاد باید بین ۱ تا ۵۰ باشد.', parse_mode=None)
                return
            await self.db.update_moderation(chat_id, join_restrict_free_slots=n)
            await self._show_join_restrict_config(chat_id, message_id)
            return
        if data.startswith('lock:joinact:'):
            act = data.rsplit(':', 1)[1]
            if act not in ('kick', 'warn'):
                return
            await self.db.update_moderation(chat_id, join_restrict_action=act)
            await self._show_join_restrict_config(chat_id, message_id)
            return
        if data == 'lock:joinclear':
            await self.db.update_moderation(chat_id, join_window_start=0, join_window_count=0)
            await self.safe_send(chat_id, '✅ دوره محدودیت عضویت فعلی لغو شد.', parse_mode=None)
            await self._show_join_restrict_config(chat_id, message_id)
            return
        if data == 'lock:refresh':
            await self._show_locks_panel(chat_id, message_id=message_id, header='🔐 پنل قفل‌های گروه (به‌روز شد)')
            return
        if data == 'lock:warncfg':
            await self._show_warn_config(chat_id, message_id)
            return
        if data == 'lock:punishcfg':
            await self._show_punish_config(chat_id, message_id)
            return
        if data == 'lock:stats':
            await self._show_lock_stats(chat_id, message_id)
            return
        if data == 'lock:warns':
            await self._show_warned_users(chat_id, message_id, 0)
            return
        if data.startswith('lock:warns:'):
            try:
                page = int(data.rsplit(':', 1)[1])
            except ValueError:
                page = 0
            await self._show_warned_users(chat_id, message_id, page)
            return
        if data.startswith('lock:clearwarn:'):
            try:
                tid = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            await self.db.clear_warns(chat_id, tid)
            await self.safe_send(chat_id, f'✅ اخطارهای کاربر `{tid}` صفر شد.', parse_mode=None)
            await self._show_warned_users(chat_id, message_id, 0)
            return
        if data.startswith('lock:setlimit:'):
            try:
                n = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            if n < 1 or n > 50:
                await self.safe_send(chat_id, '❌ عدد اخطار باید بین ۱ تا ۵۰ باشد.', parse_mode=None)
                return
            await self.db.update_moderation(chat_id, warn_limit=n)
            await self._show_warn_config(chat_id, message_id)
            return
        if data.startswith('lock:setttl:'):
            try:
                h = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            if h < 0 or h > 8760:
                await self.safe_send(chat_id, '❌ مدت اعتبار نامعتبر است.', parse_mode=None)
                return
            await self.db.update_moderation(chat_id, warn_ttl_hours=h)
            await self._show_warn_config(chat_id, message_id)
            return
        if data.startswith('lock:ptype:'):
            ptype = data.rsplit(':', 1)[1]
            if ptype not in ('mute', 'kick', 'ban', 'warn_only'):
                return
            await self.db.update_moderation(chat_id, punish_type=ptype)
            await self._show_punish_config(chat_id, message_id)
            return
        if data.startswith('lock:pdur:'):
            try:
                sec = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            if sec < 60 or sec > 604800:
                await self.safe_send(chat_id, '❌ مدت سکوت باید بین ۱ دقیقه تا ۷ روز باشد.', parse_mode=None)
                return
            await self.db.update_moderation(chat_id, punish_duration_sec=sec)
            await self._show_punish_config(chat_id, message_id)
            return
        if data == 'lock:toggle_auto':
            gs = await self.db.get_group_settings(chat_id)
            mod = gs.get('moderation') or {}
            await self.db.update_moderation(chat_id, auto_punish=not mod.get('auto_punish', True))
            await self._show_punish_config(chat_id, message_id)
            return
        if data == 'lock:toggle_reset':
            gs = await self.db.get_group_settings(chat_id)
            mod = gs.get('moderation') or {}
            await self.db.update_moderation(chat_id, reset_warns_on_punish=not mod.get('reset_warns_on_punish', True))
            await self._show_punish_config(chat_id, message_id)
            return
        if data == 'grp:welcome_toggle' or data == 'wel:toggle':
            gs = await self.db.get_group_settings(chat_id)
            new_v = not gs.get('welcome_enabled', True)
            await self.db.set_group_setting_fields(chat_id, welcome_enabled=new_v)
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'grp:welcome_help' or data == 'wel:help':
            await self.safe_edit(
                chat_id, message_id,
                '📝 *راهنمای متن خوشامد*\n\n'
                'دستور متن:\n`خوشامدگویی متن دلخواه شما`\n\n'
                'متغیرها:\n'
                '• `{name}` نام کاربر\n'
                '• `{mention}` منشن با uid\n'
                '• `{group}` نام گروه\n'
                '• `{uid}` شناسه عددی\n\n'
                'روشن/خاموش از پنل یا:\n`خوشامدگویی روشن` / `خوشامدگویی خاموش`\n'
                'پیش‌فرض: `خوشامدگویی پیش‌فرض`',
                inline([[('🔙 بازگشت', 'wel:panel'), ('✖️ بستن', 'close')]]),
            )
            return
        if data == 'wel:panel':
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'wel:refresh':
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'wel:view':
            gs = await self.db.get_group_settings(chat_id)
            custom = (gs.get('welcome_text') or '').strip()
            body = custom if custom else '— از پاسخ‌های متنوع پیش‌فرض استفاده می‌شود —'
            await self.safe_edit(
                chat_id, message_id,
                f'👁 *متن خوشامد فعلی:*\n\n{body}',
                inline([[('🔙 بازگشت', 'wel:panel')]]),
            )
            return
        if data == 'wel:preview':
            gs = await self.db.get_group_settings(chat_id)
            title = (chat.get('title') or 'گروه')
            sample_uid = uid
            sample_name = 'کاربر نمونه'
            mention = f'[{self._md_escape_name(sample_name)}](uid:{sample_uid})'
            use_mention = bool(gs.get('welcome_mention', True))
            custom = (gs.get('welcome_text') or '').strip()
            if custom:
                text = self._render_member_text(
                    custom, name=sample_name, uid=sample_uid, group=title, mention=mention, use_mention=use_mention,
                )
            else:
                tmpl = random.choice(self.WELCOME_TEMPLATES)
                text = self._render_member_text(
                    tmpl, name=sample_name, uid=sample_uid, group=title, mention=mention, use_mention=use_mention,
                )
            await self.safe_edit(
                chat_id, message_id,
                f'🔎 *پیش‌نمایش خوشامد:*\n\n{text}',
                inline([[('🔙 بازگشت', 'wel:panel')]]),
            )
            return
        if data == 'wel:default':
            await self.db.set_group_setting_fields(chat_id, welcome_text=None, welcome_enabled=True)
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'wel:varied':
            gs = await self.db.get_group_settings(chat_id)
            await self.db.set_group_setting_fields(chat_id, welcome_varied=not gs.get('welcome_varied', True))
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'wel:mention':
            gs = await self.db.get_group_settings(chat_id)
            await self.db.set_group_setting_fields(chat_id, welcome_mention=not gs.get('welcome_mention', True))
            await self._show_welcome_panel(chat_id, message_id)
            return
        if data == 'wel:settext':
            self.states[uid] = {'step': 'welcome_text', 'chat_id': chat_id, 'message_id': message_id}
            await self.safe_edit(
                chat_id, message_id,
                '✏️ متن خوشامد جدید را بفرست.\nمتغیرها: `{name}` `{mention}` `{group}` `{uid}`\nبرای لغو: `لغو`',
                inline([[('❌ لغو', 'wel:panel')]]),
            )
            return
        # ── Goodbye panel callbacks
        if data == 'bye:panel':
            await self._show_goodbye_panel(chat_id, message_id)
            return
        if data == 'bye:toggle':
            gs = await self.db.get_group_settings(chat_id)
            await self.db.set_group_setting_fields(chat_id, goodbye_enabled=not gs.get('goodbye_enabled', True))
            await self._show_goodbye_panel(chat_id, message_id)
            return
        if data == 'bye:varied':
            gs = await self.db.get_group_settings(chat_id)
            await self.db.set_group_setting_fields(chat_id, goodbye_varied=not gs.get('goodbye_varied', True))
            await self._show_goodbye_panel(chat_id, message_id)
            return
        if data == 'bye:default':
            await self.db.set_group_setting_fields(chat_id, goodbye_text=None, goodbye_enabled=True)
            await self._show_goodbye_panel(chat_id, message_id)
            return
        if data == 'bye:view':
            gs = await self.db.get_group_settings(chat_id)
            custom = (gs.get('goodbye_text') or '').strip()
            body = custom if custom else '— از پاسخ‌های متنوع پیش‌فرض استفاده می‌شود —'
            await self.safe_edit(
                chat_id, message_id,
                f'👁 *متن خداحافظی فعلی:*\n\n{body}',
                inline([[('🔙 بازگشت', 'bye:panel')]]),
            )
            return
        if data == 'bye:preview':
            gs = await self.db.get_group_settings(chat_id)
            title = (chat.get('title') or 'گروه')
            sample_name = 'کاربر نمونه'
            mention = f'[{self._md_escape_name(sample_name)}](uid:{uid})'
            custom = (gs.get('goodbye_text') or '').strip()
            if custom:
                text = self._render_member_text(
                    custom, name=sample_name, uid=uid, group=title, mention=mention, use_mention=True,
                )
            else:
                tmpl = random.choice(self.GOODBYE_TEMPLATES)
                text = self._render_member_text(
                    tmpl, name=sample_name, uid=uid, group=title, mention=mention, use_mention=True,
                )
            await self.safe_edit(
                chat_id, message_id,
                f'🔎 *پیش‌نمایش خداحافظی:*\n\n{text}',
                inline([[('🔙 بازگشت', 'bye:panel')]]),
            )
            return
        if data == 'bye:settext':
            self.states[uid] = {'step': 'goodbye_text', 'chat_id': chat_id, 'message_id': message_id}
            await self.safe_edit(
                chat_id, message_id,
                '✏️ متن خداحافظی جدید را بفرست.\nمتغیرها: `{name}` `{mention}` `{group}` `{uid}`\nبرای لغو: `لغو`',
                inline([[('❌ لغو', 'bye:panel')]]),
            )
            return
        if data == 'bye:help':
            await self.safe_edit(
                chat_id, message_id,
                '📝 *راهنمای خداحافظی*\n\n'
                'دستور متن:\n`خداحافظی متن دلخواه شما`\n\n'
                'متغیرها: `{name}` `{mention}` `{group}` `{uid}`\n'
                'روشن/خاموش از پنل.',
                inline([[('🔙 بازگشت', 'bye:panel')]]),
            )
            return
        if data.startswith('grp:vips:'):
            try:
                page = int(data.rsplit(':', 1)[1])
            except ValueError:
                page = 0
            await self._show_vip_list(chat_id, page)
            return
        if data.startswith('grp:vipdel:'):
            tid = int(data.rsplit(':', 1)[1])
            ok = await self.db.remove_vip(chat_id, tid)
            await self.safe_send(chat_id, 'حذف شد.' if ok else 'پیدا نشد.', parse_mode=None)
            await self._show_vip_list(chat_id, 0)
            return

    async def _security_keyboard(self, chat_id: int) -> dict:
        """Backward-compatible alias for locks panel keyboard."""
        return await self._locks_keyboard(chat_id)

    async def _locks_keyboard(self, chat_id: int) -> dict:
        gs = await self.db.get_group_settings(chat_id)
        sec = gs.get('security') or {}
        rows = []
        # pair buttons two-per-row where possible
        pair: list = []
        for key, title in self.LOCK_LABELS:
            on = bool(sec.get(key))
            mark = '🟢' if on else '🔴'
            pair.append((f'{mark} {title}', f'lock:tog:{key}'))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([
            ('⚙️ تنظیمات اخطار', 'lock:warncfg'),
            ('⚖️ تنظیم مجازات', 'lock:punishcfg'),
        ])
        rows.append([
            ('🚷 تنظیم محدودیت عضویت', 'lock:joincfg'),
            ('⏰ قفل زمان‌بندی‌شده گروه', 'lock:sch'),
        ])
        rows.append([
            ('📊 آمار تخلفات', 'lock:stats'),
            ('👥 اخطار کاربران', 'lock:warns'),
        ])
        rows.append([
            ('🔄 تازه‌سازی', 'lock:refresh'),
            ('🔙 بازگشت', 'grp:panel'),
        ])
        rows.append([('✖️ بستن', 'close')])
        return inline(rows)

    async def _show_welcome_panel(self, chat_id: int, message_id: int | None = None) -> None:
        gs = await self.db.get_group_settings(chat_id)
        on = '🟢 روشن' if gs.get('welcome_enabled', True) else '🔴 خاموش'
        varied = '🟢' if gs.get('welcome_varied', True) else '🔴'
        mention = '🟢' if gs.get('welcome_mention', True) else '🔴'
        custom = (gs.get('welcome_text') or '').strip()
        text_status = 'سفارشی' if custom else 'پیش‌فرض متنوع'
        body = (
            f'👋 *پنل خوشامدگویی*\n\n'
            f'وضعیت: *{on}*\n'
            f'متن: *{text_status}*\n'
            f'پاسخ‌های متنوع: {varied}\n'
            f'نمایش نام با منشن: {mention}\n'
        )
        if custom:
            preview = custom if len(custom) <= 120 else custom[:117] + '...'
            body += f'\nمتن فعلی:\n`{preview}`\n'
        kb = inline([
            [('🟢/🔴 روشن-خاموش', 'wel:toggle'), ('👁 مشاهده متن', 'wel:view')],
            [('✏️ تغییر متن', 'wel:settext'), ('🔎 پیش‌نمایش', 'wel:preview')],
            [(f'{varied} پاسخ متنوع', 'wel:varied'), (f'{mention} منشن نام', 'wel:mention')],
            [('♻️ متن پیش‌فرض', 'wel:default'), ('❓ راهنما', 'wel:help')],
            [('🔄 تازه‌سازی', 'wel:refresh'), ('🔙 بازگشت', 'grp:panel')],
            [('✖️ بستن', 'close')],
        ])
        if message_id:
            await self.safe_edit(chat_id, message_id, body, kb)
        else:
            await self.safe_send(chat_id, body, kb)

    async def _show_goodbye_panel(self, chat_id: int, message_id: int | None = None) -> None:
        gs = await self.db.get_group_settings(chat_id)
        on = '🟢 روشن' if gs.get('goodbye_enabled', True) else '🔴 خاموش'
        varied = '🟢' if gs.get('goodbye_varied', True) else '🔴'
        custom = (gs.get('goodbye_text') or '').strip()
        text_status = 'سفارشی' if custom else 'پیش‌فرض متنوع'
        body = (
            f'🚪 *پنل خداحافظی / بدرقه*\n\n'
            f'وضعیت: *{on}*\n'
            f'متن: *{text_status}*\n'
            f'پاسخ‌های متنوع: {varied}\n'
        )
        if custom:
            preview = custom if len(custom) <= 120 else custom[:117] + '...'
            body += f'\nمتن فعلی:\n`{preview}`\n'
        kb = inline([
            [('🟢/🔴 روشن-خاموش', 'bye:toggle'), ('👁 مشاهده متن', 'bye:view')],
            [('✏️ تغییر متن', 'bye:settext'), ('🔎 پیش‌نمایش', 'bye:preview')],
            [(f'{varied} پاسخ متنوع', 'bye:varied'), ('♻️ متن پیش‌فرض', 'bye:default')],
            [('❓ راهنما', 'bye:help'), ('🔄 تازه‌سازی', 'bye:panel')],
            [('🔙 بازگشت', 'grp:panel'), ('✖️ بستن', 'close')],
        ])
        if message_id:
            await self.safe_edit(chat_id, message_id, body, kb)
        else:
            await self.safe_send(chat_id, body, kb)

    # ── Scheduled group lock ─────────────────────────────────────────────

    async def _show_schedule_panel(self, chat_id: int, message_id: int | None = None) -> None:
        rows = await self.db.list_lock_schedules(chat_id)
        active_n = sum(1 for r in rows if r.get('enabled'))
        applying = sum(1 for r in rows if r.get('is_active_now'))
        body = (
            f'⏰ *قفل زمان‌بندی‌شده گروه*\n\n'
            f'می‌خوای گروه از چه ساعتی تا چه ساعتی قفل باشه؟\n\n'
            f'تعداد برنامه: *{len(rows)}* | فعال: *{active_n}* | در حال اعمال: *{applying}*\n'
            f'منطقه زمانی پیش‌فرض: `{schlock.DEFAULT_TZ}`\n\n'
            f'قفل یعنی اعضای عادی نتوانند پیام بفرستند؛ مدیران همچنان می‌توانند.'
        )
        kb = inline([
            [('➕ افزودن زمان‌بندی', 'sch:add'), ('📋 مشاهده زمان‌بندی‌ها', 'sch:list')],
            [('✏️ ویرایش', 'sch:editmenu'), ('🗑 حذف', 'sch:delmenu')],
            [('🔘 روشن/خاموش', 'sch:togmenu'), ('🔄 همگام‌سازی الان', 'sch:sync')],
            [('🔙 بازگشت به قفل‌ها', 'lock:panel'), ('✖️ بستن', 'close')],
        ])
        if message_id:
            await self.safe_edit(chat_id, message_id, body, kb)
        else:
            await self.safe_send(chat_id, body, kb)

    async def _handle_schedule_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
            await self.safe_send(chat_id, '⛔ این بخش فقط برای مدیران گروه فعاله!', parse_mode=None)
            return
        if data in ('lock:sch', 'sch:panel'):
            await self._show_schedule_panel(chat_id, message_id)
            return
        if data == 'sch:add':
            self.states[uid] = {'step': 'schedule_text', 'chat_id': chat_id, 'message_id': message_id}
            guide = (
                '⏰ زمان قفل گروه رو وارد کن.\n\n'
                'فرمت:\n'
                '«ساعت شروع ساعت پایان روزها»\n\n'
                'مثال:\n'
                '`17:00 21:00 هر روز`\n'
                'یعنی هر روز از ساعت ۱۷:۰۰ تا ۲۱:۰۰ گروه قفل باشه.\n\n'
                '`17:00 21:00 دو روز`\n'
                'یعنی برای دو روز آینده (امروز و فردا).\n\n'
                'مثال‌های دیگر:\n'
                '`17:00 تا 21:00 هر روز`\n'
                '`۱۷:۰۰ تا ۲۱:۰۰ دو روز`\n'
                '`17:00 21:00 شنبه، دوشنبه، چهارشنبه`\n'
                '`22:00 02:00 هر روز` (عبور از نیمه‌شب)\n\n'
                'اعداد فارسی یا انگلیسی؛ «تا» اختیاری.\n'
                'برای لغو: `لغو`'
            )
            await self.safe_edit(
                chat_id, message_id, guide,
                inline([[('❌ لغو', 'sch:panel')]]),
            )
            return
        if data == 'sch:list':
            rows = await self.db.list_lock_schedules(chat_id)
            if not rows:
                body = '📋 هنوز زمان‌بندی‌ای ثبت نشده.'
            else:
                body = '📋 *زمان‌بندی‌ها:*\n\n' + '\n'.join(schlock.format_schedule_row(r) for r in rows)
            await self.safe_edit(
                chat_id, message_id, body,
                inline([[('🔙 بازگشت', 'sch:panel')]]),
            )
            return
        if data in ('sch:editmenu', 'sch:delmenu', 'sch:togmenu'):
            rows = await self.db.list_lock_schedules(chat_id)
            if not rows:
                await self.safe_edit(chat_id, message_id, 'ℹ️ برنامه‌ای وجود ندارد.', inline([[('🔙 بازگشت', 'sch:panel')]]))
                return
            action = {'sch:editmenu': 'edit', 'sch:delmenu': 'del', 'sch:togmenu': 'tog'}[data]
            title = {'edit': '✏️ کدام را ویرایش کنم؟', 'del': '🗑 کدام را حذف کنم؟', 'tog': '🔘 روشن/خاموش کدام؟'}[action]
            buttons = []
            for r in rows[:20]:
                label = schlock.format_schedule_row(r)[:40]
                buttons.append([(label, f'sch:{action}:{r["id"]}')])
            buttons.append([('🔙 بازگشت', 'sch:panel')])
            await self.safe_edit(chat_id, message_id, title, inline(buttons))
            return
        if data.startswith('sch:del:'):
            try:
                sid = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            ok = await self.db.delete_lock_schedule(sid, chat_id)
            await self.safe_send(chat_id, '✅ حذف شد.' if ok else '❌ پیدا نشد.', parse_mode=None)
            # Recompute lock state for chat
            await self._apply_group_lock_state(chat_id, reason='schedule_deleted')
            await self._show_schedule_panel(chat_id, message_id)
            return
        if data.startswith('sch:tog:'):
            try:
                sid = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            row = await self.db.get_lock_schedule(sid)
            if not row or int(row['chat_id']) != chat_id:
                await self.safe_send(chat_id, '❌ پیدا نشد.', parse_mode=None)
                return
            new_en = not bool(row.get('enabled', True))
            await self.db.update_lock_schedule(sid, enabled=new_en, is_active_now=False if not new_en else row.get('is_active_now'))
            await self.safe_send(chat_id, '🟢 برنامه روشن شد.' if new_en else '🔴 برنامه خاموش شد.', parse_mode=None)
            await self._apply_group_lock_state(chat_id, reason='schedule_toggle')
            await self._show_schedule_panel(chat_id, message_id)
            return
        if data.startswith('sch:edit:'):
            try:
                sid = int(data.rsplit(':', 1)[1])
            except ValueError:
                return
            row = await self.db.get_lock_schedule(sid)
            if not row or int(row['chat_id']) != chat_id:
                await self.safe_send(chat_id, '❌ پیدا نشد.', parse_mode=None)
                return
            # Delete old + ask for new text (simple reliable edit)
            await self.db.delete_lock_schedule(sid, chat_id)
            self.states[uid] = {'step': 'schedule_text', 'chat_id': chat_id, 'message_id': message_id}
            await self.safe_edit(
                chat_id, message_id,
                f'✏️ برنامه #{sid} حذف شد تا جایگزین شود.\nهمان فرمت افزودن را بفرست:\n`17:00 21:00 هر روز`',
                inline([[('❌ لغو', 'sch:panel')]]),
            )
            return
        if data == 'sch:sync':
            await self._apply_group_lock_state(chat_id, reason='manual_sync')
            await self.safe_send(chat_id, '🔄 وضعیت قفل با زمان‌بندی‌ها همگام شد.', parse_mode=None)
            await self._show_schedule_panel(chat_id, message_id)
            return

    async def _desired_schedule_lock(self, chat_id: int) -> bool:
        """True if any enabled schedule says the group should be locked now."""
        rows = await self.db.list_lock_schedules(chat_id)
        now = datetime.now(ZoneInfo(schlock.DEFAULT_TZ))
        desired = False
        for r in rows:
            if not r.get('enabled', True):
                if r.get('is_active_now'):
                    await self.db.update_lock_schedule(int(r['id']), is_active_now=False)
                continue
            days = r.get('days') or []
            if isinstance(days, str):
                try:
                    days = json.loads(days)
                except Exception:
                    days = []
            vf = r.get('valid_from')
            vu = r.get('valid_until')
            should = schlock.schedule_should_lock(
                now=now,
                start_hm=str(r['start_hm']),
                end_hm=str(r['end_hm']),
                days=days,
                valid_from=vf,
                valid_until=vu,
            )
            if should != bool(r.get('is_active_now')):
                await self.db.update_lock_schedule(
                    int(r['id']),
                    is_active_now=should,
                    last_action='want_lock' if should else 'want_unlock',
                    last_action_at=datetime.now(timezone.utc),
                )
            if should:
                desired = True
        return desired

    async def _apply_group_lock_state(self, chat_id: int, reason: str = '') -> None:
        """Compute final lock from manual + schedule; call setChatPermissions once if needed."""
        gs = await self.db.get_group_settings(chat_id)
        mod = dict(gs.get('moderation') or {})
        manual = bool(mod.get('manual_group_lock'))
        try:
            sched = await self._desired_schedule_lock(chat_id)
        except Exception:
            log.exception('desired schedule lock failed chat=%s', chat_id)
            sched = bool(mod.get('schedule_group_lock'))
        want_lock = manual or sched
        prev_sched = bool(mod.get('schedule_group_lock'))
        prev_want = bool(mod.get('group_lock_effective'))
        if sched != prev_sched:
            try:
                await self.db.update_moderation(chat_id, schedule_group_lock=sched)
            except Exception:
                log.exception('update schedule_group_lock flag failed')

        # Persist effective lock for fast path + announce on edge
        try:
            await self.db.update_moderation(chat_id, group_lock_effective=want_lock)
        except Exception:
            log.debug('update group_lock_effective failed', exc_info=True)

        self._invalidate_time_lock_cache(chat_id)

        # Announce only when schedule portion transitions (not every 30s tick)
        if sched and not prev_sched:
            await self._announce_schedule_lock(chat_id, locking=True)
        elif (not sched) and prev_sched and not manual:
            await self._announce_schedule_lock(chat_id, locking=False)

        # Best-effort platform permissions (may be unsupported on Telegram)
        try:
            if want_lock:
                await self.api.set_chat_permissions(chat_id, schlock.PERMS_LOCKED)
                log.info('group LOCKED chat=%s reason=%s manual=%s sched=%s', chat_id, reason, manual, sched)
            else:
                await self.api.set_chat_permissions(chat_id, schlock.PERMS_OPEN)
                log.info('group UNLOCKED chat=%s reason=%s', chat_id, reason)
        except Exception as exc:
            log.warning('setChatPermissions failed chat=%s reason=%s: %s', chat_id, reason, type(exc).__name__)
            # Enforcement continues via message deletion path

    async def _schedule_lock_loop(self) -> None:
        """One global loop: every ~30s evaluate schedules and sync permissions."""
        await asyncio.sleep(5)
        while True:
            try:
                rows = await self.db.list_enabled_lock_schedules()
                chats = sorted({int(r['chat_id']) for r in rows})
                # Also chats that had is_active_now but schedule disabled are handled in list_lock path
                for cid in chats:
                    try:
                        await self._apply_group_lock_state(cid, reason='scheduler')
                    except Exception:
                        log.exception('schedule apply failed chat=%s', cid)
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('schedule lock loop error')
            await asyncio.sleep(15)

    async def _show_join_restrict_config(self, chat_id: int, message_id: int) -> None:
        gs = await self.db.get_group_settings(chat_id)
        sec = gs.get('security') or {}
        mod = gs.get('moderation') or {}
        enabled = bool(sec.get('joinrestrict'))
        minutes = int(mod.get('join_restrict_minutes') or 30)
        slots = int(mod.get('join_restrict_free_slots') or 1)
        action = (mod.get('join_restrict_action') or 'kick').lower()
        action_fa = 'اخراج (Kick)' if action == 'kick' else 'اخطار (+ مجازات اخطارها)'
        window_start = float(mod.get('join_window_start') or 0)
        window_count = int(mod.get('join_window_count') or 0)
        now = time.time()
        if enabled and window_start > 0 and (now - window_start) < minutes * 60:
            remain = int(minutes * 60 - (now - window_start))
            status = f'فعال — حدود {max(1, remain // 60)} دقیقه باقی‌مانده (ورودهای دوره: {window_count})'
        elif enabled:
            status = 'روشن — منتظر شروع دوره با ورود عضو'
        else:
            status = 'خاموش'
        text = (
            f'🚷 *تنظیمات محدودیت عضویت*\n\n'
            f'وضعیت: *{status}*\n'
            f'مدت دوره: *{minutes} دقیقه*\n'
            f'ورود آزاد در شروع دوره: *{slots}* نفر\n'
            f'اقدام برای ورود اضافه: *{action_fa}*\n\n'
            f'بعد از پر شدن سهمیه ورود آزاد، تا پایان مدت، ورود جدید اخراج/اخطار می‌شود.\n'
            f'پس از پایان مدت، دوره از نو شروع می‌شود.'
        )
        kb = inline([
            [('🟢/🔴 روشن-خاموش', 'lock:tog:joinrestrict')],
            [('۱د', 'lock:joinmin:1'), ('۵د', 'lock:joinmin:5'), ('۱۰د', 'lock:joinmin:10'), ('۱۵د', 'lock:joinmin:15')],
            [('۳۰د', 'lock:joinmin:30'), ('۶۰د', 'lock:joinmin:60'), ('۱۲۰د', 'lock:joinmin:120')],
            [('آزاد:۱', 'lock:joinslots:1'), ('آزاد:۲', 'lock:joinslots:2'), ('آزاد:۳', 'lock:joinslots:3'), ('آزاد:۵', 'lock:joinslots:5')],
            [('👢 اقدام: اخراج', 'lock:joinact:kick'), ('⚠️ اقدام: اخطار', 'lock:joinact:warn')],
            [('🛑 لغو دوره فعلی', 'lock:joinclear')],
            [('🔙 بازگشت به قفل‌ها', 'lock:panel'), ('✖️ بستن', 'close')],
        ])
        await self.safe_edit(chat_id, message_id, text, kb)

    async def _show_locks_panel(self, chat_id: int, message_id: int | None = None, header: str | None = None) -> None:
        gs = await self.db.get_group_settings(chat_id)
        mod = gs.get('moderation') or {}
        sec = gs.get('security') or {}
        ptype_fa = {
            'mute': 'سکوت', 'kick': 'اخراج', 'ban': 'مسدود', 'warn_only': 'فقط اخطار',
        }.get(mod.get('punish_type', 'mute'), 'سکوت')
        jr = '🟢' if sec.get('joinrestrict') else '🔴'
        text = header or '🔐 مدیریت قفل‌های گروه'
        text += (
            f"\n\nآستانه اخطار: *{mod.get('warn_limit', 5)}*"
            f"\nمجازات: *{ptype_fa}*"
            f"\nخودکار: *{'روشن' if mod.get('auto_punish', True) else 'خاموش'}*"
            f"\nمحدودیت عضویت: {jr} (*{mod.get('join_restrict_minutes', 30)}* دقیقه)"
            f"\n\nروی هر قفل بزن تا روشن/خاموش شود."
        )
        kb = await self._locks_keyboard(chat_id)
        if message_id:
            await self.safe_edit(chat_id, message_id, text, kb)
        else:
            await self.safe_send(chat_id, text, kb)

    async def _show_warn_config(self, chat_id: int, message_id: int) -> None:
        gs = await self.db.get_group_settings(chat_id)
        mod = gs.get('moderation') or {}
        ttl = int(mod.get('warn_ttl_hours') or 0)
        ttl_s = 'دائمی' if ttl <= 0 else f'{ttl} ساعت'
        text = (
            f"⚙️ *تنظیمات اخطار*\n"
            f"آستانه مجازات: *{mod.get('warn_limit', 5)}*\n"
            f"اعتبار اخطار: *{ttl_s}*\n\n"
            f"آستانه را انتخاب کن:"
        )
        kb = inline([
            [('۳', 'lock:setlimit:3'), ('۵', 'lock:setlimit:5'), ('۷', 'lock:setlimit:7'), ('۱۰', 'lock:setlimit:10')],
            [('دائمی', 'lock:setttl:0'), ('۲۴س', 'lock:setttl:24'), ('۷روز', 'lock:setttl:168')],
            [('🔙 بازگشت به قفل‌ها', 'lock:panel'), ('✖️ بستن', 'close')],
        ])
        await self.safe_edit(chat_id, message_id, text, kb)

    async def _show_punish_config(self, chat_id: int, message_id: int) -> None:
        gs = await self.db.get_group_settings(chat_id)
        mod = gs.get('moderation') or {}
        ptype = mod.get('punish_type', 'mute')
        dur = int(mod.get('punish_duration_sec') or 3600)
        auto = '🟢' if mod.get('auto_punish', True) else '🔴'
        reset = '🟢' if mod.get('reset_warns_on_punish', True) else '🔴'
        text = (
            f"⚖️ *تنظیم مجازات*\n"
            f"نوع فعلی: *{ptype}*\n"
            f"مدت سکوت: *{max(1, dur // 60)} دقیقه*\n"
            f"مجازات خودکار: {auto}\n"
            f"صفر شدن اخطار بعد مجازات: {reset}\n\n"
            f"نکته: Kick = اخراج موقت (می‌تواند دوباره عضو شود). Ban = مسدود دائمی."
        )
        kb = inline([
            [('🤐 سکوت', 'lock:ptype:mute'), ('👢 اخراج', 'lock:ptype:kick')],
            [('⛔ مسدود', 'lock:ptype:ban'), ('⚠️ فقط اخطار', 'lock:ptype:warn_only')],
            [('۱۰د', 'lock:pdur:600'), ('۳۰د', 'lock:pdur:1800'), ('۱س', 'lock:pdur:3600'), ('۲۴س', 'lock:pdur:86400')],
            [(f'{auto} مجازات خودکار', 'lock:toggle_auto'), (f'{reset} صفر اخطار', 'lock:toggle_reset')],
            [('🔙 بازگشت به قفل‌ها', 'lock:panel'), ('✖️ بستن', 'close')],
        ])
        await self.safe_edit(chat_id, message_id, text, kb)

    async def _show_lock_stats(self, chat_id: int, message_id: int) -> None:
        try:
            warned = await self.db.count_warned_users(chat_id)
            logs_n = await self.db.count_warn_logs(chat_id)
            recent = await self.db.list_warn_logs(chat_id, limit=8)
            punishes = await self.db.list_punish_logs(chat_id, limit=5)
        except Exception:
            log.exception('lock stats failed')
            await self.safe_edit(chat_id, message_id, '❌ دریافت آمار ممکن نشد.', inline([[('🔙 بازگشت', 'lock:panel')]]))
            return
        lines = [
            f'📊 *آمار تخلفات این گروه*',
            f'کاربران دارای اخطار: *{warned}*',
            f'کل اخطارهای ثبت‌شده: *{logs_n}*',
            '',
            '*آخرین تخلفات:*',
        ]
        if recent:
            for r in recent:
                lines.append(f"• `{r['user_id']}` — {r.get('reason') or '—'} — {str(r.get('created_at') or '')[:16]}")
        else:
            lines.append('موردی نیست.')
        if punishes:
            lines.append('\n*آخرین مجازات‌ها:*')
            for p in punishes:
                ok = '✅' if p.get('success') else '❌'
                lines.append(f"• {ok} `{p['user_id']}` — {p.get('punish_type')} — warns={p.get('warns_at')}")
        await self.safe_edit(
            chat_id, message_id, '\n'.join(lines),
            inline([[('👥 اخطار کاربران', 'lock:warns'), ('🔙 بازگشت', 'lock:panel')]]),
        )

    async def _show_warned_users(self, chat_id: int, message_id: int, page: int) -> None:
        page = max(0, page)
        total = await self.db.count_warned_users(chat_id)
        rows = await self.db.list_warned_users(chat_id, limit=8, offset=page * 8)
        lines = [f'👥 *کاربران دارای اخطار* ({total})\n']
        buttons = []
        for r in rows:
            name = r.get('first_name') or r.get('username') or str(r['user_id'])
            lines.append(f"• {name} — `{r['user_id']}` — اخطار: *{r['warn_count']}*")
            buttons.append([(f"🗑 صفر {str(name)[:12]}", f"lock:clearwarn:{r['user_id']}")])
        nav = []
        if page > 0:
            nav.append(('⬅️ قبلی', f'lock:warns:{page-1}'))
        if (page + 1) * 8 < total:
            nav.append(('بعدی ➡️', f'lock:warns:{page+1}'))
        if nav:
            buttons.append(nav)
        buttons.append([('🔙 بازگشت', 'lock:panel'), ('✖️ بستن', 'close')])
        body = '\n'.join(lines) if rows else 'هیچ کاربری اخطار فعال ندارد.'
        await self.safe_edit(chat_id, message_id, body, inline(buttons))

    async def handle_state(self, chat_id: int, uid: int, user: dict, text: str, state: dict, msg: dict) -> bool:
        step=state.get('step')
        if step == 'schedule_text':
            if text.strip() in ('لغو', 'cancel', 'انصراف'):
                self.states.pop(uid, None)
                await self._show_schedule_panel(chat_id, state.get('message_id'))
                return True
            try:
                parsed = schlock.parse_schedule_text(text)
            except ValueError as e:
                await self.safe_send(chat_id, str(e), parse_mode=None)
                return True
            try:
                row = await self.db.create_lock_schedule(
                    chat_id=int(state.get('chat_id') or chat_id),
                    start_hm=parsed['start_hm'],
                    end_hm=parsed['end_hm'],
                    days=parsed['days'],
                    valid_from=parsed.get('valid_from'),
                    valid_until=parsed.get('valid_until'),
                    timezone=parsed.get('timezone') or schlock.DEFAULT_TZ,
                    created_by=uid,
                )
                self.states.pop(uid, None)
                await self.safe_send(
                    chat_id,
                    f"✅ زمان‌بندی ثبت شد.\n{schlock.format_schedule_row(row)}\n"
                    f"منطقه زمانی: {schlock.DEFAULT_TZ}",
                    parse_mode=None,
                )
                await self._show_schedule_panel(chat_id, None)
            except Exception:
                log.exception('create_lock_schedule failed')
                await self.safe_send(chat_id, '❌ ذخیره زمان‌بندی ناموفق بود.', parse_mode=None)
            return True
        if step in ('welcome_text', 'goodbye_text'):
            if text.strip() in ('لغو', 'cancel', 'انصراف'):
                self.states.pop(uid, None)
                if step == 'welcome_text':
                    await self._show_welcome_panel(chat_id, state.get('message_id'))
                else:
                    await self._show_goodbye_panel(chat_id, state.get('message_id'))
                return True
            body = text.strip()
            if not body or len(body) > 1000:
                await self.safe_send(chat_id, '❌ متن خالی یا بیش از حد بلند است (حداکثر ۱۰۰۰).', parse_mode=None)
                return True
            target_chat = int(state.get('chat_id') or chat_id)
            if step == 'welcome_text':
                await self.db.set_group_setting_fields(target_chat, welcome_text=body, welcome_enabled=True)
                self.states.pop(uid, None)
                await self.safe_send(chat_id, f'✅ متن خوشامد ذخیره شد.\nپیش‌نمایش:\n{body}', parse_mode=None)
                await self._show_welcome_panel(chat_id, None)
            else:
                await self.db.set_group_setting_fields(target_chat, goodbye_text=body, goodbye_enabled=True)
                self.states.pop(uid, None)
                await self.safe_send(chat_id, f'✅ متن خداحافظی ذخیره شد.\nپیش‌نمایش:\n{body}', parse_mode=None)
                await self._show_goodbye_panel(chat_id, None)
            return True
        if step == 'ad_url':
            normalized = self._validate_tg_link(text)
            if not normalized:
                await self.safe_send(
                    chat_id,
                    'لینک نامعتبر است. فقط لینک‌های t.me/... پذیرفته می‌شوند.\nمثال: `https://t.me/channelname`',
                    inline([[('↩️ لغو', 'adm:ad'), ('✖️ بستن', 'close')]]),
                    skip_ad=True,
                )
                return True
            # Telegram API has no reliable resolveUsername for arbitrary links; inform admin.
            self.states[uid] = {
                'step': 'ad_title', 'chat_id': chat_id, 'message_id': state.get('message_id'),
                'ad_url': normalized,
            }
            await self.safe_send(
                chat_id,
                f'لینک پذیرفته شد: `{normalized}`\n\n'
                '⚠️ API بله امکان تأیید خودکار وجود مقصد را ندارد؛ صحت لینک بر عهده توست.\n\n'
                'عنوان دکمه‌ی تبلیغ رو وارد کن؛ این متن روی دکمه نمایش داده می‌شه.',
                inline([[('↩️ لغو', 'adm:ad'), ('✖️ بستن', 'close')]]),
                skip_ad=True,
            )
            return True
        if step == 'ad_title':
            title = (text or '').strip()
            if not title or len(title) > 64:
                await self.safe_send(chat_id, 'عنوان باید بین ۱ تا ۶۴ کاراکتر باشد. دوباره بفرست.', skip_ad=True)
                return True
            url = state.get('ad_url') or str(await self.db.get_setting('ad_url', '') or '')
            if not url:
                self.states.pop(uid, None)
                await self.safe_send(chat_id, 'لینک پیدا نشد؛ از ابتدا شروع کن.', inline([[('↩️ مدیریت تبلیغ', 'adm:ad')]]), skip_ad=True)
                return True
            await self.db.set_setting('ad_url', url, uid)
            await self.db.set_setting('ad_title', title, uid)
            await self.db.set_setting('ad_enabled', True, uid)
            self._invalidate_ad_cache()
            await self.db.log(uid, 'ad_configured', details={'url': url, 'title': title})
            self.states.pop(uid, None)
            preview = url_button(title, url)
            await self.safe_send(chat_id, 'تبلیغ با موفقیت فعال شد.\nپیش‌نمایش دکمه:', preview, skip_ad=True)
            await self.safe_send(chat_id, 'مدیریت بیشتر:', inline([[('📢 مدیریت تبلیغ', 'adm:ad'), ('✖️ بستن', 'close')]]), skip_ad=True)
            return True
        if step == 'broadcast_await_msg':
            payload = self._extract_broadcast_payload(msg, text)
            if not payload:
                await self.safe_send(chat_id, 'پیام قابل ارسال تشخیص داده نشد. متن، عکس، ویدئو، فایل یا GIF بفرست.', skip_ad=True)
                return True
            target = state.get('bc_target', 'users')
            if target == 'users':
                count = len(await self.db.private_user_ids())
            elif target == 'groups':
                count = len(await self.db.group_chat_ids())
            else:
                count = len(await self.db.private_user_ids()) + len(await self.db.group_chat_ids())
            label = {'users': 'کاربران خصوصی', 'groups': 'گروه‌ها', 'all': 'همه مخاطبان'}[target]
            preview_text = payload.get('text') or payload.get('caption') or f'[{payload.get("kind", "media")}]'
            self.states[uid] = {
                'step': 'broadcast_confirm',
                'chat_id': chat_id,
                'bc_target': target,
                'bc_payload': payload,
            }
            await self.safe_send(
                chat_id,
                f'*پیش‌نمایش پیام همگانی*\nهدف: {label}\nتعداد تقریبی مخاطبان: `{count}`\nنوع: `{payload.get("kind")}`\nمحتوا:\n{preview_text[:500]}',
                inline([
                    [('✅ تأیید ارسال', 'adm:bc:confirm')],
                    [('✖️ لغو', 'adm:bc:abort'), ('↩️ برگشت', 'adm:broadcast')],
                ]),
                skip_ad=True,
            )
            return True
        if step == 'search':
            results=await self.db.search_target(text,state['target_type'])
            if not results:
                await self.safe_send(chat_id,'موردی پیدا نشد. فقط کاربرها و گروه‌هایی قابل جست‌وجو هستند که ربات قبلاً آن‌ها را ثبت کرده باشد.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))
                self.states.pop(uid,None); return True
            target_type=state['target_type']
            rows=[]
            for r in results[:3]: rows.append([(f"{r.get('name') or 'بدون نام'} · {r['id']}",f"adm:target:{target_type}:{r['id']}")])
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            await self.safe_send(chat_id,'*نتایج جست‌وجو*\nمورد رو انتخاب کن:',inline(rows))
            self.states.pop(uid,None); return True
        if step == 'case_lookup':
            match=re.search(r'(\d{4})',text)
            self.states.pop(uid,None)
            if not match:
                await self.safe_send(chat_id,'کد باید ۴ رقم داشته باشد؛ مثال `#5453`'); return True
            case=await self.db.find_case(int(match.group(1)))
            if not case:
                await self.safe_send(chat_id,'این کد پیگیری در دیتابیس پیدا نشد.'); return True
            await self.safe_send(chat_id,self.case_text(case),inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'set_donation_total':
            if uid != OWNER_ID:
                self.states.pop(uid, None)
                await self.safe_send(chat_id, '⛔ فقط مالک اصلی.', parse_mode=None)
                return True
            if text.strip() in ('لغو', 'cancel', 'انصراف'):
                self.states.pop(uid, None)
                await self.safe_send(chat_id, '❌ اصلاح مبلغ لغو شد.', parse_mode=None)
                return True
            digits = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
            normalized = text.translate(digits).replace(',', '').replace('٬', '').replace(' ', '').replace('٫', '.')
            # allow integer only (strip thousand separators already)
            if '.' in normalized:
                # reject decimals for toman total
                await self.safe_send(chat_id, '❌ فقط عدد صحیح تومان بفرست؛ مثال: `120000`', parse_mode=None)
                return True
            if not normalized.isdigit():
                await self.safe_send(chat_id, '❌ ورودی نامعتبر است. فقط عدد (فارسی/انگلیسی).', parse_mode=None)
                return True
            amount = int(normalized)
            if amount < 0 or amount > 10**15:
                await self.safe_send(chat_id, '❌ مبلغ خارج از محدوده مجاز است.', parse_mode=None)
                return True
            try:
                current = await self.db.get_donations_total()
            except Exception:
                current = 0
            self.states[uid] = {
                'step': 'set_donation_total_confirm',
                'chat_id': chat_id,
                'new_amount': amount,
                'old_amount': current,
            }
            await self.safe_send(
                chat_id,
                f'⚠️ تأیید اصلاح مبلغ حمایت\n\n'
                f'مبلغ قبلی: {current:,} تومان\n'
                f'مبلغ جدید: {amount:,} تومان\n\n'
                f'این مقدار در آمار عمومی و اختصاصی جایگزین می‌شود.',
                inline([
                    [('✅ تأیید و ذخیره', 'own:don_total:yes'), ('❌ لغو', 'own:don_total:no')],
                ]),
                parse_mode=None,
            )
            return True

        if step == 'donation_amount':
            digits = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
            normalized = text.translate(digits).replace(',', '').replace('٬', '').replace(' ', '').replace('٫', '.')
            normalized = normalized.replace('.', '')
            if not normalized.isdigit() or int(normalized) < 1 or int(normalized) > 1000000000000:
                await self.safe_send(chat_id, 'مبلغ باید فقط عدد مثبت باشد؛ مثال: `2000` یا `2.000` تومان. دوباره بفرست.')
                return True
            amount = int(normalized)
            card = await self.db.get_setting('donation_card', '6219861958356742')
            card_name = await self.db.get_setting('donation_card_name', 'آرتین گلستانی')
            owner_username = str(await self.db.get_setting('donation_owner_username', 'commander04')).lstrip('@')
            instructions = await self.db.get_setting('donation_instructions', 'دونیت کاملاً اختیاری است.')
            self.states[uid] = {'step':'donation_confirm_paid','chat_id':chat_id,'amount':amount,'user':user}
            await self.safe_send(chat_id,
                f'*مبلغ انتخابی: {amount:,} تومان*\n\n{instructions}\n\n💌 *روش اول:* به پیوی مالک برو و در صورت تمایل پاکت هدیه بفرست: `@{owner_username}`\n\n💳 *روش دوم: واریز کارت‌به‌کارت*\nشماره کارت: `{card}`\nبه نام: *{card_name}*\n\nبعد از انجام حمایت، یکی از گزینه‌های زیر رو بزن 😊🌱',
                inline([[('✅ واریز کردم','donate:paid'),('↩️ پشیمان شدم / اتمام','donate:cancel')]]))
            return True
        if step == 'edit_donation_setting':
            key = state.get('setting_key')
            if not text.strip():
                await self.safe_send(chat_id, 'مقدار خالی ذخیره نمی‌شود؛ متن یا مقدار جدید را بفرست.')
                return True
            if key == 'donation_card':
                normalized = text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')).replace(' ','').replace('-','')
                if not normalized.isdigit() or len(normalized) < 16 or len(normalized) > 19:
                    await self.safe_send(chat_id, 'شماره کارت باید ۱۶ تا ۱۹ رقم باشد. دوباره بفرست.')
                    return True
                text = normalized
            if key == 'donation_owner_username': text = text.strip().lstrip('@')
            await self.db.set_setting(key, text, uid)
            await self.db.log(uid, 'donation_setting_updated', details={'setting':key})
            self.states.pop(uid, None)
            await self.safe_send(chat_id, '✅ تنظیم حمایت مالی ذخیره شد.', inline([[('💚 برگشت به تنظیمات حمایت','own:donations')],[('↩️ پنل مالک','admin:page:0'),('✖️ بستن','close')]]))
            return True
        if step == 'edit_about':
            if not text: await self.safe_send(chat_id,'متن خالی قابل ذخیره نیست.'); return True
            await self.db.set_setting('about',text,uid); await self.db.log(uid,'about_updated',details={'length':len(text)})
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ متن «درباره ما» ذخیره شد.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'admin_add_id':
            if not text.isdigit(): await self.safe_send(chat_id,'شناسه باید عددی باشد؛ دوباره بفرست.'); return True
            target=int(text)
            if target==OWNER_ID: await self.safe_send(chat_id,'مالک را نمی‌توان به‌عنوان ادمین معمولی اضافه کرد.'); return True
            async with self.db._pool().acquire() as c:
                await c.execute('INSERT INTO admins(user_id,display_name,permissions,added_by) VALUES($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE SET active=TRUE,added_by=EXCLUDED.added_by',target,'',DEFAULT_PERMS,uid)
            await self.db.log(uid,'admin_added','user',target)
            self.states.pop(uid,None); await self.safe_send(chat_id,f'✅ ادمین `{target}` اضافه شد.\nمجوز بن کاربر و گروه به‌صورت پیش‌فرض خاموش است.',inline([[('⚙️ تنظیم دسترسی‌ها',f'own:admin:{target}')],[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return True
        if step == 'team_edit_name':
            state['team_data']['name']=text; state['step']='team_edit_title'
            await self.safe_send(chat_id,'عنوان جدید را بفرست.'); return True
        if step == 'team_edit_title':
            state['team_data']['title']=text; state['step']='team_edit_description'
            await self.safe_send(chat_id,'توضیح جدید را بفرست؛ برای خالی‌گذاشتن یک خط تیره بفرست.'); return True
        if step == 'team_edit_description':
            state['team_data']['description']='' if text=='-' else text; state['step']='team_edit_username'
            await self.safe_send(chat_id,'آیدی عمومی جدید را بفرست؛ اگر ندارد یک خط تیره بفرست.'); return True
        if step == 'team_edit_username':
            d=state['team_data']; username='' if text=='-' else text.lstrip('@'); member_id=int(state['member_id'])
            async with self.db._pool().acquire() as c:
                await c.execute('UPDATE team_members SET name=$2,title=$3,description=$4,username=$5 WHERE id=$1',member_id,d['name'],d['title'],d['description'],username)
            await self.db.log(uid,'team_member_updated','team_member',member_id)
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ اطلاعات عضو تیم ویرایش شد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return True
        if step == 'team_add_name':
            state['team_data']['name']=text; state['step']='team_add_title'
            await self.safe_send(chat_id,'عنوان یا نقش این عضو را بفرست.'); return True
        if step == 'team_add_title':
            state['team_data']['title']=text; state['step']='team_add_description'
            await self.safe_send(chat_id,'توضیح کوتاه عضو را بفرست؛ اگر توضیح ندارد یک خط تیره بفرست.'); return True
        if step == 'team_add_description':
            state['team_data']['description']='' if text=='-' else text; state['step']='team_add_username'
            await self.safe_send(chat_id,'آیدی عمومی عضو را بفرست؛ اگر ندارد یک خط تیره بفرست.'); return True
        if step == 'team_add_username':
            d=state['team_data']; username='' if text=='-' else text.lstrip('@')
            await self.db.add_team_member(d['name'],d['title'],d['description'],username,uid)
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ عضو تیم اضافه شد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return True
        if step == 'ban_reason':
            state['reason']=text or 'دلیلی ثبت نشده'; state['step']='ban_duration'
            await self.safe_send(chat_id,'مدت محدودیت رو به روز بفرست؛ برای دائمی بنویس `دائمی` و برای یک روز `1`.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'ban_duration':
            permanent=text.strip() in ('دائمی','دائم','بی پایان','بی‌پایان','permanent','forever')
            days=None
            if not permanent:
                match=re.search(r'\d+',text)
                if not match or int(match.group())<1 or int(match.group())>36500:
                    await self.safe_send(chat_id,'مدت باید تعداد روز مثبت یا «دائمی» باشد.'); return True
                days=int(match.group())
            target_type=state['target_type']; target_id=int(state['target_id'])
            info=await self.db.search_target(str(target_id),target_type)
            target=info[0] if info else {'id':target_id,'username':None,'name':str(target_id)}
            case=await self.db.create_case(target_type,target_id,target.get('username'),target.get('name') or str(target_id),state.get('reason',''),days,permanent,uid)
            self.states.pop(uid,None)
            duration='دائمی' if permanent else f'{days} روز'
            notice=f"*⛔ محدودیت MEGA BOT*\nدلیل: {case['reason']}\nمدت: {duration}\nکد پیگیری: `#{case['case_code']}`\nصادرکننده: `{uid}`"
            # For group restriction, post and try to pin the notice. For user, DM is best-effort.
            sent=None
            if target_type=='group':
                sent=await self.safe_send(target_id,notice)
                if sent:
                    try: await self.api.pin_message(target_id,int(sent['message_id']))
                    except Exception as exc: log.info('Could not pin group restriction notice: %s',exc)
                # Best-effort owner notification; if the owner has never started the bot, Telegram may reject the DM.
                try:
                    members=await self.api.call('getChatAdministrators', {'chat_id':target_id})
                    creator=next((m.get('user') for m in (members or []) if m.get('status')=='creator' and m.get('user')),None)
                    if creator:
                        await self.safe_send(int(creator['id']), notice+'\n\nاین گروه در MEGA BOT محدود شده است.')
                except Exception as exc:
                    log.info('Could not notify group owner: %s',exc)
            else:
                sent=await self.safe_send(target_id,notice+'\n\nبرای پیگیری، بخش *تیم من* را باز کن.')
            await self.safe_send(chat_id,f"✅ پرونده ثبت شد.\nکد: `#{case['case_code']}`\nمدت: {duration}\nاعلان: {'ارسال شد' if sent else 'ارسال پیام ممکن نشد'}",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))
            return True
        return False

    def case_text(self, case: dict) -> str:
        target='کاربر' if case['target_type']=='user' else 'گروه'
        duration='دائمی' if case['permanent'] else (f"{case['duration_days']} روز" if case['duration_days'] else 'نامشخص')
        active='فعال' if case['active'] else 'پایان‌یافته / رفع‌شده'
        return (f"*پرونده #{case['case_code']}*\nنوع: {target}\nشناسه: `{case['target_id']}`\nنام: {case['target_name']}\n"
                f"دلیل: {case['reason']}\nمدت: {duration}\nوضعیت: {active}\nصادرکننده: `{case['issued_by']}`\n"
                f"تاریخ ثبت: {case['created_at'].strftime('%Y-%m-%d %H:%M UTC')}")

    async def my_cases(self, chat_id: int, uid: int) -> None:
        async with self.db._pool().acquire() as c:
            rows=await c.fetch("SELECT * FROM moderation_cases WHERE target_type='user' AND target_id=$1 ORDER BY created_at DESC LIMIT 10",uid)
        if not rows:
            await self.safe_send(chat_id,'*تیم من / پیگیری*\nپرونده‌ای برای حساب تو ثبت نشده است.',USER_KEYBOARD); return
        text='*تیم من / پیگیری پرونده‌ها*\n\n'+'\n\n'.join(f"*#{r['case_code']}* — {'فعال' if r['active'] else 'پایان‌یافته'}\nدلیل: {r['reason']}\nمدت: {'دائمی' if r['permanent'] else str(r['duration_days'])+' روز'}" for r in rows)
        await self.safe_send(chat_id,text,inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))

    async def start_ttt(self, chat_id: int, creator: int, name: str) -> None:
        gid=secrets.token_hex(3)
        game={'kind':'ttt','creator':creator,'chat_id':chat_id,'board':[' ']*9,'players':[creator,None],'turn':0}
        self.game_cache[gid]=game
        await self.safe_send(chat_id,f"*بازی دوز*\nسازنده: {name}\nنفر دوم با زدن «پیوستن» وارد بازی شود.",self.ttt_keyboard(gid,game))

    def ttt_keyboard(self, gid: str, game: dict) -> dict:
        board=game['board']; rows=[]
        for r in range(3):
            rows.append([(board[r*3+c] if board[r*3+c].strip() else '▫️',f'ttt:move:{gid}:{r*3+c}') for c in range(3)])
        rows.append([('🙋 پیوستن','ttt:join:'+gid),('✖️ لغو','game:cancel:'+gid)])
        return inline(rows)

    async def handle_ttt_callback(self, query: dict, data: str) -> None:
        msg=query.get('message') or {}; chat_id=int((msg.get('chat') or {}).get('id',0)); mid=int(msg.get('message_id',0)); uid=int((query.get('from') or {}).get('id',0))
        parts=data.split(':'); action=parts[1]; gid=parts[2]; game=self.game_cache.get(gid)
        if not game: return await self.safe_send(chat_id,'این بازی تمام شده است.')
        if action=='join':
            if uid==game['creator']: return await self.safe_send(chat_id,'برای بازی، یک نفر دیگر باید بپیوندد.')
            game['players'][1]=uid
            await self.safe_edit(chat_id,mid,'*بازی دوز شروع شد!* نفرات به ترتیب بازی می‌کنند.',self.ttt_keyboard(gid,game)); return
        if action=='move':
            if not game['players'][1]: return await self.safe_send(chat_id,'اول نفر دوم باید بپیوندد.')
            idx=int(parts[3]); player_idx=game['turn']
            if uid != game['players'][player_idx]: return await self.safe_send(chat_id,'الان نوبت تو نیست.')
            if game['board'][idx].strip(): return await self.safe_send(chat_id,'این خانه پر است.')
            mark='❌' if player_idx==0 else '⭕'; game['board'][idx]=mark; game['turn']=1-player_idx
            win=self.ttt_winner(game['board'])
            if win or all(x.strip() for x in game['board']):
                outcome='مساوی شد!' if not win else f"برنده: `{game['players'][0] if win=='❌' else game['players'][1]}`"
                await self.safe_edit(chat_id,mid,'*پایان بازی دوز*\n'+outcome,self.ttt_keyboard(gid,game)); self.game_cache.pop(gid,None)
            else: await self.safe_edit(chat_id,mid,'*بازی دوز*\nنوبت بازیکن بعدی است.',self.ttt_keyboard(gid,game))

    @staticmethod
    def ttt_winner(b: list[str]) -> str | None:
        lines=[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
        for a,c,d in lines:
            if b[a].strip() and b[a]==b[c]==b[d]: return b[a]
        return None

    async def start_one_game(self, chat_id: int, uid: int, name: str) -> None:
        gid=secrets.token_hex(3)
        self.game_cache[gid]={'kind':'one','creator':uid,'chat_id':chat_id,'players':{uid:name},'started':False}
        await self.safe_send(chat_id,f"*🎲 هرکی تک بیاره*\nسازنده: {name}\nبازیکن‌ها با عضویت وارد شوند.",inline([
            [('▶️ استارت توسط شروع‌کننده','one:start:'+gid)],
            [('🙋 عضویت در هرکی تک بیاره','one:join:'+gid),('🚪 خروج','one:leave:'+gid)],
            [('🗑️ حذف بازی','game:cancel:'+gid)],
            [('↩️ برگشت','home'),('✖️ بستن','close')]
        ]))

    async def handle_one_callback(self, query: dict, data: str) -> None:
        msg=query.get('message') or {}; chat_id=int((msg.get('chat') or {}).get('id',0)); mid=int(msg.get('message_id',0)); uid=int((query.get('from') or {}).get('id',0))
        _,action,gid=data.split(':',2); game=self.game_cache.get(gid)
        if not game: return await self.safe_send(chat_id,'این بازی دیگه فعال نیست.')
        if action=='join':
            game['players'][uid]=str(uid); await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game)); return
        if action=='leave':
            game['players'].pop(uid,None); await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game)); return
        if action=='start':
            if uid!=game['creator']: return await self.safe_send(chat_id,'فقط شروع‌کننده می‌تواند بازی را استارت کند.')
            if len(game['players'])<2: return await self.safe_send(chat_id,'حداقل دو بازیکن لازم است.')
            game['started']=True
            rolls={p:random.randint(1,6) for p in game['players']}
            ones=[p for p,v in rolls.items() if v==1]
            if ones: result='🏆 تک آورنده: '+', '.join(f'`{p}`' for p in ones)
            else: result='هیچکس تک نیاورد...'
            result+='\n\n'+ '\n'.join(f'`{p}`: {v}' for p,v in rolls.items())
            await self.safe_edit(chat_id,mid,'*نتیجه هرکی تک بیاره*\n'+result,inline([[('🔁 دور بعد','one:next:'+gid),('🗑️ حذف بازی','game:cancel:'+gid)],[('✖️ بستن','close')]])); return
        if action=='next':
            if uid!=game['creator']: return await self.safe_send(chat_id,'فقط شروع‌کننده می‌تواند دور بعد را اجرا کند.')
            game['started']=False; await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game))

    def one_text(self, game: dict) -> str:
        players=game.get('players',{})
        return '*🎲 هرکی تک بیاره*\n\nبازیکنان:\n'+'\n'.join(f"• `{pid}` — {name}" for pid,name in players.items())+'\n\nسازنده می‌تواند بازی را شروع کند.'

    def one_keyboard(self, gid: str, game: dict) -> dict:
        return inline([[('▶️ استارت توسط شروع‌کننده','one:start:'+gid)],[('🙋 عضویت','one:join:'+gid),('🚪 خروج','one:leave:'+gid)],[('🗑️ حذف بازی','game:cancel:'+gid)],[('↩️ برگشت','home'),('✖️ بستن','close')]])
