from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from db import SessionLocal
from models import Player
from game_data import FACTIONS, ZONES, exp_to_next_level

router = Router()


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == message.from_user.id))
        player = result.scalar_one_or_none()

    if not player:
        await message.answer("Ты ещё не начал игру. Введи /start.")
        return

    zone_name = ZONES.get(player.current_zone, {}).get("name", player.current_zone)
    need_exp = exp_to_next_level(player.level)

    text = (
        f"🤖 {player.name} | {FACTIONS[player.faction].split(' — ')[0]}\n"
        f"Уровень: {player.level} (опыт {player.exp}/{need_exp})\n"
        f"HP: {player.hp}/{player.max_hp}\n"
        f"Атака: {player.attack} | Броня: {player.defense}\n\n"
        f"Металлолом: {player.scrap} | Энергоядра: {player.energy_core}\n"
        f"Текущая зона: {zone_name}"
    )
    await message.answer(text)
