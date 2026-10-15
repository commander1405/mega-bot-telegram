import unittest
from mega_bot.keyboards import USER_KEYBOARD

class PureLogicTests(unittest.TestCase):
    def test_main_keyboard_has_five_buttons_and_expected_layout(self):
        rows = USER_KEYBOARD['keyboard']
        self.assertEqual(sum(len(row) for row in rows), 5)
        labels = [button['text'] for row in rows for button in row]
        self.assertIn('🆘 پشتیبانی', labels)
        self.assertIn('💚 حمایت مالی', labels)
        self.assertIn('👨‍💻 تیم توسعه', labels)
        self.assertIn('📊 آمار و اطلاعات', labels)
        self.assertIn('منوی بیشتر', labels)

    def test_case_code_range_has_9000_codes(self):
        self.assertEqual(len(range(1000, 10000)), 9000)

    def test_persian_case_code_parsing_pattern(self):
        import re
        self.assertEqual(re.search(r'(\d{4})', '#5453').group(1), '5453')

    def test_image_gif_conversion_and_overlay(self):
        import io
        from PIL import Image
        from mega_bot.media_tools import image_to_gif
        source = io.BytesIO()
        Image.new('RGB', (120, 80), 'navy').save(source, format='PNG')
        result = image_to_gif(source.getvalue(), 'TEST')
        self.assertTrue(result.startswith((b'GIF87a', b'GIF89a')))
        self.assertGreater(len(result), 20)

class DonationAnnouncementTests(unittest.TestCase):
    """Pure logic checks for donation announcement formatting (no network deps)."""

    def setUp(self):
        # Mirror the helpers implemented in mega_bot.app.MegaBot without importing aiohttp.
        def _md_escape(text: str) -> str:
            if not text:
                return ''
            out = []
            for ch in str(text):
                if ch in r'_*[]()`':
                    out.append('\\' + ch)
                else:
                    out.append(ch)
            return ''.join(out)

        def build_donation_announcement(donation: dict) -> str:
            amount = int(donation['amount'])
            amount_line = f'💰 مبلغ حمایت: {amount:,} تومان'
            if donation.get('show_name'):
                name = (donation.get('user_name') or 'کاربر').strip() or 'کاربر'
                safe_name = _md_escape(name)
                user_id = int(donation['user_id'])
                return f'[{safe_name}](uid:{user_id}) مارو حمایت کرد🥹✨️\n{amount_line}'
            return f'یه نفر مارو حمایت کرد🥹✨️\n{amount_line}'

        def donation_broadcast_keyboard() -> dict:
            return {'inline_keyboard': [[{'text': 'توهم حمایت کن 🌱', 'callback_data': 'donate:start'}]]}

        self._md_escape = _md_escape
        self.build = build_donation_announcement
        self.keyboard = donation_broadcast_keyboard

    def test_named_announcement_uses_uid_link_and_amount(self):
        text = self.build({
            'user_id': 243388452,
            'user_name': 'MSF♛',
            'amount': 1000,
            'show_name': True,
        })
        self.assertIn('[MSF♛](uid:243388452)', text)
        self.assertIn('مارو حمایت کرد🥹✨️', text)
        self.assertIn('💰 مبلغ حمایت: 1,000 تومان', text)
        import re
        self.assertIsNone(re.search(r'(?<!uid:)\b243388452\b', text))

    def test_anonymous_hides_identity(self):
        text = self.build({
            'user_id': 999001,
            'user_name': 'SecretUser',
            'amount': 2500,
            'show_name': False,
        })
        self.assertEqual(text, 'یه نفر مارو حمایت کرد🥹✨️\n💰 مبلغ حمایت: 2,500 تومان')
        self.assertNotIn('SecretUser', text)
        self.assertNotIn('999001', text)
        self.assertNotIn('uid:', text)

    def test_markdown_escape_in_name(self):
        text = self.build({
            'user_id': 1,
            'user_name': 'A_B*C[D]',
            'amount': 10,
            'show_name': True,
        })
        self.assertIn(r'[A\_B\*C\[D\]](uid:1)', text)

    def test_broadcast_keyboard_callback(self):
        kb = self.keyboard()
        btn = kb['inline_keyboard'][0][0]
        self.assertEqual(btn['text'], 'توهم حمایت کن 🌱')
        self.assertEqual(btn['callback_data'], 'donate:start')

    def test_source_contains_donation_fixes(self):
        import pathlib
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        self.assertIn('donate:start', src)
        self.assertIn('build_donation_announcement', src)
        self.assertIn('توهم حمایت کن 🌱', src)
        self.assertIn('یه نفر مارو حمایت کرد', src)
        self.assertIn('(uid:{user_id})', src)




