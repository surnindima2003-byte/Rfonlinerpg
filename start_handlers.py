from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from db import SessionLocal
from models import Player
from game_data import FACTIONS, STARTING_STATS

router = Router()


def factions_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title.split(" — ")[0], callback_data=f"faction:{key}")]
        for key, title in FACTIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def cmd_start(message: Message):
    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == message.from_user.id))
        player = result.scalar_one_or_none()

        if player:
            await message.answer(
                f"С возвращением, пилот {player.name}! Введи /profile, чтобы увидеть своего робота."
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

    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == callback.from_user.id))
        player = result.scalar_one_or_none()
        if player:
            await callback.answer("Ты уже выбрал фракцию раньше.", show_alert=True)
            return

        player = Player(
            tg_id=callback.from_user.id,
            name=callback.from_user.first_name or "Пилот",
            faction=faction_key,
            **STARTING_STATS,
        )
        session.add(player)
        await session.commit()

    await callback.message.edit_text(
        f"Робот собран и подключён к сети {FACTIONS[faction_key].split(' — ')[0]}.\n\n"
        "Команды:\n"
        "/profile — статус робота\n"
        "/explore — исследовать зону"
    )
    await callback.answer()
