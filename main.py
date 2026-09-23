import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import BOT_TOKEN
from db import init_db
import start_handlers as start
import profile_handlers as profile
import explore_handlers as explore


async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(start.router)
    dp.include_router(profile.router)
    dp.include_router(explore.router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