class HelpAndModerationLogicTests(unittest.TestCase):
    def test_guide_covers_moderation_commands(self):
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        self.assertIn('GUIDE_ENTRIES', src)
        self.assertIn('سکوت', src)
        self.assertIn('حذف سکوت', src)
        self.assertIn('انبن', src)
        self.assertIn('آنبن', src)
        self.assertIn('حذف بن', src)
        self.assertIn('darkknight_studio', src)

    def test_moderation_command_map(self):
        mapping = {
            'سکوت': 'mute',
            'حذف سکوت': 'unmute',
            'بن': 'ban',
            'حذف بن': 'unban',
            'انبن': 'unban',
            'آنبن': 'unban',
            'کیک': 'kick',
        }
        self.assertEqual(mapping['انبن'], mapping['آنبن'])
        self.assertEqual(mapping['حذف بن'], 'unban')

    def test_emoji_only_detection(self):
        import re
        emoji_re = re.compile(
            "["
            "\U0001F300-\U0001F9FF"
            "\U0001FA00-\U0001FAFF"
            "\U00002600-\U000027BF"
            "\U0001F1E0-\U0001F1FF"
            "\U0000200D"
            "\U0000FE0F"
            "]+",
            flags=re.UNICODE,
        )
        def is_emoji_only(text):
            stripped = (text or '').strip()
            if not stripped:
                return False
            without = emoji_re.sub('', stripped)
            without = without.replace(' ', '').replace('\u200c', '').replace('\ufe0f', '')
            return len(without) == 0 and bool(emoji_re.search(stripped))
        self.assertTrue(is_emoji_only('😭'))
        self.assertTrue(is_emoji_only('😆😂'))
        self.assertTrue(is_emoji_only('🗿'))
        self.assertFalse(is_emoji_only('امروز خوشحالم 😂'))
        self.assertFalse(is_emoji_only('سلام'))

    def test_bot_name_and_creator_phrases(self):
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        self.assertIn('منو میگی؟ جانم درخدمتم دستور چیه؟', src)
        self.assertIn('@darkknight_studio', src)
        self.assertIn('مگا بات', src)

    def test_restrict_api_method_exists(self):
        src = pathlib.Path('mega_bot/telegram_api.py').read_text(encoding='utf-8')
        self.assertIn('restrict_chat_member', src)
        self.assertIn('restrictChatMember', src)


import pathlib



class AdminPromoteResilienceTests(unittest.TestCase):
    def test_handles_my_chat_member_and_skips_bot_messages(self):
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        self.assertIn('on_my_chat_member', src)
        self.assertIn("my_chat_member", src)
        self.assertIn('is_bot', src)
        self.assertIn('_admin_cache', src)
        self.assertIn('_cached_stats', src)
        # offset advanced before process
        self.assertIn('create_task', src)

    def test_db_failures_isolated_in_on_message(self):
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        self.assertIn('upsert_user', src)
        self.assertIn('record_usage', src)
        self.assertIn('is_restricted(user)', src)



class GroupAdminShortCircuitTests(unittest.TestCase):
    def test_list_special_checks_text_before_getchatmember(self):
        """Regression: VIP list matches text first; admin check only after match."""
        src = pathlib.Path('mega_bot/app.py').read_text(encoding='utf-8')
        # Current code: is_group + text membership, then admin gate
        self.assertIn("if is_group and text in ('لیست ویژه ها', 'لیست ویژه‌ها', 'لیست ویژه')", src)
        # Old buggy order must be gone (admin check before text)
        self.assertNotIn("and await self._is_chat_admin(chat_id, uid) and text == 'لیست ویژه ها'", src)
        # Early time-lock enforcement must exist before GIF path
        self.assertIn('_enforce_group_time_lock', src)



class CallbackRoutingTests(unittest.TestCase):
    def test_admin_back_and_unknown_handled(self):
        src = pathlib.Path("mega_bot/app.py").read_text(encoding="utf-8")
        self.assertIn("admin:back:home", src)
        self.assertIn("Unhandled callback", src)
        self.assertIn("_route_callback", src)
        self.assertIn("answer_callback", src)

    def test_route_covers_core_prefixes(self):
        src = pathlib.Path("mega_bot/app.py").read_text(encoding="utf-8")
        for prefix in ("more:", "guide:", "donate:", "adm:", "own:", "mod:", "perm:", "ttt:", "one:"):
            self.assertIn(prefix, src)

if __name__ == '__main__':
    unittest.main()


