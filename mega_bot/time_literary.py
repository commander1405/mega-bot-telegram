"""Literary Tehran time / date for MEGA BOT.

Timezone: Asia/Tehran
Day parts (Tehran local hour):
  midnight  00:00–04:59
  morning   05:00–11:59
  noon      12:00–14:59
  afternoon 15:00–18:59
  night     19:00–23:59

Weekend in Iran: Thursday (4) and Friday (5) — weekday() Mon=0 … Sun=6
  so Thursday=3, Friday=4 in Python weekday().
"""
from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo('Asia/Tehran')

# Python weekday: Mon=0 … Sun=6 → map to Persian names starting Saturday
_WEEKDAY_FA = {
    0: 'دوشنبه',
    1: 'سه‌شنبه',
    2: 'چهارشنبه',
    3: 'پنج‌شنبه',
    4: 'جمعه',
    5: 'شنبه',
    6: 'یکشنبه',
}

_FA_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')

# Closing lines by day-part (and weekend variants). 20+ total.
_CLOSINGS: dict[str, list[str]] = {
    'midnight': [
        '🌌 نیمه‌شب است؛ جهان آرام خوابیده و تنها ستاره‌ها بیدارند.\nباشد که رؤیاهایت روشن باشند. 💚',
        '🕯️ در سکوتِ نیمه‌شب، زمان آهسته‌تر قدم برمی‌دارد.\nلحظه‌ات آرام و امن باشد. 💚',
        '✨ این ساعتِ راز و نیاز است.\nخدا نگهدارِ دلِ بیدارِ تو. 💚',
    ],
    'morning': [
        '🌅 صبح از راه رسید و نور، صفحهٔ تازه‌ای گشود.\nروزت سرشار از انرژیِ خوب باشد. 💚',
        '☀️ آفتاب سلام کرد؛ تو هم به خودت لبخند بزن.\nصبح بخیر، ای مسافرِ زمان. 💚',
        '🌿 بوی نان تازه و امید در هواست.\nاین صبح، مالِ توست. 💚',
        '🌸 پرنده‌ها خواندند و تقویم ورق خورد.\nروزت مبارک. 💚',
    ],
    'noon': [
        '🌞 ظهر است؛ آفتاب در میانهٔ آسمان ایستاده.\nنفسی تازه کن و ادامه بده. 💚',
        '🍵 نیم‌روزِ خوبی باشد؛ میانِ تلاش و آرامش.\nقوت قلبت پایدار. 💚',
        '⛅ ساعت از نیمهٔ روز گذشت.\nامید که مسیرت روشن بماند. 💚',
    ],
    'afternoon': [
        '🌇 عصر آمد و سایه‌ها کشیده‌تر شدند.\nآرام‌آرام به سوی غروب برو. 💚',
        '🍃 بادِ عصرگاهی ورق‌های زمان را ورق می‌زند.\nدلت سبک باشد. 💚',
        '📖 عصر، فصلِ کوتاهِ میانِ روز و شب است.\nلحظه را دریاب. 💚',
    ],
    'night': [
        '🌙 شب از راه رسید و شهر چراغانی شد.\nشبت آرام و دلت گرم. 💚',
        '⭐ ستاره‌ها دوباره سر خط آمدند.\nشبِ خوشی برایت آرزو می‌کنم. 💚',
        '🌃 پایانِ روز، آغازِ قصه‌ای دیگر است.\nآرام بخواب، ای دوست. 💚',
        '💫 شب، دفترِ خاطرات را می‌بندد.\nباشد که فردا زیباتر باشد. 💚',
    ],
    'weekend_morning': [
        '🌼 صبحِ آخر هفته است؛ عجله ممنوع.\nآخر هفتهٔ خوبی برات آرزو می‌کنم. 💚',
        '☕ پنج‌شنبه/جمعهٔ مبارک؛ روز برای خودت است.\nآرام شروع کن. 💚',
    ],
    'weekend_afternoon': [
        '🌈 عصرِ آخر هفته؛ وقتِ کمی رها شدن.\nآخر هفتهٔ خوشی داشته باشی. 💚',
        '🍃 روزِ تعطیلِ خودمانی؛ نفس بکش.\nلحظه‌ات قشنگ باشد. 💚',
    ],
    'weekend_night': [
        '🌙 شبِ آخر هفته؛ صدای خنده از دور می‌آید.\nآخر هفتهٔ خوبی برات آرزو می‌کنم. 💚',
        '✨ جمعه/پنج‌شنبه شب؛ دفتر هفته تقریباً بسته است.\nآرام باش و لذت ببر. 💚',
    ],
}

