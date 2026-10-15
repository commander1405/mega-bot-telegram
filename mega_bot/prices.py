"""Live market prices via public APIs (no API key required).

Primary source: https://api.priceto.day (free-market IRR rates from tgju.org).
BTC also cross-checked with CoinGecko when available.

Prices from priceto.day are in Iranian Rials; we convert to Toman (/10) for display.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

PRICETO_BASE = 'https://api.priceto.day/v1/latest/irr'
COINGECKO_BTC = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true'

# Short in-memory cache to avoid hammering free APIs
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 60.0  # seconds


def _rial_to_toman(rial: float | int) -> int:
    return int(round(float(rial) / 10))


def _fmt_toman(n: int) -> str:
    return f'{n:,}'


async def _fetch_json(url: str, timeout: float = 10.0) -> dict | None:
    try:
        to = aiohttp.ClientTimeout(total=timeout, connect=5)
        async with aiohttp.ClientSession(timeout=to) as session:
            async with session.get(url, headers={'User-Agent': 'MEGA-BOT/2.0'}) as resp:
                if resp.status >= 400:
                    log.warning('price fetch HTTP %s for %s', resp.status, url)
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning('price fetch error %s: %s', url, type(exc).__name__)
        return None


async def _priceto(symbol: str) -> dict | None:
    key = f'pt:{symbol}'
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    data = await _fetch_json(f'{PRICETO_BASE}/{symbol}')
    if not data or not data.get('success') or data.get('price') is None:
        return None
    _cache[key] = (now, data)
    return data


async def get_usd_toman() -> dict[str, Any] | None:
    """Free-market USD price in Toman."""
    data = await _priceto('usd')
    if not data:
        return None
    toman = _rial_to_toman(data['price'])
    return {
        'symbol': 'USD',
        'name': 'دلار آمریکا',
        'price_toman': toman,
        'price_display': _fmt_toman(toman),
        'source': 'api.priceto.day (بازار آزاد / tgju)',
        'updated_at': data.get('time') or '',
        'note': 'نرخ بازار آزاد؛ واحد: تومان',
    }


async def get_currencies() -> list[dict[str, Any]] | None:
    """Five selected currencies in Toman."""
    mapping = [
        ('usd', 'USD', 'دلار آمریکا'),
        ('euro', 'EUR', 'یورو'),
        ('gbp', 'GBP', 'پوند انگلیس'),
        ('aed', 'AED', 'درهم امارات'),
        ('try', 'TRY', 'لیر ترکیه'),
    ]
    results: list[dict[str, Any]] = []
    for sym, code, name in mapping:
        data = await _priceto(sym)
        if not data:
            continue
        toman = _rial_to_toman(data['price'])
        results.append({
            'symbol': code,
            'name': name,
            'price_toman': toman,
            'price_display': _fmt_toman(toman),
            'source': 'api.priceto.day',
            'updated_at': data.get('time') or '',
        })
    return results or None


async def get_bitcoin() -> dict[str, Any] | None:
    """BTC price in Toman (+ optional 24h change from CoinGecko USD)."""
    data = await _priceto('bitcoin')
    if not data:
        return None
    toman = _rial_to_toman(data['price'])
    change_24h = None
    try:
        cg = await _fetch_json(COINGECKO_BTC, timeout=8)
        if cg and 'bitcoin' in cg:
            change_24h = cg['bitcoin'].get('usd_24h_change')
    except Exception:
        pass
    out: dict[str, Any] = {
        'symbol': 'BTC',
        'name': 'بیت‌کوین',
        'price_toman': toman,
        'price_display': _fmt_toman(toman),
        'source': 'api.priceto.day',
        'updated_at': data.get('time') or '',
    }
    if change_24h is not None:
        out['change_24h'] = round(float(change_24h), 2)
        out['change_source'] = 'CoinGecko'
    return out


async def get_gold() -> dict[str, Any] | None:
    """Gold prices available from priceto.day (18k, 24k, selected coins)."""
    items: list[dict[str, Any]] = []
    for sym, label, unit in (
        ('gold-18k', 'طلای ۱۸ عیار', 'هر گرم (تومان)'),
        ('gold-24k', 'طلای ۲۴ عیار', 'هر گرم (تومان)'),
        ('coin-emami', 'سکه امامی', 'هر عدد (تومان)'),
        ('coin-baharazadi', 'سکه بهار آزادی', 'هر عدد (تومان)'),
    ):
        data = await _priceto(sym)
        if not data:
            continue
        toman = _rial_to_toman(data['price'])
        items.append({
            'label': label,
            'unit': unit,
            'price_toman': toman,
            'price_display': _fmt_toman(toman),
            'updated_at': data.get('time') or '',
        })
    if not items:
        return None
    return {
        'items': items,
        'source': 'api.priceto.day (بازار آزاد / tgju)',
        'note': 'قیمت‌ها از ریاال به تومان تبدیل شده‌اند (÷۱۰). واحد هر ردیف مشخص است.',
    }


def format_usd_message(d: dict[str, Any]) -> str:
    return (
        f"💵 *دلار آمریکا*\n"
        f"قیمت: `{d['price_display']}` تومان\n"
        f"نوع نرخ: بازار آزاد\n"
        f"منبع: {d['source']}\n"
        f"به‌روزرسانی: `{d['updated_at']}`"
    )


def format_currencies_message(rows: list[dict[str, Any]]) -> str:
    lines = ['💱 *قیمت ارزها (بازار آزاد)*\n']
    for r in rows:
        lines.append(f"• *{r['name']}* ({r['symbol']}): `{r['price_display']}` تومان")
    if rows:
        lines.append(f"\nمنبع: {rows[0]['source']}")
        lines.append(f"به‌روزرسانی: `{rows[0]['updated_at']}`")
    return '\n'.join(lines)


def format_btc_message(d: dict[str, Any]) -> str:
    ch = ''
    if d.get('change_24h') is not None:
        sign = '+' if d['change_24h'] >= 0 else ''
        ch = f"\nتغییر ۲۴س (USD / {d.get('change_source','')}): `{sign}{d['change_24h']}%`"
    return (
        f"₿ *بیت‌کوین (BTC)*\n"
        f"قیمت: `{d['price_display']}` تومان{ch}\n"
        f"منبع قیمت تومان: {d['source']}\n"
        f"به‌روزرسانی: `{d['updated_at']}`"
    )


def format_gold_message(d: dict[str, Any]) -> str:
    lines = ['🥇 *قیمت طلا و سکه*\n']
    for it in d['items']:
        lines.append(f"• *{it['label']}*: `{it['price_display']}` — {it['unit']}")
    lines.append(f"\nمنبع: {d['source']}")
    if d['items']:
        lines.append(f"به‌روزرسانی: `{d['items'][0]['updated_at']}`")
    lines.append(f"\n_{d.get('note', '')}_")
    return '\n'.join(lines)
