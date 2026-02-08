import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

from .config import load_config
from .db import DB
from .handlers import router

logging.basicConfig(level=logging.INFO)

async def main():
    cfg = load_config()
    db = DB(cfg.db_path)
    await db.init()

    bot = Bot(token=cfg.bot_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp["db"] = db
    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