_last_closing: str | None = None


def _fa_num(s: str | int) -> str:
    return str(s).translate(_FA_DIGITS)


def _jalali(dt: datetime) -> tuple[int, int, int]:
    """Gregorian datetime → (jy, jm, jd) approximate algorithm."""
    gy, gm, gd = dt.year, dt.month, dt.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


_JMONTH = [
    '', 'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
    'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند',
]


def day_part(hour: int) -> str:
    """Documented Tehran day-part boundaries."""
    if 0 <= hour < 5:
        return 'midnight'
    if 5 <= hour < 12:
        return 'morning'
    if 12 <= hour < 15:
        return 'noon'
    if 15 <= hour < 19:
        return 'afternoon'
    return 'night'


def is_iran_weekend(weekday: int) -> bool:
    """Python weekday: Thursday=3, Friday=4."""
    return weekday in (3, 4)


def _pick_closing(part: str, weekend: bool) -> str:
    global _last_closing
    pool: list[str] = []
    if weekend:
        if part in ('morning', 'noon'):
            pool = list(_CLOSINGS['weekend_morning'])
        elif part == 'afternoon':
            pool = list(_CLOSINGS['weekend_afternoon'])
        else:
            pool = list(_CLOSINGS['weekend_night'])
        pool.extend(_CLOSINGS.get(part, []))
    else:
        pool = list(_CLOSINGS.get(part, _CLOSINGS['night']))
    choices = [c for c in pool if c != _last_closing] or pool
    chosen = random.choice(choices)
    _last_closing = chosen
    return chosen


def format_time_story(now: datetime | None = None) -> str:
    """Build the literary time message for Asia/Tehran."""
    try:
        now = now or datetime.now(TEHRAN)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TEHRAN)
        else:
            now = now.astimezone(TEHRAN)
    except Exception:
        now = datetime.utcnow()
        # fallback without zone
        pass

    try:
        jy, jm, jd = _jalali(now)
        jmonth = _JMONTH[jm] if 1 <= jm <= 12 else str(jm)
        weekday = _WEEKDAY_FA.get(now.weekday(), '')
        shamsi = f'{weekday} {_fa_num(jd)} {jmonth} {_fa_num(jy)}'
    except Exception:
        shamsi = '—'

    try:
        miladi = now.strftime('%Y/%m/%d')
        clock = now.strftime('%H:%M:%S')
        # Persian digits for clock optionally — keep western for readability of sample
        clock_fa = _fa_num(clock)
    except Exception:
        miladi = '—'
        clock_fa = '—'

    try:
        part = day_part(now.hour)
        weekend = is_iran_weekend(now.weekday())
        closing = _pick_closing(part, weekend)
    except Exception:
        closing = '💚'

    return (
        '📜 *حکایتِ زمان*\n\n'
        'روزی که تقویم ورق می‌خورد، چنین نوشت:\n\n'
        f'« {shamsi} »\n\n'
        'و ساعت، با صدای خفیفی زمزمه کرد:\n\n'
        f'« {clock_fa} »\n\n'
        f'📅 تاریخ میلادی: `{miladi}`\n\n'
        '━━━━━━━━━━━━━━━\n\n'
        f'{closing}'
    )