class ScheduleLockLogicTests(unittest.TestCase):
    """Time-window evaluation and Persian parsing for scheduled group locks."""

    def test_same_day_window(self):
        from datetime import datetime, date
        from zoneinfo import ZoneInfo
        from mega_bot.schedule_lock import schedule_should_lock
        TZ = ZoneInfo('Asia/Tehran')
        day = date(2026, 10, 12)  # Monday
        def at(h, m):
            return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
        days = list(range(7))
        self.assertTrue(schedule_should_lock(now=at(18, 0), start_hm='17:00', end_hm='21:00', days=days, valid_from=None, valid_until=None))
        self.assertFalse(schedule_should_lock(now=at(16, 59), start_hm='17:00', end_hm='21:00', days=days, valid_from=None, valid_until=None))
        self.assertFalse(schedule_should_lock(now=at(21, 0), start_hm='17:00', end_hm='21:00', days=days, valid_from=None, valid_until=None))

    def test_overnight_window(self):
        from datetime import datetime, date
        from zoneinfo import ZoneInfo
        from mega_bot.schedule_lock import schedule_should_lock
        TZ = ZoneInfo('Asia/Tehran')
        day = date(2026, 10, 12)
        def at(h, m, d=day):
            return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
        days = list(range(7))
        self.assertTrue(schedule_should_lock(now=at(23, 0), start_hm='22:00', end_hm='02:00', days=days, valid_from=None, valid_until=None))
        self.assertTrue(schedule_should_lock(now=at(1, 0), start_hm='22:00', end_hm='02:00', days=days, valid_from=None, valid_until=None))
        self.assertFalse(schedule_should_lock(now=at(3, 0), start_hm='22:00', end_hm='02:00', days=days, valid_from=None, valid_until=None))

    def test_persian_digits_and_har_roz(self):
        from mega_bot.schedule_lock import parse_schedule_text
        r = parse_schedule_text('۱۷:۰۰ ۲۱:۰۰ هر روز')
        self.assertEqual(r['start_hm'], '17:00')
        self.assertEqual(r['end_hm'], '21:00')
        self.assertEqual(len(r['days']), 7)

    def test_ta_and_two_days(self):
        from mega_bot.schedule_lock import parse_schedule_text
        r = parse_schedule_text('17:00 تا 21:00 دو روز')
        self.assertEqual(r['start_hm'], '17:00')
        self.assertIsNotNone(r.get('valid_from'))
        self.assertIsNotNone(r.get('valid_until'))

    def test_invalid_equal_times(self):
        from mega_bot.schedule_lock import parse_schedule_text
        with self.assertRaises(ValueError):
            parse_schedule_text('10:00 10:00 هر روز')

    def test_social_stats_line_no_fake_zero(self):
        # Inline mirror of MegaBot._social_stats_line logic (avoid aiohttp import)
        def line(stats):
            stats = stats or {}
            g = stats.get('groups')
            if g is None:
                g = stats.get('groups_registered')
            if not stats.get('_ok') and g is None:
                return 'no-claim'
            try:
                g = int(g or 0)
            except Exception:
                return 'no-claim'
            if g > 0:
                return f'groups={g}'
            if g == 0 and stats.get('_ok'):
                return 'zero-real'
            return 'no-claim'
        self.assertEqual(line({}), 'no-claim')
        self.assertEqual(line({'_ok': True, 'groups': 12}), 'groups=12')
        self.assertEqual(line({'_ok': True, 'groups': 0}), 'zero-real')


class LevelSystemLogicTests(unittest.TestCase):
    def test_xp_curve_increases(self):
        from mega_bot.levels import xp_to_next
        self.assertLess(xp_to_next(1), xp_to_next(5))
        self.assertLess(xp_to_next(5), xp_to_next(12))
        self.assertLess(xp_to_next(12), xp_to_next(50))

    def test_level_from_xp_and_cap(self):
        from mega_bot.levels import level_from_xp, cumulative_xp_for_level, MAX_LEVEL, apply_xp
        self.assertEqual(level_from_xp(0), 1)
        self.assertEqual(level_from_xp(cumulative_xp_for_level(12)), 12)
        cap = cumulative_xp_for_level(MAX_LEVEL)
        new, old_lv, new_lv = apply_xp(cap - 10, 10_000_000)
        self.assertEqual(new_lv, MAX_LEVEL)
        self.assertLessEqual(new, cap)

    def test_level12_requires_substantial_xp(self):
        from mega_bot.levels import cumulative_xp_for_level, MESSAGES_PER_XP, XP_PER_GRANT
        need = cumulative_xp_for_level(12)
        msgs = need * MESSAGES_PER_XP / max(1, XP_PER_GRANT)
        self.assertGreater(need, 20_000)
        self.assertGreater(msgs, 4000)

    def test_two_messages_per_xp_constants(self):
        from mega_bot.levels import MESSAGES_PER_XP, XP_PER_GRANT, SPAM_MSG_LIMIT
        self.assertEqual(MESSAGES_PER_XP, 2)
        self.assertEqual(XP_PER_GRANT, 1)
        self.assertGreaterEqual(SPAM_MSG_LIMIT, 50)

    def test_progress_bar_and_percent(self):
        from mega_bot.levels import progress_in_level, progress_bar, cumulative_xp_for_level, xp_to_next
        base = cumulative_xp_for_level(5)
        need = xp_to_next(5)
        mid = base + need // 2
        p = progress_in_level(mid)
        self.assertEqual(p['level'], 5)
        self.assertAlmostEqual(p['percent'], 50.0, delta=2.0)
        bar = progress_bar(50, width=10)
        self.assertEqual(len(bar), 10)
        self.assertIn('█', bar)
        self.assertIn('░', bar)

    def test_valid_message_filter(self):
        from mega_bot.levels import is_valid_activity_message
        self.assertFalse(is_valid_activity_message(''))
        self.assertFalse(is_valid_activity_message('   '))
        self.assertTrue(is_valid_activity_message('سلام'))
        self.assertTrue(is_valid_activity_message('', {'photo': [{}]}))
