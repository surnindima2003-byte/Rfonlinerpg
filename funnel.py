"""Воронка новичка: на каком шаге уходят игроки.

Каждый шаг записывается один раз на игрока (таблица funnel_events). Отметка не ждёт базу:
запись уходит фоновой задачей, а повторные отметки отсекаются в памяти.
Отчёт — в статистике админа (stats.funnel_report).
"""
import asyncio
import logging
import time

from sqlalchemy import select, func

import metrics
from db import SessionLocal
from db_atomic import insert_ignore
from models import FunnelEvent

log = logging.getLogger("funnel")

# порядок шагов в отчёте
STEPS = [
    ("bot_start", "Нажал /start в боте"),
    ("app_open", "Открыл игру"),
    ("world", "Вошёл в живой мир"),
    ("kill1", "Первое убийство"),
    ("loot1", "Первый ценный лут"),
    ("lvl5", "5-й уровень"),
    ("lvl10", "10-й уровень (PvP)"),
    ("boss1", "Первый главарь"),
    ("lvl20", "20-й уровень"),
    ("chipwar", "Участвовал в Chip War"),
    ("market", "Первая сделка на маркете"),
    ("pay", "Первая оплата"),
]
KNOWN = {k for k, _ in STEPS}

_seen = set()            # (uid, step) уже записаны в этом процессе
_tasks = set()


def mark(uid, step):
    """Отметить шаг. Можно звать сколько угодно раз и откуда угодно: база не ждётся."""
    if step not in KNOWN or not uid or (uid, step) in _seen:
        return
    _seen.add((uid, step))
    if len(_seen) > 500_000:                     # память: при переполнении просто забываем кэш
        _seen.clear()
    try:
        t = asyncio.get_running_loop().create_task(_write(uid, step))
    except RuntimeError:
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _write(uid, step):
    try:
        async with SessionLocal() as s:
            await s.execute(insert_ignore(FunnelEvent.__table__, tg_id=uid, step=step, ts=int(time.time())))
            await s.commit()
    except Exception:
        _seen.discard((uid, step))
        metrics.inc("funnel.write_error")
        log.exception("funnel %s %s", uid, step)


async def report(s, days=7):
    """Сколько игроков, пришедших за последние `days` дней, дошли до каждого шага."""
    since = int(time.time()) - days * 86400
    # когорта: первое событие игрока (любое) — в окне
    first = select(FunnelEvent.tg_id, func.min(FunnelEvent.ts).label("t0")).group_by(FunnelEvent.tg_id).subquery()
    cohort = select(first.c.tg_id).where(first.c.t0 >= since)
    rows = (await s.execute(select(FunnelEvent.step, func.count()).where(FunnelEvent.tg_id.in_(cohort)).group_by(FunnelEvent.step))).all()
    counts = {step: n for step, n in rows}
    return [{"step": k, "name": name, "n": counts.get(k, 0)} for k, name in STEPS]
