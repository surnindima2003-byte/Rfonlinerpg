"""VIP на сервере: уровень по потраченным GRAM (кошелёк на сервере) и бонус к дропу ценного лута.

Таблица совпадает с VIP в game.html. Бонус к дропу сервер применяет сам при броске лута
за подтверждённые убийства, поэтому подделать его на телефоне нельзя.
"""
import time

from sqlalchemy import select

# (нужно потратить GRAM, +опыт, +лом, +дроп) — как в игре
LEVELS = [
    (1, .05, 0, 0), (6, .05, .05, 0), (16, .10, .10, 0), (41, .20, .20, 0), (91, .35, .35, .10),
    (166, .50, .50, .20), (266, .60, .60, .25), (416, .75, .75, .30), (616, .90, .90, .40), (866, 1.0, 1.0, 1.0),
    (1166, 1.2, 1.2, 1.2), (1566, 1.4, 1.4, 1.4), (2066, 1.6, 1.6, 1.6), (2666, 1.8, 1.8, 1.8), (3366, 2.3, 2.3, 2.3),
]
NANO = 1_000_000_000
_cache = {}                         # tg_id -> (время, уровень)


def level_of(spent_gram):
    return sum(1 for need, *_ in LEVELS if spent_gram >= need)


def drop_mult(level):
    return 1 + (LEVELS[level - 1][3] if level > 0 else 0)


async def level(s, uid, admin=False):
    """Уровень VIP игрока (кэш на минуту). Админ — всегда максимальный."""
    if admin:
        return len(LEVELS)
    hit = _cache.get(uid)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    from models import GramWallet
    spent = (await s.execute(select(GramWallet.spent).where(GramWallet.tg_id == uid))).scalar_one_or_none() or 0
    lv = level_of(spent / NANO)
    _cache[uid] = (time.time(), lv)
    return lv


def forget(uid):
    """Сбросить кэш после покупки в магазине — новый VIP начнёт действовать сразу."""
    _cache.pop(uid, None)
