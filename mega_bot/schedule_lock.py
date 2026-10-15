"""Scheduled group-lock helpers: time parsing and window evaluation (Asia/Tehran)."""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TZ = 'Asia/Tehran'

# Python weekday: Mon=0 ... Sun=6
DAY_ALIASES: dict[str, int] = {
    'شنبه': 5, 'شنیه': 5,
    'یکشنبه': 6, 'یک شنبه': 6,
    'دوشنبه': 0, 'دو شنبه': 0,
    'سهشنبه': 1, 'سه‌شنبه': 1, 'سه شنبه': 1,
    'چهارشنبه': 2, 'چهار شنبه': 2,
    'پنجشنبه': 3, 'پنج‌شنبه': 3, 'پنج شنبه': 3,
    'جمعه': 4,
}
DAY_LABELS = {
    5: 'شنبه', 6: 'یکشنبه', 0: 'دوشنبه', 1: 'سه‌شنبه',
    2: 'چهارشنبه', 3: 'پنجشنبه', 4: 'جمعه',
}
ALL_DAYS = [5, 6, 0, 1, 2, 3, 4]

FA_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')

# Locked / open default member permissions (admins keep rights via Telegram rules)
PERMS_LOCKED = {
    'can_send_messages': False,
    'can_send_media_messages': False,
    'can_send_polls': False,
    'can_send_other_messages': False,
    'can_add_web_page_previews': False,
    'can_change_info': False,
    'can_invite_users': False,
    'can_pin_messages': False,
}
PERMS_OPEN = {
    'can_send_messages': True,
    'can_send_media_messages': True,
    'can_send_polls': True,
    'can_send_other_messages': True,
    'can_add_web_page_previews': True,
    'can_change_info': False,
    'can_invite_users': True,
    'can_pin_messages': False,
}


def fa_to_en_digits(s: str) -> str:
    return (s or '').translate(FA_DIGITS)


def normalize_input(text: str) -> str:
    t = fa_to_en_digits(text or '')
    t = t.replace('‌', ' ').replace('ـ', ' ')
    t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ي', 'ی').replace('ك', 'ک')
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def parse_hm(token: str) -> str | None:
    """Return HH:MM or None."""
    token = fa_to_en_digits(token).strip()
    m = re.fullmatch(r'(\d{1,2})[:\.٫](\d{1,2})', token)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f'{h:02d}:{mi:02d}'


def hm_to_minutes(hm: str) -> int:
    h, m = hm.split(':')
    return int(h) * 60 + int(m)


