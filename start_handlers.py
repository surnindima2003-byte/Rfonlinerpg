from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from sqlalchemy import select

from bot_utils import edit_or_send
from db import SessionLocal
from db_atomic import insert_ignore
from models import Player
from game_data import FACTIONS, STARTING_STATS
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


def factions_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title.split(" — ")[0], callback_data=f"faction:{key}")]
        for key, title in FACTIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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

    text = (
        "Добро пожаловать в MetalWar — мир, где корпорации сражаются за ресурсы руками боевых роботов.\n\n"
        "Выбери фракцию, за которую будет сражаться твой робот:\n\n"
        + "\n\n".join(f"• {v}" for v in FACTIONS.values())
    )
    await message.answer(text, reply_markup=factions_keyboard())


@router.callback_query(F.data.startswith("faction:"))
async def choose_faction(callback: CallbackQuery):
    faction_key = callback.data.split(":", 1)[1]
    if faction_key not in FACTIONS:
        # данные кнопки присылает клиент: раньше чужое значение записывалось в базу, а /profile потом падал
        await callback.answer("Такой фракции нет", show_alert=True)
        return

    async with SessionLocal() as session:
        # «вставить, если нет»: двойное нажатие раньше давало ошибку уникальности
        res = await session.execute(insert_ignore(Player.__table__, tg_id=callback.from_user.id,
                                                  name=(callback.from_user.first_name or "Пилот")[:64], faction=faction_key,
                                                  current_zone="scrapfields", **STARTING_STATS))
        await session.commit()
    if not res.rowcount:
        await callback.answer("Ты уже выбрал фракцию раньше.", show_alert=True)
        return

    await edit_or_send(callback,
        f"Робот собран и подключён к сети {FACTIONS[faction_key].split(' — ')[0]}.\n\n"
        "Жми «Играть», чтобы открыть ангар и управлять роботом.\n\n"
        "Команды:\n"
        "/play — открыть игру\n"
        "/profile — статус робота\n"
        "/explore — быстрый рейд в чате",
        reply_markup=play_keyboard(),
    )
    await callback.answer()
