"""VIP на сервере: уровень по потраченным GRAM (кошелёк на сервере) и бонус к дропу ценного лута.

Таблица совпадает с VIP в game.html. Бонус к дропу сервер применяет сам при броске лута
за подтверждённые убийства, поэтому подделать его на телефоне нельзя.
"""
import time

from sqlalchemy import select

# (нужно потратить GRAM, +опыт, +лом, +дроп) — как в игре
LEVELS = [
    (1, 0.05, 0, 0), (6, 0.05, 0.05, 0), (16, 0.1, 0.1, 0), (41, 0.2, 0.2, 0), (91, 0.35, 0.35, 0.1),
    (191, 0.5, 0.5, 0.2), (341, 0.6, 0.6, 0.25), (541, 0.75, 0.75, 0.3), (841, 0.9, 0.9, 0.4), (1341, 1.0, 1.0, 1.0),
    (2041, 1.2, 1.2, 1.2), (3041, 1.4, 1.4, 1.4), (4541, 1.6, 1.6, 1.6), (6541, 1.8, 1.8, 1.8), (9541, 2.3, 2.3, 2.3),
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


def cleanup():
    """Раз в минуту: кэш уровней VIP старше 10 минут не нужен (при надобности перечитается из базы)."""
    now = time.time()
    for uid in [u for u, (t, _) in _cache.items() if now - t > 600]:
        _cache.pop(uid, None)
