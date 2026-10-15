"""TTS for MEGA BOT — free, fast, no practical length limit.

Persian: Piper neural «Amir» offline (fa_IR-amir-medium) — free, unlimited,
no API key, works even when cloud TTS is blocked.
English: Google Translate TTS.

Long text is chunked and merged with ffmpeg. Soft safety ceiling 100k chars.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_TEXT = 'تست'
# Soft safety ceiling only (memory); not a product limit
MAX_CHARS = int(os.getenv('TTS_MAX_CHARS', '100000'))
GOOGLE_CHUNK = int(os.getenv('TTS_GOOGLE_CHUNK', '160'))
GOOGLE_PARALLEL = int(os.getenv('TTS_GOOGLE_PARALLEL', '4'))

PIPER_MODEL_NAME = os.getenv('TTS_PIPER_MODEL', 'fa_IR-amir-medium')
PIPER_BASE = os.getenv(
    'TTS_PIPER_BASE',
    'https://huggingface.co/rhasspy/piper-voices/resolve/main/fa/fa_IR/amir/medium',
)
PIPER_DIR = Path(os.getenv('TTS_PIPER_DIR', '/tmp/mega_piper'))
# Calm speech: higher length_scale = slower; lower noise = less nervous
PIPER_LENGTH_SCALE = float(os.getenv('TTS_PIPER_LENGTH', '1.45'))
PIPER_NOISE_SCALE = float(os.getenv('TTS_PIPER_NOISE', '0.35'))
PIPER_NOISE_W = float(os.getenv('TTS_PIPER_NOISE_W', '0.55'))
PIPER_VOLUME = float(os.getenv('TTS_PIPER_VOLUME', '1.0'))

_piper_voice = None
_piper_lock = asyncio.Lock()


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def normalize_tts_text(text: str) -> str:
    t = (text or '').strip()
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def detect_lang(text: str) -> str:
    latin = len(re.findall(r'[A-Za-z]', text or ''))
    arabic = len(re.findall(r'[\u0600-\u06FF]', text or ''))
    if latin > arabic * 1.2 and latin >= 3:
        return 'en'
    return 'fa'


def estimate_duration_sec(text: str) -> int:
    n = len(normalize_tts_text(text) or '')
    return max(1, int(n / 14) + 1)


def _chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buf = ''
    parts = re.split(r'(?<=[.!?…؟!\n،,;؛:])\s+', text)
    for part in parts:
        if not part:
            continue
        if len(buf) + len(part) + 1 <= size:
            buf = f'{buf} {part}'.strip() if buf else part
        else:
            if buf:
                chunks.append(buf)
            if len(part) <= size:
                buf = part
            else:
                for i in range(0, len(part), size):
                    chunks.append(part[i:i + size])
                buf = ''
    if buf:
        chunks.append(buf)
    return chunks or [text[:size]]


def _ffmpeg_to_ogg(src: str, ogg: str, timeout: int = 300) -> None:
    ffmpeg = _which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg not found')
    r = subprocess.run(
        [
            ffmpeg, '-y', '-i', src,
            '-c:a', 'libopus', '-b:a', '32k',
            '-vbr', 'on', '-compression_level', '10',
            ogg,
        ],
        capture_output=True,
        timeout=timeout,
    )
    if r.returncode != 0 or not os.path.isfile(ogg) or os.path.getsize(ogg) < 40:
        err = (r.stderr or b'')[-300:].decode('utf-8', 'ignore')
        raise RuntimeError(f'ffmpeg ogg failed: {err}')


def _concat_audio_files(paths: list[str], out: str) -> None:
    if len(paths) == 1:
        shutil.copyfile(paths[0], out)
        return
    ffmpeg = _which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg not found')
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as lst:
        for p in paths:
            # escape single quotes for concat demuxer
            safe = p.replace("'", "'\\''")
            lst.write(f"file '{safe}'\n")
        list_path = lst.name
    try:
        r = subprocess.run(
            [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', out],
            capture_output=True,
            timeout=300,
        )
        if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 40:
            r2 = subprocess.run(
                [
                    ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_path,
                    '-c:a', 'libopus', '-b:a', '32k', out,
                ],
                capture_output=True,
                timeout=300,
            )
            if r2.returncode != 0 or not os.path.isfile(out):
                raise RuntimeError('ffmpeg concat failed')
    finally:
        try:
            os.unlink(list_path)
        except Exception:
            pass


# ── Google Translate TTS (fast, free, famous) ───────────────────────────

def _google_one_chunk_sync(text: str, lang: str) -> bytes:
    q = urllib.parse.urlencode({'ie': 'UTF-8', 'q': text, 'tl': lang, 'client': 'tw-ob'})
    url = f'https://translate.google.com/translate_tts?{q}'
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://translate.google.com/',
            'Accept': '*/*',
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    if len(data) < 50:
        raise RuntimeError('google empty chunk')
    return data


async def _google_one_chunk(text: str, lang: str) -> bytes:
    return await asyncio.to_thread(_google_one_chunk_sync, text, lang)


async def synthesize_google(text: str, lang: str) -> bytes:
    text = normalize_tts_text(text) or DEFAULT_TEXT
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    chunks = _chunk_text(text, GOOGLE_CHUNK)
    # Parallel fetch in batches for speed
    mp3_parts: list[bytes] = []
    sem = asyncio.Semaphore(max(1, GOOGLE_PARALLEL))

    async def fetch(i: int, ch: str) -> tuple[int, bytes]:
        async with sem:
            data = await _google_one_chunk(ch, lang)
            return i, data

    tasks = [fetch(i, ch) for i, ch in enumerate(chunks)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda x: x[0])
    mp3_parts = [d for _, d in results]

    with tempfile.TemporaryDirectory(prefix='mega-gtts-') as td:
        paths: list[str] = []
        for i, data in enumerate(mp3_parts):
            path = os.path.join(td, f'{i:04d}.mp3')
            with open(path, 'wb') as f:
                f.write(data)
            paths.append(path)
        merged = os.path.join(td, 'm.mp3')
        _concat_audio_files(paths, merged)
        ogg = os.path.join(td, 'o.ogg')
        try:
            _ffmpeg_to_ogg(merged, ogg)
            with open(ogg, 'rb') as f:
                return f.read()
        except Exception:
            with open(merged, 'rb') as f:
                return f.read()


# ── Piper offline fallback (unlimited, no network) ──────────────────────

def _ensure_piper_files() -> Path:
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    name = PIPER_MODEL_NAME
    onnx = PIPER_DIR / f'{name}.onnx'
    conf = PIPER_DIR / f'{name}.onnx.json'
    if onnx.is_file() and onnx.stat().st_size > 1000 and conf.is_file():
        return onnx
    # Download model + config
    for suffix, dest in (('.onnx', onnx), ('.onnx.json', conf)):
        url = f'{PIPER_BASE}/{name}{suffix if suffix != ".onnx" else ".onnx"}'
        # huggingface layout: .../amir/medium/fa_IR-amir-medium.onnx
        url = f'{PIPER_BASE}/{name}{suffix}'
        if suffix == '.onnx':
            url = f'{PIPER_BASE}/{name}.onnx'
        else:
            url = f'{PIPER_BASE}/{name}.onnx.json'
        log.info('Downloading Piper %s', url)
        req = urllib.request.Request(url, headers={'User-Agent': 'MEGA-BOT'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        dest.write_bytes(data)
    if not onnx.is_file() or onnx.stat().st_size < 1000:
        raise RuntimeError('piper model download failed')
    return onnx


def _load_piper():
    global _piper_voice
    if _piper_voice is not None:
        return _piper_voice
    from piper import PiperVoice
    model = _ensure_piper_files()
    _piper_voice = PiperVoice.load(str(model))
    return _piper_voice


def _piper_syn_config():
    """Calm, slower delivery — less nervous / rushed."""
    from piper.config import SynthesisConfig
    return SynthesisConfig(
        length_scale=PIPER_LENGTH_SCALE,   # >1 slower & calmer
        noise_scale=PIPER_NOISE_SCALE,     # lower = smoother, less jitter
        noise_w_scale=PIPER_NOISE_W,
        normalize_audio=True,
        volume=PIPER_VOLUME,
    )


def _piper_synthesize_sync(text: str) -> bytes:
    import wave
    voice = _load_piper()
    text = normalize_tts_text(text) or DEFAULT_TEXT
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    # Slightly longer phrases read more naturally when speaking slowly
    chunks = _chunk_text(text, 600)
    syn = _piper_syn_config()
    with tempfile.TemporaryDirectory(prefix='mega-piper-') as td:
        wav_parts: list[str] = []
        for i, ch in enumerate(chunks):
            path = os.path.join(td, f'{i:04d}.wav')
            with wave.open(path, 'wb') as wf:
                voice.synthesize_wav(ch, wf, syn_config=syn)
            wav_parts.append(path)
        merged = os.path.join(td, 'm.wav')
        _concat_audio_files(wav_parts, merged)
        ogg = os.path.join(td, 'o.ogg')
        _ffmpeg_to_ogg(merged, ogg)
        with open(ogg, 'rb') as f:
            data = f.read()
    if len(data) < 50:
        raise RuntimeError('piper empty')
    return data


async def synthesize_piper_fa(text: str) -> bytes:
    async with _piper_lock:
        return await asyncio.to_thread(_piper_synthesize_sync, text)


# ── Public ──────────────────────────────────────────────────────────────

async def synthesize_persian(text: str = DEFAULT_TEXT) -> tuple[bytes, str]:
    """FA → Piper Amir offline (free/unlimited/fast). EN → Google TTS."""
    text = normalize_tts_text(text or DEFAULT_TEXT) or DEFAULT_TEXT
    lang = detect_lang(text)
    errors: list[str] = []

    if lang == 'en':
        try:
            data = await synthesize_google(text, 'en')
            ext = 'ogg' if data[:4] == b'OggS' else 'mp3'
            log.info('TTS engine=google-en bytes=%s', len(data))
            return data, f'voice.{ext}'
        except Exception as exc:
            raise RuntimeError(f'انگلیسی google:{type(exc).__name__}: {exc}') from exc

    # Persian: offline Piper neural male (no network, no quota, long text OK)
    try:
        data = await synthesize_piper_fa(text)
        log.info('TTS engine=piper-amir bytes=%s chars=%s', len(data), len(text))
        return data, 'voice.ogg'
    except Exception as exc:
        errors.append(f'piper:{type(exc).__name__}')
        log.warning('piper-fa failed: %s', exc)

    # Last resort: Google (often blocked for fa on some hosts)
    try:
        data = await synthesize_google(text, 'fa')
        ext = 'ogg' if data[:4] == b'OggS' else 'mp3'
        log.info('TTS engine=google-fa bytes=%s', len(data))
        return data, f'voice.{ext}'
    except Exception as exc:
        errors.append(f'google:{type(exc).__name__}')
        log.warning('google-fa failed: %s', exc)

    raise RuntimeError(f'ساخت ویس ناموفق: {", ".join(errors)}')
