import asyncio
import logging
from config import BOT_TOKEN, DATABASE_URL, POLL_TIMEOUT, LOG_LEVEL
from mega_bot.telegram_api import TelegramAPI
from mega_bot.database import Database
from mega_bot.app import MegaBot


def validate_config() -> None:
    if not BOT_TOKEN or BOT_TOKEN == 'PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE':
        raise SystemExit('BOT_TOKEN را در config.py یا متغیر محیطی BOT_TOKEN تنظیم کن.')
    if not DATABASE_URL or DATABASE_URL.startswith('postgresql://USER:'):
        raise SystemExit('DATABASE_URL را در config.py یا متغیر محیطی DATABASE_URL تنظیم کن.')

async def main() -> None:
    validate_config()
    logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    api = TelegramAPI(BOT_TOKEN)
    db = Database(DATABASE_URL)
    await api.start()
    try:
        await db.connect()
        bot = MegaBot(api, db)
        await bot.run(POLL_TIMEOUT)
    finally:
        await db.close()
        await api.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('MEGA BOT (Telegram) stopped.')
