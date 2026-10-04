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
from models import GramTx, SeasonPts

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
SOON = set()                       # события ещё не открыты — задание видно, но пока недоступно
FIELDS = {"scrapfields", "reactor_ruins", "iron_canyon", "farm1", "season1"}   # фарм-зоны (и сезонная) идут в «убить в полях»
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


# Защита от «стирки» на маркете: два своих аккаунта гоняют один лот туда-обратно, платя только 5% комиссии,
# и набирают очки в рейтинг с призами в USDT. В задания идёт не больше PAIR_CAP GRAM в день с одним и тем же
# контрагентом (отдельно для покупки и продажи) — дневные задания «на 3 GRAM» выполняются как раньше.
PAIR_CAP = 3.0


async def add_trade(buyer, seller, amount):
    """Сделка на маркете: покупателю — mbuy, продавцу — msell, с лимитом на пару в день."""
    if not buyer or not seller or buyer == seller or amount <= 0:
        return
    for uid, other, ctr in ((buyer, seller, "mbuy"), (seller, buyer, "msell")):
        try:
            st = await _load(uid)
        except Exception:
            log.exception("сезон: не удалось загрузить очки uid=%s", uid)
            continue
        pairs = _period(st, "day").setdefault("pair", {})        # обнуляется вместе с днём
        key = f"{ctr}:{other}"
        used = pairs.get(key, 0)
        take = round(max(0.0, min(amount, PAIR_CAP - used)), 4)
        if take <= 0:
            continue
        pairs[key] = round(used + take, 4)
        await add(uid, ctr, take)


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


# ---------- билет сезона ----------
_ticket = {}                                    # uid -> (сезон, есть билет, когда проверяли)


