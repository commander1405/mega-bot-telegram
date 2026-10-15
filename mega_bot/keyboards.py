from __future__ import annotations

# Main reply keyboard: four features formerly hidden under "منوی بیشتر" plus
# one entry to the secondary menu. Keep this as a persistent Telegram reply keyboard.
USER_KEYBOARD = {
    'keyboard': [
        [{'text': '🆘 پشتیبانی'}, {'text': '💚 حمایت مالی'}],
        [{'text': '👨‍💻 تیم توسعه'}, {'text': '📊 آمار و اطلاعات'}],
        [{'text': 'منوی بیشتر'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}


def inline(rows: list[list[tuple[str, str]]]) -> dict:
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


def url_button(label: str, url: str) -> dict:
    """Single-row inline keyboard with one URL button (Telegram-compatible)."""
    return {'inline_keyboard': [[{'text': label, 'url': url}]]}


def merge_inline(base: dict | None, extra_row: list[dict]) -> dict:
    """Append a row of button dicts to an existing inline keyboard without mutating base."""
    if not base or 'inline_keyboard' not in base:
        return {'inline_keyboard': [extra_row]}
    rows = [list(r) for r in base['inline_keyboard']]
    rows.append(extra_row)
    return {'inline_keyboard': rows}


def menu_page(items: list[tuple[str, str]], page: int, prefix: str, back: str = 'home', page_size: int = 3) -> dict:
    """At most 6 buttons per page including navigation and back/close controls."""
    start = page * page_size
    shown = items[start:start + page_size]
    rows = [[(label, data)] for label, data in shown]
    controls: list[tuple[str, str]] = []
    if page > 0:
        controls.append(('⬅️ قبلی', f'{prefix}:page:{page-1}'))
    if start + page_size < len(items):
        controls.append(('بعدی ➡️', f'{prefix}:page:{page+1}'))
    if controls:
        rows.append(controls[:2])
    rows.append([('↩️ برگشت', f'{prefix}:back:{back}'), ('✖️ بستن', 'close')])
    return inline(rows)


def main_admin_menu(owner: bool = False) -> dict:
    items = [
        ('📊 آمار و گزارش‌ها', 'adm:stats'),
        ('👤 جست‌وجوی کاربر', 'adm:search:user'),
        ('👥 جست‌وجوی گروه', 'adm:search:group'),
        ('🎫 بررسی کد پیگیری', 'adm:case'),
        ('🧾 گزارش فعالیت‌ها', 'adm:logs'),
    ]
    if owner:
        items += [
            ('🛡️ مدیریت ادمین‌ها', 'own:admins'),
            ('📝 ویرایش درباره ما', 'own:about'),
            ('👨‍💻 تیم توسعه‌دهنده', 'own:team'),
            ('⚙️ تنظیمات مالک', 'own:settings'),
        ]
    return menu_page(items, 0, 'admin', 'home')


def user_actions(target_type: str, target_id: int, can_ban: bool) -> dict:
    rows = []
    if can_ban:
        rows.append([('⛔ محدودسازی', f'mod:ban:{target_type}:{target_id}')])
    rows.append([('♻️ رفع محدودیت', f'mod:unban:{target_type}:{target_id}')])
    rows.append([('↩️ برگشت', f'adm:search:{target_type}'), ('✖️ بستن', 'close')])
    return inline(rows)
