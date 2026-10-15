"""Unicode font stylizer for MEGA BOT — Persian frames + English math alphabets.

Pages of 6 styles. Callback data: font:{token}:{style_id}
Original text stored in-memory by short token (TTL).
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Callable

# token -> (text, expires_ts)
_STORE: dict[str, tuple[str, float]] = {}
_TTL = 3600
PAGE_SIZE = 6
BASE_PAGES = 5  # 30 styles
_EXTRA_OFFSET = 100  # style ids for "more" pool start


def _put(text: str) -> str:
    _cleanup()
    tok = secrets.token_hex(4)
    _STORE[tok] = (text, time.time() + _TTL)
    return tok


def _get(tok: str) -> str | None:
    item = _STORE.get(tok)
    if not item:
        return None
    text, exp = item
    if time.time() > exp:
        _STORE.pop(tok, None)
        return None
    return text


def _cleanup() -> None:
    now = time.time()
    dead = [k for k, (_, e) in _STORE.items() if e <= now]
    for k in dead[:200]:
        _STORE.pop(k, None)


# ── English / Latin math-style maps ─────────────────────────────────────

def _map_chars(text: str, table: dict[str, str]) -> str:
    return ''.join(table.get(c, c) for c in text)


def _build_az(base_upper: int, base_lower: int) -> dict[str, str]:
    t: dict[str, str] = {}
    for i in range(26):
        t[chr(ord('A') + i)] = chr(base_upper + i)
        t[chr(ord('a') + i)] = chr(base_lower + i)
    return t


def _build_az_digits(base_upper: int, base_lower: int, base_digit: int | None = None) -> dict[str, str]:
    t = _build_az(base_upper, base_lower)
    if base_digit is not None:
        for i in range(10):
            t[str(i)] = chr(base_digit + i)
    return t


STYLES_EN: list[tuple[str, Callable[[str], str]]] = [
    ('𝐁𝐨𝐥𝐝', lambda s: _map_chars(s, _build_az_digits(0x1D400, 0x1D41A, 0x1D7CE))),
    ('𝐼𝑡𝑎𝑙𝑖𝑐', lambda s: _map_chars(s, _build_az(0x1D434, 0x1D44E))),
    ('𝑩𝒐𝒍𝒅 𝑰𝒕𝒂𝒍𝒊𝒄', lambda s: _map_chars(s, _build_az(0x1D468, 0x1D482))),
    ('𝒮𝒸𝓇𝒾𝓅𝓉', lambda s: _map_chars(s, _build_az(0x1D49C, 0x1D4B6))),
    ('𝓢𝓬𝓻𝓲𝓹𝓽 𝓑𝓸𝓵𝓭', lambda s: _map_chars(s, _build_az(0x1D4D0, 0x1D4EA))),
    ('𝔉𝔯𝔞𝔨𝔱𝔲𝔯', lambda s: _map_chars(s, _build_az(0x1D504, 0x1D51E))),
    ('𝕱𝖗𝖆𝖐𝖙𝖚𝖗 𝕭𝖔𝖑𝖉', lambda s: _map_chars(s, _build_az(0x1D56C, 0x1D586))),
    ('𝔻𝕠𝕦𝕓𝕝𝕖', lambda s: _map_chars(s, _build_az_digits(0x1D538, 0x1D552, 0x1D7D8))),
    ('𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎', lambda s: _map_chars(s, _build_az_digits(0x1D670, 0x1D68A, 0x1D7F6))),
    ('𝖲𝖺𝗇𝗌', lambda s: _map_chars(s, _build_az_digits(0x1D5A0, 0x1D5BA, 0x1D7E2))),
    ('𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱', lambda s: _map_chars(s, _build_az_digits(0x1D5D4, 0x1D5EE, 0x1D7EC))),
    ('𝘚𝘢𝘯𝘴 𝘐𝘵𝘢𝘭𝘪𝘤', lambda s: _map_chars(s, _build_az(0x1D608, 0x1D622))),
    ('𝙎𝙖𝙣𝙨 𝘽𝙄', lambda s: _map_chars(s, _build_az(0x1D63C, 0x1D656))),
    ('ＳｍａｌｌＣａｐｓ', lambda s: _map_chars(s, {**{chr(ord('a')+i): chr(0x1D00 + i) if False else chr(0x1D00) for i in range(0)}, **{
        'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ғ','g':'ɢ','h':'ʜ','i':'ɪ','j':'ᴊ','k':'ᴋ','l':'ʟ','m':'ᴍ',
        'n':'ɴ','o':'ᴏ','p':'ᴘ','q':'ǫ','r':'ʀ','s':'s','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x','y':'ʏ','z':'ᴢ',
        'A':'ᴀ','B':'ʙ','C':'ᴄ','D':'ᴅ','E':'ᴇ','F':'ғ','G':'ɢ','H':'ʜ','I':'ɪ','J':'ᴊ','K':'ᴋ','L':'ʟ','M':'ᴍ',
        'N':'ɴ','O':'ᴏ','P':'ᴘ','Q':'ǫ','R':'ʀ','S':'s','T':'ᴛ','U':'ᴜ','V':'ᴠ','W':'ᴡ','X':'x','Y':'ʏ','Z':'ᴢ',
    }})),
    ('Ｆｕｌｌｗｉｄｔｈ', lambda s: ''.join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in s)),
    ('Cᴏᴍʙɪɴɪɴɢ', lambda s: ''.join(c + '\u0305' if c.isalpha() else c for c in s)),
    ('‧˙‧Dotted‧˙‧', lambda s: '‧'.join(s) if s else s),
    ('⟦Bracketed⟧', lambda s: f'⟦ {s} ⟧'),
    ('「JP Quotes」', lambda s: f'「{s}」'),
    ('『Fancy』', lambda s: f'『{s}』'),
    ('【Square】', lambda s: f'【{s}】'),
    ('✧ Sparkle ✧', lambda s: f'✧ {s} ✧'),
    ('★ Star ★', lambda s: f'★ {s} ★'),
    ('꧁ ꧂', lambda s: f'꧁༒ {s} ༒꧂'),
    ('༺ ༻', lambda s: f'༺ {s} ༻'),
    ('⫷ ⫸', lambda s: f'⫷ {s} ⫸'),
    ('◈ Diamond ◈', lambda s: f'◈ {s} ◈'),
    ('• Bullet •', lambda s: f'• {s} •'),
    ('═ Lines ═', lambda s: f'═══ {s} ═══'),
    ('︵ ︶', lambda s: f'︵ {s} ︶'),
]

# Extra pool for "more fonts" (non-overlapping decorative)
STYLES_EXTRA: list[tuple[str, Callable[[str], str]]] = [
    ('✦ Crest ✦', lambda s: f'✦･ﾟ✧ {s} ✧･ﾟ✦'),
    ('☾ Moon ☽', lambda s: f'☾ {s} ☽'),
    ('❀ Flower ❀', lambda s: f'❀ {s} ❀'),
    ('❖ Gem ❖', lambda s: f'❖ {s} ❖'),
    ('⚡ Bolt ⚡', lambda s: f'⚡ {s} ⚡'),
    ('♛ Crown ♛', lambda s: f'♛ {s} ♛'),
    ('𓆩♡𓆪', lambda s: f'𓆩♡𓆪 {s} 𓆩♡𓆪'),
    ('░ Shade ░', lambda s: f'░▒▓ {s} ▓▒░'),
    ('「✦」', lambda s: f'「✦ {s} ✦」'),
    ('『♡』', lambda s: f'『♡ {s} ♡』'),
    ('【♪】', lambda s: f'【♪ {s} ♪】'),
    ('⟨ Angle ⟩', lambda s: f'⟨ {s} ⟩'),
    ('⌞ Corner ⌝', lambda s: f'⌞ {s} ⌝'),
    ('╭ ╮', lambda s: f'╭─ {s} ─╮'),
    ('« Guille »', lambda s: f'« {s} »'),
    ('· Dot ·', lambda s: f'· · · {s} · · ·'),
    ('━ Thick ━', lambda s: f'━ {s} ━'),
    ('┊ Thin ┊', lambda s: f'┊ {s} ┊'),
]

# Persian-safe decorative styles (frames only — never corrupt letters)
_TATWEEL = '\u0640'

def _kashida(s: str, n: int = 1) -> str:
    """Controlled tatweel between Arabic letters only."""
    out = []
    for i, c in enumerate(s):
        out.append(c)
        if i + 1 < len(s) and _is_arabic_letter(c) and _is_arabic_letter(s[i + 1]):
            out.append(_TATWEEL * n)
    return ''.join(out)


def _is_arabic_letter(c: str) -> bool:
    o = ord(c)
    return (0x0621 <= o <= 0x064A) or (0x0671 <= o <= 0x06D3)


def _has_arabic(s: str) -> bool:
    return any(_is_arabic_letter(c) for c in s)


def _has_latin(s: str) -> bool:
    return bool(re.search(r'[A-Za-z]', s))


STYLES_FA: list[tuple[str, Callable[[str], str]]] = [
    ('✦ ساده ✦', lambda s: f'✦ {s} ✦'),
    ('★ ستاره ★', lambda s: f'★ {s} ★'),
    ('꧁ تاج ꧂', lambda s: f'꧁༒ {s} ༒꧂'),
    ('༺ زیبا ༻', lambda s: f'༺ {s} ༻'),
    ('「 کادر 」', lambda s: f'「 {s} 」'),
    ('『 دوتایی 』', lambda s: f'『 {s} 』'),
    ('【 مربعی 】', lambda s: f'【 {s} 】'),
    ('⫷ گوشه ⫸', lambda s: f'⫷ {s} ⫸'),
    ('❀ گل ❀', lambda s: f'❀ {s} ❀'),
    ('☾ ماه ☽', lambda s: f'☾ {s} ☽'),
    ('♛ تاج ♛', lambda s: f'♛ {s} ♛'),
    ('𓆩♡𓆪', lambda s: f'𓆩♡𓆪 {s} 𓆩♡𓆪'),
    ('═ خط ═', lambda s: f'═══ {s} ═══'),
    ('• نقطه •', lambda s: f'• {s} •'),
    ('✧ درخشان ✧', lambda s: f'✧･ﾟ {s} ･ﾟ✧'),
    ('◈ الماس ◈', lambda s: f'◈ {s} ◈'),
    ('کشیدهٔ نرم', lambda s: _kashida(s, 1)),
    ('کشیدهٔ بلند', lambda s: _kashida(s, 2)),
    ('「✦」', lambda s: f'「✦ {s} ✦」'),
    ('『♡』', lambda s: f'『♡ {s} ♡』'),
    ('【♪】', lambda s: f'【♪ {s} ♪】'),
    ('╭─╮', lambda s: f'╭─ {s} ─╮'),
    ('« نقل »', lambda s: f'« {s} »'),
    ('┊ ظریف ┊', lambda s: f'┊ {s} ┊'),
    ('░ سایه ░', lambda s: f'░▒▓ {s} ▓▒░'),
    ('✦･ﾟ✧', lambda s: f'✦･ﾟ✧ {s} ✧･ﾟ✦'),
    ('‧˙‧ نقطه‌ای', lambda s: '‧'.join(s) if len(s) <= 24 else f'‧ {s} ‧'),
    ('━━ ضخیم ━━', lambda s: f'━━ {s} ━━'),
    ('︵ خمیده ︶', lambda s: f'︵ {s} ︶'),
    ('♡ قلب ♡', lambda s: f'♡ {s} ♡'),
]


def _catalog_for(text: str) -> list[tuple[str, Callable[[str], str]]]:
    """Pick styles: FA frames for Arabic, EN transforms for Latin, both for mixed."""
    fa = _has_arabic(text)
    en = _has_latin(text)
    if fa and not en:
        return list(STYLES_FA)
    if en and not fa:
        return list(STYLES_EN)
    # mixed / neither: FA frames first (safe) then a few EN maps for Latin parts
    mixed: list[tuple[str, Callable[[str], str]]] = list(STYLES_FA[:18])
    mixed.extend(STYLES_EN[:12])
    return mixed


def total_base_styles(text: str) -> int:
    return min(len(_catalog_for(text)), BASE_PAGES * PAGE_SIZE)


def total_pages(text: str, extra: bool = False) -> int:
    n = total_base_styles(text)
    if extra:
        n += len(STYLES_EXTRA)
    return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)


def apply_style(text: str, style_id: int) -> str:
    cat = _catalog_for(text)
    if 0 <= style_id < len(cat):
        return cat[style_id][1](text)
    if style_id >= _EXTRA_OFFSET:
        idx = style_id - _EXTRA_OFFSET
        if 0 <= idx < len(STYLES_EXTRA):
            return STYLES_EXTRA[idx][1](text)
    return text


def style_label(text: str, style_id: int) -> str:
    cat = _catalog_for(text)
    if 0 <= style_id < len(cat):
        sample = cat[style_id][1](text[:12] if len(text) > 12 else text)
        return sample[:40] if sample else cat[style_id][0]
    if style_id >= _EXTRA_OFFSET:
        idx = style_id - _EXTRA_OFFSET
        if 0 <= idx < len(STYLES_EXTRA):
            sample = STYLES_EXTRA[idx][1](text[:12] if len(text) > 12 else text)
            return sample[:40] if sample else STYLES_EXTRA[idx][0]
    return '?'


def page_style_ids(text: str, page: int, extra: bool = False) -> list[int]:
    """Return up to PAGE_SIZE style ids for this page (0-based page)."""
    cat = _catalog_for(text)
    base_n = min(len(cat), BASE_PAGES * PAGE_SIZE)
    ids = list(range(base_n))
    if extra:
        ids.extend(_EXTRA_OFFSET + i for i in range(len(STYLES_EXTRA)))
    start = page * PAGE_SIZE
    return ids[start:start + PAGE_SIZE]


def build_panel(text: str, page: int = 0, extra: bool = False) -> tuple[str, list[list[tuple[str, str]]], str]:
    """Returns (caption, keyboard rows as (label, callback_data), token)."""
    text = (text or '').strip()
    if not text:
        text = 'متن'
    if len(text) > 200:
        text = text[:200]
    tok = _put(text)
    pages = total_pages(text, extra=extra)
    page = max(0, min(page, pages - 1))
    ids = page_style_ids(text, page, extra=extra)

    caption = (
        f'✨ *فونت‌ساز MEGA BOT*\n\n'
        f'📝 متن اصلی: `{text[:80]}`\n\n'
        f'صفحه {page + 1} از {pages}\n'
        f'یک استایل را انتخاب کن تا متن قابل‌کپی برایت ارسال شود:'
    )
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for sid in ids:
        label = style_label(text, sid)
        # Button labels max ~64 chars for Telegram
        label = label.replace('\n', ' ')[:28] or f'#{sid}'
        row.append((label, f'font:{tok}:{sid}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(('⬅️ قبلی', f'fontp:{tok}:{page - 1}:{"1" if extra else "0"}'))
    if page < pages - 1:
        nav.append(('بعدی ➡️', f'fontp:{tok}:{page + 1}:{"1" if extra else "0"}'))
    if nav:
        rows.append(nav)

    # On last base page or last extra page show more
    if not extra and page >= BASE_PAGES - 1:
        rows.append([('🔄 فونت‌های بیشتر', f'fontp:{tok}:0:1')])
    elif extra and page == pages - 1:
        rows.append([('↩️ بازگشت به فونت‌های اصلی', f'fontp:{tok}:0:0')])

    rows.append([('✖️ بستن', 'close')])
    return caption, rows, tok


def resolve_selection(tok: str, style_id: int) -> str | None:
    text = _get(tok)
    if text is None:
        return None
    return apply_style(text, style_id)


def resolve_page(tok: str, page: int, extra: bool) -> tuple[str, list[list[tuple[str, str]]]] | None:
    text = _get(tok)
    if text is None:
        return None
    # refresh TTL
    _STORE[tok] = (text, time.time() + _TTL)
    cap, rows, _ = build_panel(text, page=page, extra=extra)
    return cap, rows