def season_start(key):
    y, m = map(int, key.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return season_end(f"{y}-{m:02d}")


async def has_ticket(uid):
    """Куплен ли билет текущего сезона (по журналу покупок GRAM; кэш на минуту)."""
    key = season_key()
    hit = _ticket.get(uid)
    if hit and hit[0] == key and time.time() - hit[2] < 60:
        return hit[1]
    async with SessionLocal() as s:
        row = (await s.execute(select(GramTx.id).where(GramTx.tg_id == uid, GramTx.kind == "shop",
                                                       GramTx.ref.like(f"shop:{uid}:season:%"),
                                                       GramTx.ts >= season_start(key)).limit(1))).first()
    _ticket[uid] = (key, bool(row), time.time())
    return bool(row)


def forget_ticket(uid):
    _ticket.pop(uid, None)


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
        v = state_view(await _load(user["id"]))
        v["ticket"] = await has_ticket(user["id"])
        return web.json_response(v)

    async def api_event(request):
        body, user = await read_auth(request)
        uid = user["id"]
        if body.get("kind") == "pot_atk" and time.time() - pot_t.get(uid, 0) >= POT_GAP:
            pot_t[uid] = time.time()
            await add(uid, "pot_atk", 1)
        return web.json_response(state_view(await _load(uid)))

    app.router.add_post("/api/season/state", api_state)
    app.router.add_post("/api/season/event", api_event)


# ===================== РЕЙТИНГ СЕЗОНА И ПРИЗЫ =====================
# Топ-20 по очкам сезона. После конца сезона итоги фиксируются (таблица season_prizes),
# админ подтверждает выплату, затем победитель забирает приз в GRAM кнопкой «Забрать».
PRIZES_USDT = {1: 100, 2: 50, 3: 50, **{p: 10 for p in range(4, 11)}, **{p: 5 for p in range(11, 21)}}
TOP_N = 20


def prev_season(key):
    y, m = map(int, key.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y}-{m:02d}"


async def _nicks(s, ids):
    from models import GameSave
    if not ids:
        return {}
    rows = (await s.execute(select(GameSave.tg_id, GameSave.nick, GameSave.name).where(GameSave.tg_id.in_(ids)))).all()
    return {a: (b or c or "Пилот") for a, b, c in rows}


async def top(s, key, limit=TOP_N):
    """Лучшие по очкам: при равенстве выше тот, кто набрал раньше."""
    rows = (await s.execute(select(SeasonPts.tg_id, SeasonPts.pts).where(SeasonPts.season == key, SeasonPts.pts > 0)
                            .order_by(SeasonPts.pts.desc(), SeasonPts.updated.asc()).limit(limit))).all()
    names = await _nicks(s, [r[0] for r in rows])
    return [{"place": i + 1, "id": str(uid), "nick": names.get(uid, "Пилот"), "pts": pts, "usdt": PRIZES_USDT.get(i + 1, 0)}
            for i, (uid, pts) in enumerate(rows)]


async def finalize(key):
    """Зафиксировать призёров прошедшего сезона (один раз)."""
    from models import SeasonPrize
    import gram
    from config import GRAM_USD
    if time.time() < season_end(key):
        return False
    await flush()                                          # последние очки из памяти — в базу
    async with SessionLocal() as s:
        if (await s.execute(select(SeasonPrize.tg_id).where(SeasonPrize.season == key).limit(1))).first():
            return False
        winners = await top(s, key)
        now = int(time.time())
        for w in winners:
            s.add(SeasonPrize(season=key, tg_id=int(w["id"]), place=w["place"], pts=w["pts"], nick=w["nick"][:40],
                              usdt=w["usdt"], nano=int(w["usdt"] / GRAM_USD * gram.NANO), status="wait", created=now))
        if not winners:                                    # пустой сезон — отметка, чтобы не проверять снова
            s.add(SeasonPrize(season=key, tg_id=0, place=0, status="none", created=now))
        await s.commit()
    log.warning("Сезон %s: итоги зафиксированы, призёров %s", key, len(winners))
    asyncio.create_task(_notify_winners(key, winners))
    return True


async def _notify_winners(key, winners):
    """Победителям — сообщение от бота в Telegram."""
    import gram
    bot = gram.BOT.get("bot")
    if not bot:
        return
    for w in winners:
        try:
            await bot.send_message(int(w["id"]), f"🏆 Сезон {key} завершён! Ты занял {w['place']} место ({w['pts']} очков сезона). "
                                                 f"Приз: {w['usdt']} USDT в GRAM. После подтверждения админом забери его в игре: Меню → Сезон → Рейтинг.")
        except Exception as e:
            log.info("сезон: не удалось уведомить %s: %s", w["id"], e)
        await asyncio.sleep(0.05)


def setup_rating(app, read_auth, push_to_player):
    from models import SeasonPrize
    import gram

    async def api_top(request):
        body, user = await read_auth(request)
        key = season_key()
        try:
            await finalize(prev_season(key))
        except Exception:
            log.exception("сезон: итоги прошлого сезона")
        await flush()
        st = await _load(user["id"])
        async with SessionLocal() as s:
            rows = await top(s, key)
            mine = next((r for r in rows if r["id"] == str(user["id"])), None)
            if not mine and st["pts"] > 0:
                higher = (await s.execute(select(SeasonPts.tg_id).where(SeasonPts.season == key, SeasonPts.pts > st["pts"]))).all()
                mine = {"place": len(higher) + 1, "pts": st["pts"]}
            prize = (await s.execute(select(SeasonPrize).where(SeasonPrize.tg_id == user["id"], SeasonPrize.status.in_(["wait", "approved"]))
                                     .order_by(SeasonPrize.season.desc()))).scalars().first()
            pending = None
            if user.get("admin"):
                ps = (await s.execute(select(SeasonPrize).where(SeasonPrize.status == "wait", SeasonPrize.tg_id != 0))).scalars().all()
                if ps:
                    pending = {"season": ps[0].season, "count": len(ps), "usdt": sum(p.usdt for p in ps), "gram": gram.g(sum(p.nano for p in ps))}
            hist = (await s.execute(select(SeasonPrize).where(SeasonPrize.place.between(1, 3), SeasonPrize.tg_id != 0)
                                    .order_by(SeasonPrize.season.desc(), SeasonPrize.place).limit(9))).scalars().all()
        history = {}
        for h in hist:
            history.setdefault(h.season, []).append({"place": h.place, "nick": h.nick, "pts": h.pts, "usdt": h.usdt})
        from config import GRAM_USD
        return web.json_response({"ok": True, "season": key, "top": rows, "me": mine, "rate": GRAM_USD,
                                  "history": [{"season": k, "top": v} for k, v in history.items()],
                                  "prizes": {str(k): v for k, v in PRIZES_USDT.items()},
                                  "prize": {"season": prize.season, "place": prize.place, "usdt": prize.usdt, "gram": gram.g(prize.nano),
                                            "status": prize.status} if prize else None, "admin_pending": pending})

    async def api_claim(request):
        body, user = await read_auth(request)
        uid = user["id"]
        async with SessionLocal() as s:
            p = (await s.execute(select(SeasonPrize).where(SeasonPrize.tg_id == uid, SeasonPrize.status == "approved")
                                 .order_by(SeasonPrize.season.desc()))).scalars().first()
            if not p:
                return web.json_response({"ok": False, "error": "Нет наград к выдаче"})
            p.status = "claimed"
            ok = await gram.move(s, uid, p.nano, "season_prize", f"season_prize:{p.season}:{uid}", f"Приз сезона {p.season}: {p.place} место")
            if not ok:
                return web.json_response({"ok": False, "error": "Не удалось зачислить"})
            try:
                await s.commit()
            except Exception:
                log.exception("сезон: приз uid=%s уже выдан?", uid)
                return web.json_response({"ok": False, "error": "Награда уже получена"})
            bal = (await gram.wallet_of(s, uid)).balance
        log.warning("Сезон %s: приз выдан uid=%s, %s место, %s USDT", p.season, uid, p.place, p.usdt)
        return web.json_response({"ok": True, "gram": gram.g(p.nano), "balance": gram.g(bal)})

    async def api_approve(request):
        body, user = await read_auth(request)
        if not user.get("admin"):
            raise web.HTTPForbidden()
        async with SessionLocal() as s:
            ps = (await s.execute(select(SeasonPrize).where(SeasonPrize.status == "wait", SeasonPrize.tg_id != 0))).scalars().all()
            for p in ps:
                p.status = "approved"
            await s.commit()
        for p in ps:
            await push_to_player(p.tg_id, {"t": "pinfo",
                                           "text": f"🏆 Приз сезона {p.season} ({p.place} место) готов: забери во вкладке «Рейтинг» окна «Сезон»"})
        log.warning("Сезон: админ %s подтвердил выплаты, призёров %s", user["username"], len(ps))
        return web.json_response({"ok": True, "count": len(ps)})

    app.router.add_post("/api/season/top", api_top)
    app.router.add_post("/api/season/claim", api_claim)
    app.router.add_post("/api/season/approve", api_approve)
