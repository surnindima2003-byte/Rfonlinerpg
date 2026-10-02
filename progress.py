"""Серверный учёт опыта: уровень игрока считает сервер, а не телефон.

Опыт начисляется за каждое убийство, которое сервер подтвердил (локация, уровень моба, частота).
Допустимый уровень считается с запасом ×8: он покрывает бонусы VIP, гильдии и пати,
поэтому честный игрок в ограничение не упирается, а «нарисованный» уровень срезается.
В PvP, рейтинге и боевой мощи используется только этот уровень.
"""
from sqlalchemy import select

from models import ServerProg, GameSave

LEVEL_CAP = 50
XP_SLACK = 12              # VIP 15 даёт +230% опыта, плюс гильдия и пати
_cap = {}                       # tg_id -> допустимый уровень (кэш для живого мира)


def mob_exp(lv, boss=False):
    k = lv - 1
    return round((5 + 3 * k + 0.2 * k * k) * (15 if boss else 1))


def exp_need(l):
    return round((3 + 0.9 * l * l) * mob_exp(l))


def level_from_exp(e):
    lv = 1
    while lv < LEVEL_CAP and e >= exp_need(lv):
        e -= exp_need(lv)
        lv += 1
    return lv


async def prog_of(s, uid):
    row = (await s.execute(select(ServerProg).where(ServerProg.tg_id == uid))).scalar_one_or_none()
    if not row:
        # первый раз: уже набранный уровень засчитываем (до 50-го), дальше — только подтверждённый опыт
        lvl = (await s.execute(select(GameSave.lvl).where(GameSave.tg_id == uid))).scalar() or 1
        row = ServerProg(tg_id=uid, exp=0, base_lvl=max(1, min(LEVEL_CAP, lvl)), bonus=0)
        s.add(row)
    return row


def allowed(row):
    return min(LEVEL_CAP, max(row.base_lvl or 1, level_from_exp((row.exp or 0) * XP_SLACK)) + (row.bonus or 0))


async def cap_of(s, uid):
    row = await prog_of(s, uid)
    _cap[uid] = allowed(row)
    return _cap[uid]


def cached_cap(uid):
    return _cap.get(uid)


async def add_kill(s, uid, lv, boss=False):
    row = await prog_of(s, uid)
    row.exp = (row.exp or 0) + mob_exp(lv, boss)
    _cap[uid] = allowed(row)
    return _cap[uid]


async def add_levels(s, uid, n):
    row = await prog_of(s, uid)
    row.bonus = (row.bonus or 0) + int(n)
    _cap[uid] = allowed(row)


def bm_cap(lvl):
    """Предел боевой мощи для уровня — с большим запасом на лучшее снаряжение и заточку."""
    return 3000 + 900 * lvl + 20 * lvl * lvl
