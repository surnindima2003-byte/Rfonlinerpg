"""Очки сезона и задания сезона (как «Сезон 4» в Liberty, в нашем стиле).

Очки начисляет только сервер — по событиям, которые он видит сам: убийства (подтверждённые
пачки убийств), сообщения чата, покупки и продажи на маркете. Кнопка не нужна: задание
выполнено — очки начислены сразу, игроку приходит уведомление.

Сезон длится месяц: начинается 1-го числа в 18:00 МСК и заканчивается 1-го числа следующего
месяца в 18:00 МСК. Ежедневные задания сбрасываются в 00:00 МСК, недельные — в понедельник.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
from sqlalchemy import select

import metrics
from db import SessionLocal
from models import SeasonPts

log = logging.getLogger("season")
MSK = timezone(timedelta(hours=3))

# id, раздел, название, нужно, очков, счётчик
TASKS = [
    ("perm_mbuy", "perm", "Покупка на маркете — за каждый GRAM", 1, 10, "mbuy"),
    ("d_chat", "day", "Отправить 10 сообщений в игровой чат", 10, 30, "chat"),
    ("d_mbuy", "day", "Купить на маркете на 3 GRAM", 3, 100, "mbuy"),
    ("d_msell", "day", "Продать на маркете на 3 GRAM", 3, 100, "msell"),
    ("w_f1", "week", "Убить 100 000 монстров в Нижних цехах (1 этаж)", 100000, 300, "kill_f1"),
    ("w_f2", "week", "Убить 100 000 монстров в Глубинных цехах (2 этаж)", 100000, 300, "kill_f2"),
    ("w_farm", "week", "Убить 100 000 монстров в полях (фарм-зоны)", 100000, 300, "kill_farm"),
    ("w_pot", "week", "Выпить зелье силы 10 раз", 10, 50, "pot_atk"),
    ("w_tower", "week", "Участвовать в Кровавой башне 3 раза", 3, 200, "tower"),
    ("w_boss", "week", "Ударить мирового босса 3 раза", 3, 200, "wboss"),
]
SOON = {"tower", "wboss"}                       # события ещё не открыты — задание видно, но пока недоступно
FIELDS = {"scrapfields", "reactor_ruins", "iron_canyon"}
POT_GAP = 9 * 60                                # зелье действует 10 минут — засчитываем не чаще раза в 9 минут

_st = {}                                         # uid -> состояние сезона (в памяти, сбрасывается в базу раз в 20 с)
_dirty = set()
_push = None


def now_msk():
    return datetime.now(MSK)


def season_key(dt=None):
    dt = dt or now_msk()
    start = dt.replace(day=1, hour=18, minute=0, second=0, microsecond=0)
    if dt < start:
        dt = (dt.replace(day=1) - timedelta(days=1))
    return f"{dt.year}-{dt.month:02d}"


def season_end(key):
    y, m = map(int, key.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return int(datetime(y, m, 1, 18, 0, tzinfo=MSK).timestamp())


def day_key(dt=None):
    return (dt or now_msk()).strftime("%Y-%m-%d")


def week_key(dt=None):
    dt = dt or now_msk()
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def _period(st, scope):
    """Счётчики дня/недели: при смене дня или недели обнуляются."""
    key = day_key() if scope == "day" else week_key() if scope == "week" else "all"
    box = st["data"].setdefault(scope, {})
    if box.get("key") != key:
        box.clear()
        box["key"] = key
    box.setdefault("c", {})
    box.setdefault("done", [])
    return box


async def _load(uid):
    key = season_key()
    st = _st.get(uid)
    if st and st["season"] == key:
        return st
    async with SessionLocal() as s:
        row = (await s.execute(select(SeasonPts).where(SeasonPts.tg_id == uid, SeasonPts.season == key))).scalar_one_or_none()
    st = {"season": key, "pts": row.pts if row else 0, "data": json.loads(row.data or "{}") if row else {}}
    _st[uid] = st
    return st


async def add(uid, counter, amount=1):
    """Событие для заданий: counter — chat, mbuy, msell, kill_f1, kill_f2, kill_farm, pot_atk."""
    if not uid or amount <= 0:
        return
    try:
        st = await _load(uid)
    except Exception:
        log.exception("сезон: не удалось загрузить очки uid=%s", uid)
        return
    gained, names = 0, []
    for tid, scope, title, need, pts, ctr in TASKS:
        if ctr != counter or ctr in SOON:
            continue
        box = _period(st, scope)
        c = box["c"]
        c[tid] = round(c.get(tid, 0) + amount, 4)
        if scope == "perm":                     # постоянное: очки за каждую целую единицу
            paid = box.setdefault("paid", {}).get(tid, 0)
            whole = int(c[tid] // need)
            if whole > paid:
                gained += (whole - paid) * pts
                box["paid"][tid] = whole
        elif tid not in box["done"] and c[tid] >= need:
            box["done"].append(tid)
            gained += pts
            names.append(title)
    _dirty.add(uid)
    if gained:
        st["pts"] += gained
        metrics.inc("season.pts", gained)
        if _push:
            text = ("Задание сезона: " + names[0] + f" · +{gained} очков") if names else f"+{gained} очков сезона"
            try:
                await _push(uid, {"t": "spts", "pts": st["pts"], "gain": gained, "text": text})
            except Exception:
                pass


def kill_counter(loc):
    return "kill_f1" if loc == "sector1" else "kill_f2" if loc == "sector2" else "kill_farm" if loc in FIELDS else None


async def flush():
    if not _dirty:
        return
    batch = list(_dirty)
    _dirty.clear()
    now = int(time.time())
    async with SessionLocal() as s:
        for uid in batch:
            st = _st.get(uid)
            if not st:
                continue
            row = (await s.execute(select(SeasonPts).where(SeasonPts.tg_id == uid, SeasonPts.season == st["season"]))).scalar_one_or_none()
            if not row:
                row = SeasonPts(tg_id=uid, season=st["season"])
                s.add(row)
            row.pts, row.data, row.updated = st["pts"], json.dumps(st["data"]), now
        await s.commit()


async def flush_loop():
    while True:
        await asyncio.sleep(20)
        try:
            await flush()
        except Exception:
            log.exception("сезон: не удалось сохранить очки")
        for uid in [u for u, st in _st.items() if u not in _dirty and st["season"] != season_key()]:
            _st.pop(uid, None)


def state_view(st):
    tasks = []
    for tid, scope, title, need, pts, ctr in TASKS:
        box = _period(st, scope)
        prog = box["c"].get(tid, 0)
        done = tid in box.get("done", [])
        tasks.append({"id": tid, "scope": scope, "title": title, "need": need, "pts": pts, "prog": prog,
                      "done": done, "soon": ctr in SOON})
    return {"ok": True, "season": st["season"], "ends": season_end(st["season"]) * 1000, "pts": st["pts"], "tasks": tasks}


def setup(app, read_auth, push_to_player):
    global _push
    _push = push_to_player
    pot_t = {}

    async def api_state(request):
        body, user = await read_auth(request)
        return web.json_response(state_view(await _load(user["id"])))

    async def api_event(request):
        body, user = await read_auth(request)
        uid = user["id"]
        if body.get("kind") == "pot_atk" and time.time() - pot_t.get(uid, 0) >= POT_GAP:
            pot_t[uid] = time.time()
            await add(uid, "pot_atk", 1)
        return web.json_response(state_view(await _load(uid)))

    app.router.add_post("/api/season/state", api_state)
    app.router.add_post("/api/season/event", api_event)
