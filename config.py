"""MEGA BOT (Telegram) configuration.

Fill these placeholders yourself. Prefer environment variables on Render.
Never commit real tokens or database passwords.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Fill these (or set env vars with the same names) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://USER:PASSWORD@HOST:5432/DATABASE",
)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # your Telegram numeric user id

BOT_VERSION = os.getenv("BOT_VERSION", "1.0.0-telegram")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "25"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SUPPORT_TEXT = os.getenv(
    "SUPPORT_TEXT",
    "برای پشتیبانی با مالک ربات در ارتباط باشید.",
)
DONATION_TEXT = os.getenv(
    "DONATION_TEXT",
    "برای حمایت مالی از توسعه MEGA BOT با مالک ربات هماهنگ کنید.",
)
# Optional public bot username (without @) for deep links t.me/...
BOT_USERNAME = os.getenv("BOT_USERNAME", "YOUR_BOT_USERNAME")
