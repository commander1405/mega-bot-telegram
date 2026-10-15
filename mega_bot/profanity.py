"""Multi-layer profanity filter (Persian + English). Best-effort, not exhaustive.

Limitations (documented intentionally):
- Cannot catch every insult in every language/dialect/obfuscation.
- False positives are reduced via word-boundary style matching after normalization.
- Group admins can extend the word list per group via DB.
"""
from __future__ import annotations

import re

# Base list — keep conservative; groups can add more via DB.
_BASE_FA = [
    'کیر', 'کس', 'کون', 'جنده', 'حرامزاده', 'حرومزاده', 'مادرجنده',
    'گایید', 'میگام', 'بگا', 'اشغال', 'لاشی', 'دیوث', 'کونده',
]
_BASE_EN = [
    'fuck', 'shit', 'bitch', 'asshole', 'dick', 'pussy', 'bastard',
    'motherfucker', 'wtf', 'stfu',
]

_LEET = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's',
})


def normalize_text(text: str) -> str:
    t = (text or '').lower()
    t = t.translate(_LEET)
    # Arabic/Persian variants
    t = t.replace('ي', 'ی').replace('ك', 'ک').replace('ة', 'ه').replace('ؤ', 'و').replace('إ', 'ا').replace('أ', 'ا').replace('آ', 'ا')
    t = t.replace('‌', '').replace('ـ', '')  # ZWNJ and tatweel
    # Collapse repeated letters (کسسسس → کس)
    t = re.sub(r'(.)\1{2,}', r'\1\1', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def build_pattern(words: list[str]) -> re.Pattern | None:
    cleaned = []
    for w in words:
        w = normalize_text(w)
        if len(w) < 2:
            continue
        cleaned.append(re.escape(w))
    if not cleaned:
        return None
    # Word-ish boundaries for Latin; loose for Persian
    body = '|'.join(cleaned)
    return re.compile(rf'(?:^|[\s\W_])({body})(?:$|[\s\W_])', re.IGNORECASE)


_BASE_PATTERN = build_pattern(_BASE_FA + _BASE_EN)


def contains_profanity(text: str, extra_words: list[str] | None = None) -> bool:
    if not text or not text.strip():
        return False
    norm = normalize_text(text)
    if _BASE_PATTERN and _BASE_PATTERN.search(f' {norm} '):
        return True
    if extra_words:
        pat = build_pattern(extra_words)
        if pat and pat.search(f' {norm} '):
            return True
    return False
