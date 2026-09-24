import asyncio
import logging
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonWebApp, WebAppInfo

from config import BOT_TOKEN, WEBAPP_URL, PORT
from db import init_db
import start_handlers as start
import profile_handlers as profile
import explore_handlers as explore

GAME_FILE = Path(__file__).parent / "game.html"


async def game_page(request):
    # Страница игры, которую открывает кнопка «Играть» в Telegram
    return web.FileResponse(GAME_FILE, headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", game_page)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Игра доступна на порту %s", PORT)


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(profile.router)
    dp.include_router(explore.router)

    await start_web()

    # Кнопка «Играть» рядом с полем ввода во всех чатах с ботом
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
