import random

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from db import SessionLocal
from models import Player
from game_data import ZONES, NPCS, exp_to_next_level

router = Router()


def zones_keyboard(player_level: int) -> InlineKeyboardMarkup:
    buttons = []
    for key, zone in ZONES.items():
        label = zone["name"]
        if player_level < zone["min_level"]:
            label += f" 🔒 (ур. {zone['min_level']})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("explore"))
async def cmd_explore(message: Message):
    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == message.from_user.id))
        player = result.scalar_one_or_none()

    if not player:
        await message.answer("Сначала введи /start.")
        return

    await message.answer("Выбери зону для исследования:", reply_markup=zones_keyboard(player.level))


async def apply_level_ups(player: Player):
    leveled = False
    while player.exp >= exp_to_next_level(player.level):
        player.exp -= exp_to_next_level(player.level)
        player.level += 1
        player.max_hp += 10
        player.attack += 2
        player.defense += 1
        player.hp = player.max_hp
        leveled = True
    return leveled


@router.callback_query(F.data.startswith("zone:"))
async def enter_zone(callback: CallbackQuery):
    zone_key = callback.data.split(":", 1)[1]
    zone = ZONES[zone_key]

    async with SessionLocal() as session:
        result = await session.execute(select(Player).where(Player.tg_id == callback.from_user.id))
        player = result.scalar_one_or_none()

        if not player:
            await callback.answer("Сначала /start", show_alert=True)
            return

        if player.level < zone["min_level"]:
            await callback.answer(
                f"Нужен {zone['min_level']} уровень для этой зоны.", show_alert=True
            )
            return

        player.current_zone = zone_key

        # 60% шанс встретить NPC, иначе добыча ресурса
        if random.random() < 0.6:
            npc_key = random.choice(zone["npc_pool"])
            npc = NPCS[npc_key]
            log, player_hp_left, npc_hp_left, won = simulate_fight(player, npc)
            report = "\n".join(log)

            if won:
                scrap_gain = random.randint(*npc["scrap"])
                player.scrap += scrap_gain
                player.exp += npc["exp"]
                player.hp = max(player_hp_left, 1)
                leveled = await apply_level_ups(player)
                report += (
                    f"\n\n✅ Победа над «{npc['name']}»! +{npc['exp']} опыта, +{scrap_gain} металлолома."
                )
                if leveled:
                    report += f"\n🎉 Новый уровень: {player.level}!"
            else:
                player.hp = 1
                loss = min(player.scrap, random.randint(2, 6))
                player.scrap -= loss
                report += (
                    f"\n\n💥 Твой робот серьёзно повреждён и еле уцелел. "
                    f"Потеряно {loss} металлолома при экстренном ремонте."
                )

            await session.commit()
            await callback.message.edit_text(f"Зона: {zone['name']}\n\n{report}")
        else:
            resource = zone["resource"]
            amount = random.randint(*zone["resource_amount"])
            setattr(player, resource, getattr(player, resource) + amount)
            resource_name = "металлолома" if resource == "scrap" else "энергоядер"
            await session.commit()
            await callback.message.edit_text(
                f"Зона: {zone['name']}\n\nТвой робот нашёл залежи ресурсов: +{amount} {resource_name}."
            )

    await callback.answer()


def simulate_fight(player: Player, npc: dict):
    log = [f"⚔️ Встречен «{npc['name']}» (HP {npc['hp']}, атака {npc['attack']})"]
    p_hp = player.hp
    n_hp = npc["hp"]
    turn = 1

    while p_hp > 0 and n_hp > 0 and turn <= 20:
        dmg_to_npc = max(1, player.attack - turn // 10)
        n_hp -= dmg_to_npc
        if n_hp <= 0:
            break
        dmg_to_player = max(1, npc["attack"] - player.defense)
        p_hp -= dmg_to_player
        turn += 1

    log.append(f"Бой завершён за {turn} раунд(ов). Остаток HP робота: {max(p_hp, 0)}")
    return log, p_hp, n_hp, p_hp > 0
