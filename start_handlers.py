from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from sqlalchemy import select

from db import SessionLocal
from db_atomic import insert_ignore
from models import Player
from game_data import STARTING_STATS
from config import WEBAPP_URL

router = Router()


def play_keyboard():
    # Кнопка, открывающая игру внутри Telegram
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])


@router.message(Command("play"))
async def cmd_play(message: Message):
    kb = play_keyboard()
    if kb:
        await message.answer("Твой робот ждёт в ангаре:", reply_markup=kb)
    else:
        await message.answer("Игра пока не подключена: не задан адрес WEBAPP_URL.")


@router.message(CommandStart())
async def cmd_start(message: Message):
    import funnel
    funnel.mark(message.from_user.id, "bot_start")
    # реферальная ссылка: t.me/бот?start=ref_<id пригласившего>
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        import gram
        if await gram.bind_referral(message.from_user.id, int(parts[1][4:])):
            await message.answer("🤝 Ты пришёл по приглашению друга. Удачной охоты, пилот!")
    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == message.from_user.id))
        player = result.scalar_one_or_none()

        if player:
            await message.answer(
                f"С возвращением, пилот {player.name}! Жми «Играть», чтобы открыть ангар.",
                reply_markup=play_keyboard(),
            )
            return

    async with SessionLocal() as session:
        # «вставить, если нет»: двойное нажатие /start не даёт ошибку уникальности
        await session.execute(insert_ignore(Player.__table__, tg_id=message.from_user.id,
                                            name=(message.from_user.first_name or "Пилот")[:64],
                                            current_zone="scrapfields", **STARTING_STATS))
        await session.commit()
    await message.answer(
        "Добро пожаловать в MetalWar — мир боевых роботов.\n\n"
        "Робот собран и ждёт в ангаре. Жми «Играть», чтобы открыть игру и управлять роботом.\n\n"
        "Команды:\n"
        "/play — открыть игру\n"
        "/profile — статус робота\n"
        "/explore — быстрый рейд в чате",
        reply_markup=play_keyboard(),
    )
