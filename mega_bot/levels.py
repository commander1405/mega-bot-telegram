"""Group-scoped activity level / XP helpers (pure logic, no I/O)."""
from __future__ import annotations

import math
from typing import Any

MAX_LEVEL = 200
# Economy: every N valid messages → 1 XP
MESSAGES_PER_XP = 2
XP_PER_GRANT = 1
# Spam: if user sends this many messages within the window, grant no XP
SPAM_WINDOW_SEC = 60.0
SPAM_MSG_LIMIT = 80  # ~80+/min treated as spam (covers 100–500/min cases)
MIN_TEXT_LEN = 1
# Legacy aliases kept so older call sites do not break during deploy
XP_PER_MESSAGE = XP_PER_GRANT
XP_COOLDOWN_SEC = 0.0


def xp_to_next(level: int) -> int:
    """XP required to go from `level` to `level + 1`."""
    if level < 1:
        level = 1
    if level >= MAX_LEVEL:
        return 0
    return int(round(100 * (level ** 1.8)))


def cumulative_xp_for_level(level: int) -> int:
    """Total XP needed to *reach* `level` (starting from level 1 at 0 XP)."""
    if level <= 1:
        return 0
    level = min(level, MAX_LEVEL)
    return sum(xp_to_next(L) for L in range(1, level))


def level_from_xp(total_xp: int) -> int:
    """Compute level from total XP (1 .. MAX_LEVEL)."""
    xp = max(0, int(total_xp or 0))
    if xp <= 0:
        return 1
    # Binary search level
    lo, hi = 1, MAX_LEVEL
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cumulative_xp_for_level(mid) <= xp:
            lo = mid
        else:
            hi = mid - 1
    return lo


def progress_in_level(total_xp: int) -> dict[str, Any]:
    """Return level, xp into current level, need for next, percent, remaining."""
    total_xp = max(0, int(total_xp or 0))
    level = level_from_xp(total_xp)
    if level >= MAX_LEVEL:
        return {
            'level': MAX_LEVEL,
            'xp_in_level': 0,
            'xp_need': 0,
            'percent': 100.0,
            'remaining': 0,
            'total_xp': total_xp,
            'maxed': True,
        }
    base = cumulative_xp_for_level(level)
    need = xp_to_next(level)
    into = total_xp - base
    if need <= 0:
        pct = 100.0
        rem = 0
    else:
        into = max(0, min(into, need))
        pct = (into / need) * 100.0
        rem = max(0, need - into)
    return {
        'level': level,
        'xp_in_level': into,
        'xp_need': need,
        'percent': pct,
        'remaining': rem,
        'total_xp': total_xp,
        'maxed': False,
    }


def progress_bar(percent: float, width: int = 12) -> str:
    """Unicode progress bar proportional to percent (0-100)."""
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return '█' * filled + '░' * (width - filled)


def apply_xp(total_xp: int, gain: int) -> tuple[int, int, int]:
    """Apply XP gain. Returns (new_total_xp, old_level, new_level). Caps at max level XP."""
    old = max(0, int(total_xp or 0))
    old_lv = level_from_xp(old)
    if old_lv >= MAX_LEVEL:
        return old, old_lv, old_lv
    cap = cumulative_xp_for_level(MAX_LEVEL)
    new = min(cap, old + max(0, int(gain)))
    new_lv = level_from_xp(new)
    return new, old_lv, new_lv


def is_valid_activity_message(text: str, msg: dict | None = None) -> bool:
    """Whether a message should grant XP (non-empty text or any media)."""
    msg = msg or {}
    t = (text or '').strip()
    if len(t) >= MIN_TEXT_LEN:
        return True
    for key in ('photo', 'video', 'animation', 'document', 'audio', 'voice', 'sticker', 'video_note'):
        if msg.get(key):
            return True
    return False