def parse_schedule_text(text: str, *, today: date | None = None) -> dict[str, Any]:
    """Parse admin schedule line.

    Returns dict with start_hm, end_hm, days (list[int]), valid_from, valid_until
    or raises ValueError with Persian message.
    """
    raw = normalize_input(text)
    if not raw:
        raise ValueError(
            '❌ ورودی خالی است.\nمثال: `17:00 21:00 هر روز`'
        )
    # Allow optional «تا» / «-» between times
    raw = re.sub(r'\s+تا\s+', ' ', raw)
    raw = re.sub(r'(?<=\d)\s*-\s*(?=\d)', ' ', raw)

    # Extract two HH:MM tokens
    times = re.findall(r'\d{1,2}[:\.٫]\d{1,2}', raw)
    if len(times) < 2:
        raise ValueError(
            '❌ ساعت شروع و پایان را مشخص کن.\n'
            'مثال: `17:00 21:00 هر روز` یا `۱۷:۰۰ تا ۲۱:۰۰ دو روز`'
        )
    start_hm = parse_hm(times[0])
    end_hm = parse_hm(times[1])
    if not start_hm or not end_hm:
        raise ValueError('❌ ساعت نامعتبر است. بازه معتبر: 00:00 تا 23:59')
    if start_hm == end_hm:
        raise ValueError('❌ ساعت شروع و پایان نباید یکسان باشد.')

    # Remainder after removing time tokens
    rest = raw
    for t in times[:2]:
        rest = rest.replace(t, ' ', 1)
    rest = re.sub(r'\s+', ' ', rest).strip(' ,،')

    today = today or datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    days: list[int] = []
    valid_from: date | None = None
    valid_until: date | None = None

    if not rest or rest in ('هر روز', 'همه روز', 'همه روزها', 'روزانه'):
        days = list(ALL_DAYS)
    elif re.fullmatch(r'(یک|1|١) روز', rest) or rest in ('یک روز', '1 روز'):
        days = list(ALL_DAYS)
        valid_from = today
        valid_until = today
    else:
        # N days: دو روز، سه روز، ...
        m = re.fullmatch(r'(\d+)\s*روز', rest)
        fa_n = {
            'یک': 1, 'دو': 2, 'سه': 3, 'چهار': 4, 'پنج': 5,
            'شش': 6, 'هفت': 7, 'هشت': 8, 'نه': 9, 'ده': 10,
            'یازده': 11, 'دوازده': 12, 'سی': 30,
        }
        if not m:
            for word, n in fa_n.items():
                if rest in (f'{word} روز', f'{word}روز'):
                    m = type('M', (), {'group': lambda self, i, n=n: str(n)})()
                    break
        if m:
            n = int(m.group(1))
            if n < 1 or n > 366:
                raise ValueError('❌ تعداد روز باید بین ۱ تا ۳۶۶ باشد.')
            days = list(ALL_DAYS)
            valid_from = today
            valid_until = today + timedelta(days=n - 1)
        else:
            # Named weekdays, comma separated
            parts = re.split(r'[،,]\s*|\s+و\s+|\s+', rest)
            parts = [p for p in parts if p]
            for p in parts:
                key = p.strip()
                if key in DAY_ALIASES:
                    days.append(DAY_ALIASES[key])
                elif key in ('هر', 'روز'):
                    continue
                else:
                    # try without spaces
                    k2 = key.replace(' ', '')
                    found = None
                    for name, d in DAY_ALIASES.items():
                        if name.replace(' ', '') == k2:
                            found = d
                            break
                    if found is None:
                        raise ValueError(
                            f'❌ روز «{key}» شناخته نشد.\n'
                            'روزهای معتبر: شنبه، یکشنبه، دوشنبه، سه‌شنبه، چهارشنبه، پنجشنبه، جمعه، هر روز'
                        )
                    days.append(found)
            days = sorted(set(days), key=lambda d: ALL_DAYS.index(d) if d in ALL_DAYS else d)
            if not days:
                raise ValueError('❌ هیچ روز معتبری انتخاب نشد.')

    return {
        'start_hm': start_hm,
        'end_hm': end_hm,
        'days': days,
        'valid_from': valid_from,
        'valid_until': valid_until,
        'timezone': DEFAULT_TZ,
    }


def days_label(days: list) -> str:
    try:
        days = [int(d) for d in days]
    except Exception:
        days = []
    if not days or set(days) >= set(ALL_DAYS):
        return 'هر روز'
    return '، '.join(DAY_LABELS.get(int(d), str(d)) for d in days if int(d) in DAY_LABELS)


def schedule_should_lock(
    *,
    now: datetime,
    start_hm: str,
    end_hm: str,
    days: list,
    valid_from: date | None,
    valid_until: date | None,
) -> bool:
    """Whether the group should be locked at `now` (timezone-aware preferred)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
    local_date = now.date()
    if valid_from and local_date < valid_from:
        return False
    if valid_until and local_date > valid_until:
        return False

    try:
        days_set = {int(d) for d in (days or [])}
    except Exception:
        days_set = set()
    if not days_set:
        days_set = set(ALL_DAYS)

    cur_min = now.hour * 60 + now.minute
    start_m = hm_to_minutes(start_hm)
    end_m = hm_to_minutes(end_hm)
    wd = now.weekday()  # Mon=0
    yest = (now - timedelta(days=1)).weekday()

    if start_m < end_m:
        # same-day window
        if wd not in days_set:
            return False
        return start_m <= cur_min < end_m
    # overnight: lock from start to midnight on listed days, and midnight to end on next day
    if cur_min >= start_m and wd in days_set:
        return True
    if cur_min < end_m and yest in days_set:
        # also respect valid_until on the previous calendar day for overnight tail
        prev_date = local_date - timedelta(days=1)
        if valid_from and prev_date < valid_from:
            return False
        if valid_until and prev_date > valid_until:
            return False
        return True
    return False


def format_schedule_row(s: dict) -> str:
    days = s.get('days') or []
    if isinstance(days, str):
        try:
            import json
            days = json.loads(days)
        except Exception:
            days = []
    en = '🟢' if s.get('enabled', True) else '🔴'
    active = ' 🔒' if s.get('is_active_now') else ''
    rng = ''
    vf, vu = s.get('valid_from'), s.get('valid_until')
    if vf or vu:
        rng = f" | اعتبار: {vf or '…'} تا {vu or '…'}"
    return (
        f"{en}#{s.get('id')} {s.get('start_hm')}→{s.get('end_hm')} "
        f"{days_label(days)}{rng}{active}"
    )
