import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonWebApp, WebAppInfo

from config import BOT_TOKEN, WEBAPP_URL, PORT, ADMIN_USERNAMES
from db import init_db
from webserver import start_web
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

    # страница игры, API и живой мир на том же сервере
    await start_web(PORT)
    logging.info("Администраторы игры: %s", ", ".join("@" + a for a in ADMIN_USERNAMES) or "не заданы")

    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    else:
        logging.warning("WEBAPP_URL не задан: кнопка «Играть» не появится")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
