# MEGA BOT — Telegram Edition

نسخهٔ مستقل تلگرام از MEGA BOT (نسخهٔ بله جداگانه در `commander1405/mega-bot` باقی می‌ماند).

## قابلیت‌ها (هم‌تراز نسخه بله)

- منو و کیبورد (Reply + Inline)
- پنل مالک و ادمین
- مدیریت گروه: بن، آنبن، سکوت، اخطار، پاکسازی پیام، قفل‌ها، ضداسپم، قفل زمان‌بندی‌شده، محدودیت عضویت
- خوشامد / خداحافظی
- یادگیری کلمات (تکی و گروهی)
- لول / XP / پروفایل گروهی / لقب
- آمار داشبورد تصویری ۲۴ ساعته
- نجوا، فونت‌ساز، تایم ادبی، فال حافظ، جوک
- ویس (TTS)، گیف متن‌دار
- حمایت مالی و تنظیم مبلغ توسط مالک
- حالت تعمیرات

## تنظیمات

فایل `config.py` را ویرایش کن یا متغیر محیطی بگذار:

| کلید | توضیح |
|------|--------|
| `BOT_TOKEN` | توکن از [@BotFather](https://t.me/BotFather) |
| `DATABASE_URL` | PostgreSQL connection string |
| `OWNER_ID` | آیدی عددی تلگرام مالک |
| `BOT_USERNAME` | یوزرنیم ربات بدون @ |

## اجرا محلی

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# config.py یا .env را پر کن
python main.py
```

## Deploy روی Render

1. New **Worker** (نه Web) از همین ریپو
2. Build: `pip install -r requirements.txt`
3. Start: `python main.py`
4. Env: `BOT_TOKEN`, `DATABASE_URL`, `OWNER_ID`, `BOT_USERNAME`
5. یک PostgreSQL به سرویس وصل کن

**Polling:** فقط یک instance Worker اجرا شود تا conflict روی `getUpdates` پیش نیاید.

## تفاوت‌ها با نسخه Bale

| موضوع | تلگرام |
|--------|---------|
| API host | `api.telegram.org` |
| لینک عمومی | `t.me/...` |
| پروفایل عکس | `getUserProfilePhotos` پشتیبانی رسمی |
| رنگ دکمه | تلگرام رنگ اختصاصی دکمه ندارد |
| بیوگرافی کاربر | Bot API بیوگرافی عمومی کاربر را نمی‌دهد → نمایش داده نمی‌شود |

## ساختار

```
main.py
config.py
mega_bot/
  telegram_api.py   # کلاینت Telegram Bot API
  app.py            # هندلرها
  database.py       # PostgreSQL
  ...
```
