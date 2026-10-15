from __future__ import annotations
import asyncio, io, os, shutil, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_VIDEO_SECONDS = 20
# Target: most GIFs well under 5MB; long clips may approach this ceiling.
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_LONG = 8 * 1024 * 1024  # only for longer source videos
GIF_MAX_SIDE = 480
GIF_FPS = 8
GIF_MAX_FRAMES = 48

_BOLD_FONT_CANDIDATES = (
    '/usr/share/fonts/SlidesCarnival/google/Noto Sans Arabic/static/NotoSansArabic-Bold.ttf',
    '/usr/share/fonts/SlidesCarnival/google/Noto Sans Arabic/static/NotoSansArabic-ExtraBold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf',
)


def _font(size: int):
    size = max(8, int(size))
    for candidate in _BOLD_FONT_CANDIDATES:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, stroke_width: int) -> list[str]:
    """Word-wrap so each line fits max_width. Falls back to char-wrap for long tokens."""
    text = (text or '').strip()
    if not text:
        return []
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = ''
    for word in words:
        trial = word if not current else f'{current} {word}'
        bbox = draw.textbbox((0, 0), trial, font=font, stroke_width=stroke_width)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        # single word longer than max_width → char wrap
        if draw.textbbox((0, 0), word, font=font, stroke_width=stroke_width)[2] > max_width:
            chunk = ''
            for ch in word:
                t2 = chunk + ch
                if draw.textbbox((0, 0), t2, font=font, stroke_width=stroke_width)[2] <= max_width:
                    chunk = t2
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            current = chunk
        else:
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _overlay(image: Image.Image, text: str) -> Image.Image:
    """Bold, bright white text, always horizontal, auto-shrink, never leave the frame."""
    if not (text or '').strip():
        return image
    image = image.convert('RGBA')
    draw = ImageDraw.Draw(image)
    margin = max(6, int(min(image.width, image.height) * 0.04))
    max_width = max(20, image.width - 2 * margin)
    max_height = max(20, int(image.height * 0.42))  # text block may use up to ~42% of height

    # Slightly larger than before (~0.16 of height)
    size = max(16, int(image.height * 0.16))
    lines: list[str] = []
    font = _font(size)
    stroke = max(2, size // 12)

    # Shrink until wrapped block fits both width and height
    while size >= 10:
        font = _font(size)
        stroke = max(2, size // 12)
        lines = _wrap_text(draw, text, font, max_width, stroke)
        # measure block
        line_heights = []
        max_line_w = 0
        for ln in lines:
            box = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
            max_line_w = max(max_line_w, box[2] - box[0])
            line_heights.append(box[3] - box[1])
        gap = max(2, size // 8)
        block_h = sum(line_heights) + gap * max(0, len(lines) - 1)
        if max_line_w <= max_width and block_h <= max_height:
            break
        size -= 2
    else:
        font = _font(10)
        stroke = 2
        lines = _wrap_text(draw, text, font, max_width, stroke)
        line_heights = []
        for ln in lines:
            box = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
            line_heights.append(box[3] - box[1])
        gap = 2
        block_h = sum(line_heights) + gap * max(0, len(lines) - 1)

    # Vertical position ~75% down, clamp fully inside frame
    y = int(image.height * 0.72) - block_h // 2
    y = max(margin, min(image.height - block_h - margin, y))

    # Pure white + strong black outline for contrast (reads “whiter / bolder”)
    fill = (255, 255, 255, 255)
    outline = (0, 0, 0, 255)

    for i, ln in enumerate(lines):
        box = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
        tw = box[2] - box[0]
        th = box[3] - box[1]
        x = (image.width - tw) // 2
        x = max(margin, min(image.width - tw - margin, x))
        draw.text((x, y), ln, font=font, fill=fill, stroke_width=stroke, stroke_fill=outline)
        y += th + gap

    return image.convert('RGB')


def _quantize_gif_frames(frames: list[Image.Image]) -> list[Image.Image]:
    """Reduce palette for smaller GIF size."""
    out = []
    for fr in frames:
        q = fr.convert('P', palette=Image.ADAPTIVE, colors=128)
        out.append(q)
    return out


def image_to_gif(raw: bytes, text: str = '') -> bytes:
    with Image.open(io.BytesIO(raw)) as source:
        frames = []
        try:
            count = min(getattr(source, 'n_frames', 1), GIF_MAX_FRAMES)
            for i in range(count):
                source.seek(i)
                frame = ImageOps.contain(source.convert('RGB'), (GIF_MAX_SIDE, GIF_MAX_SIDE))
                if text:
                    frame = _overlay(frame, text)
                frames.append(frame.copy())
        except EOFError:
            pass
    if not frames:
        raise ValueError('تصویر قابل پردازش نیست.')
    frames = _quantize_gif_frames(frames)
    out = io.BytesIO()
    frames[0].save(
        out,
        format='GIF',
        save_all=True,
        append_images=frames[1:],
        duration=max(80, int(1000 / GIF_FPS)),
        loop=0,
        optimize=True,
        disposal=2,
    )
    result = out.getvalue()
    if len(result) > MAX_OUTPUT_BYTES:
        # Second pass: smaller side + fewer frames
        smaller = []
        step = max(1, len(frames) // 24)
        for fr in frames[::step][:24]:
            fr_rgb = fr.convert('RGB')
            fr_rgb = ImageOps.contain(fr_rgb, (360, 360))
            if text:
                fr_rgb = _overlay(fr_rgb, text)
            smaller.append(fr_rgb.convert('P', palette=Image.ADAPTIVE, colors=64))
        out2 = io.BytesIO()
        smaller[0].save(
            out2, format='GIF', save_all=True, append_images=smaller[1:],
            duration=120, loop=0, optimize=True, disposal=2,
        )
        result = out2.getvalue()
        if len(result) > MAX_OUTPUT_BYTES:
            raise ValueError('حجم GIF بیش از حد مجاز است (۵ مگابایت).')
    return result


def video_to_gif(raw: bytes, suffix: str = '.mp4', text: str = '') -> bytes:
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg روی سرور نصب نیست؛ از Dockerfile همین پروژه برای استقرار استفاده کن.')
    # Longer sources get a slightly higher soft ceiling
    long_source = len(raw) > 3 * 1024 * 1024
    limit = MAX_OUTPUT_LONG if long_source else MAX_OUTPUT_BYTES

    with tempfile.TemporaryDirectory(prefix='megabot-gif-') as tmp:
        src, dst = Path(tmp) / ('input' + suffix), Path(tmp) / 'output.gif'
        src.write_bytes(raw)
        side = GIF_MAX_SIDE
        fps = GIF_FPS
        vf = f'fps={fps},scale={side}:-1:flags=lanczos:force_original_aspect_ratio=decrease'
        if text:
            safe = (
                text.replace('\\', '\\\\')
                .replace(':', '\\:')
                .replace("'", "\\'")
                .replace('%', '\\%')
            )
            font = next((p for p in _BOLD_FONT_CANDIDATES if os.path.exists(p)), '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
            # Auto-ish size: shorter text larger; long text smaller (never leaves frame via x/y clamp)
            tlen = max(1, len(text.strip()))
            # h/9 for short, down toward h/16 for long
            fs_div = 9 if tlen <= 12 else (11 if tlen <= 24 else (14 if tlen <= 40 else 16))
            # drawtext: horizontal, centered, lower third, bold font + strong border
            vf += (
                f",drawtext=fontfile='{font}':text='{safe}'"
                f":fontcolor=white:borderw=3:bordercolor=black"
                f":fontsize=h/{fs_div}:x=(w-text_w)/2:y=h*0.72-text_h/2"
            )
        palette = Path(tmp) / 'palette.png'
        cmd1 = [
            'ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src),
            '-t', str(MAX_VIDEO_SECONDS), '-vf', vf + ',palettegen=stats_mode=diff', str(palette),
        ]
        cmd2 = [
            'ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src), '-i', str(palette),
            '-t', str(MAX_VIDEO_SECONDS),
            '-filter_complex', f'{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3',
            '-loop', '0', str(dst),
        ]
        subprocess.run(cmd1, check=True, timeout=45, capture_output=True)
        subprocess.run(cmd2, check=True, timeout=45, capture_output=True)
        result = dst.read_bytes()

        # Re-encode smaller if still over target
        if len(result) > limit:
            side2, fps2 = 360, 6
            vf2 = f'fps={fps2},scale={side2}:-1:flags=lanczos:force_original_aspect_ratio=decrease'
            if text:
                safe = (
                    text.replace('\\', '\\\\')
                    .replace(':', '\\:')
                    .replace("'", "\\'")
                    .replace('%', '\\%')
                )
                font = next((p for p in _BOLD_FONT_CANDIDATES if os.path.exists(p)), '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
                tlen = max(1, len(text.strip()))
                fs_div = 10 if tlen <= 12 else (13 if tlen <= 30 else 16)
                vf2 += (
                    f",drawtext=fontfile='{font}':text='{safe}'"
                    f":fontcolor=white:borderw=3:bordercolor=black"
                    f":fontsize=h/{fs_div}:x=(w-text_w)/2:y=h*0.72-text_h/2"
                )
            palette2 = Path(tmp) / 'palette2.png'
            dst2 = Path(tmp) / 'output2.gif'
            subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src),
                 '-t', str(MAX_VIDEO_SECONDS), '-vf', vf2 + ',palettegen=stats_mode=diff', str(palette2)],
                check=True, timeout=45, capture_output=True,
            )
            subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src), '-i', str(palette2),
                 '-t', str(MAX_VIDEO_SECONDS),
                 '-filter_complex', f'{vf2}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5',
                 '-loop', '0', str(dst2)],
                check=True, timeout=45, capture_output=True,
            )
            result = dst2.read_bytes()

        hard_cap = MAX_OUTPUT_LONG if long_source else MAX_OUTPUT_BYTES
        if len(result) > hard_cap:
            raise ValueError(f'حجم GIF بیش از حد مجاز است ({hard_cap // (1024 * 1024)} مگابایت).')
        return result


async def convert_media(raw: bytes, filename: str, text: str = '') -> bytes:
    suffix = Path(filename).suffix.lower()
    if suffix in ('.mp4', '.mov', '.webm', '.mkv', '.avi'):
        return await asyncio.to_thread(video_to_gif, raw, suffix, text)
    return await asyncio.to_thread(image_to_gif, raw, text)
