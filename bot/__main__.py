import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

from .config import load_config, Config
from .db import init_db
from .handlers import router
from .monitor import monitor_loop

async def main():
    cfg: Config = load_config()
    os.makedirs("data", exist_ok=True)
    await init_db()

    bot = Bot(token=cfg.bot_token, parse_mode=ParseMode.MARKDOWN)
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp.include_router(router)

    asyncio.create_task(monitor_loop(bot, cfg))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
