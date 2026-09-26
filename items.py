"""Защита экономики: ценный лут выпадает по решению сервера и регистрируется.

На маркет за GRAM можно выставить только зарегистрированную вещь (снаряжение с номером uid
или сферы из серверного учёта). Нарисованные на телефоне вещи в игре работают, но продать их нельзя.
"""
import json
import logging
import random
import re
import secrets
import time

from aiohttp import web
from sqlalchemy import select, func

from db import SessionLocal
from models import ItemInst, SphereBal, GameSave

log = logging.getLogger("items")

# снаряжение: (id, минимальный уровень) — зеркало ITEMS из игры, стартовые вещи не выпадают
GEAR = [("laser1", 1), ("laser2", 3), ("plasma", 6), ("sensor1", 1), ("sensor2", 4), ("plate1", 1), ("plate2", 4),
        ("tracks1", 1), ("tracks2", 3), ("servo", 2), ("shieldgen", 5), ("reactor1", 1), ("reactor2", 5)]
GEAR_IDS = {g for g, _ in GEAR}
SPHERES = ("sph_cu", "sph_ti")
BASE_MOBS = {"scrap_crawler", "rogue_drone", "sentry_bot", "war_walker"}
LOC_MIN = {"scrapfields": 1, "reactor_ruins": 3, "iron_canyon": 6, "sector1": 1, "sector2": 21, "arena_fear": 1}
DUNGEON_RANGE = {"sector1": (1, 20), "sector2": (21, 40), "arena_fear": (1, 40)}
ENCH_CHANCE = [100, 100, 100, 75, 65, 55, 45, 38, 32, 26, 20, 15, 10, 7, 5]   # как в игре
P = lambda pct: pct / 100.0


def gear_per(lv):
    """Шанс каждой вещи по грейдам (серый, зелёный, синий, золотой) — как gearPer в игре."""
    if lv <= 10:
        return [P(0.0028 + 0.00008 * (lv - 1)), 0, 0, 0]
    if lv <= 20:
        return [0, P(0.0001), 0, 0]
    if lv <= 30:
        return [0, P(0.00012), P(0.00002), 0]
    return [0, 0, P(0.0001), P(0.00001)]


def mob_level(mob, loc):
    m = re.fullmatch(r"dg(\d{1,2})", mob or "")
    if m:
        lv = int(m.group(1))
        lo, hi = DUNGEON_RANGE.get(loc, (0, -1))
        return lv if lo <= lv <= hi else None
    if mob in BASE_MOBS and loc in ("scrapfields", "reactor_ruins", "iron_canyon"):
        return LOC_MIN[loc]
    return None


def roll(lv):
    """Бросок ценного лута за одного убитого моба."""
    out, s = [], lv - 1
    pool = [g for g, need in GEAR if need <= lv + 2]
    for grade, per in enumerate(gear_per(lv)):
        if per and random.random() < per * len(pool):
            out.append({"kind": "gear", "id": random.choice(pool), "g": grade})
    if random.random() < P(0.0033 + 0.000036 * s):
        out.append({"kind": "sph", "id": "sph_cu", "n": 1})
    if random.random() < P(0.00033 + 0.0000036 * s):
        out.append({"kind": "sph", "id": "sph_ti", "n": 1})
    return out


def new_uid():
    return "i" + secrets.token_hex(8)


async def mint_gear(s, owner, item_id, g, e=0, source="drop"):
    uid = new_uid()
    s.add(ItemInst(uid=uid, owner=owner, item=item_id, g=g, e=e, status="inv", source=source, created=int(time.time())))
    return {"id": item_id, "g": g, "e": e, "uid": uid}


async def sph_row(s, owner, sid):
    row = (await s.execute(select(SphereBal).where(SphereBal.owner == owner, SphereBal.item == sid))).scalar_one_or_none()
    if not row:
        row = SphereBal(owner=owner, item=sid, n=0)
        s.add(row)
    return row


async def add_spheres(s, owner, sid, n):
    row = await sph_row(s, owner, sid)
    row.n = (row.n or 0) + n
    return {"id": sid, "n": n, "reg": True}


# ---------- ограничение частоты убийств ----------
_bucket = {}                                    # tg_id -> [жетоны, время]
KILL_RATE, KILL_BURST = 2.5, 30                 # в среднем 2,5 убийства в секунду, запас 30


def allow_kill(uid):
    now = time.time()
    tokens, t0 = _bucket.get(uid, (KILL_BURST, now))
    tokens = min(KILL_BURST, tokens + (now - t0) * KILL_RATE)
    if tokens < 1:
        _bucket[uid] = (tokens, now)
        return False
    _bucket[uid] = (tokens - 1, now)
    return True


# ---------- API ----------
async def api_items(request):
    from webserver import read_auth, online
    body, user = await read_auth(request)
    op, uid = request.match_info["op"], user["id"]
    async with SessionLocal() as s:
        if op == "state":
            items = (await s.execute(select(ItemInst).where(ItemInst.owner == uid, ItemInst.status == "inv"))).scalars().all()
            sph = {r.item: r.n for r in (await s.execute(select(SphereBal).where(SphereBal.owner == uid))).scalars().all()}
            return web.json_response({"ok": True, "items": [{"uid": x.uid, "id": x.item, "g": x.g, "e": x.e} for x in items],
                                      "sph": {k: sph.get(k, 0) for k in SPHERES}})

        if op == "kill":
            # пачка убийств за последние ~1,2 с: [{mob, loc}, ...]
            kills = body.get("kills") or [{"mob": body.get("mob"), "loc": body.get("loc")}]
            me = online(uid)
            save = (await s.execute(select(GameSave.lvl).where(GameSave.tg_id == uid))).scalar()
            drops = []
            for i, k in enumerate(kills[:40]):
                if not isinstance(k, dict):
                    continue
                mob, loc = str(k.get("mob", ""))[:24], str(k.get("loc", ""))[:24]
                lv = mob_level(mob, loc)
                if not me or me.get("loc") != loc or lv is None or lv > (save or 1) + 12 or not allow_kill(uid):
                    continue                                   # не в этой локации, слишком сильный моб или слишком часто
                for d in roll(lv):
                    item = await mint_gear(s, uid, d["id"], d["g"]) if d["kind"] == "gear" else await add_spheres(s, uid, d["id"], d["n"])
                    item["i"] = i
                    drops.append(item)
            if drops:
                await s.commit()
                log.info("Лут: игрок %s → %s", uid, [d["id"] for d in drops])
            return web.json_response({"ok": True, "drops": drops})

        if op == "enchant":
            x = (await s.execute(select(ItemInst).where(ItemInst.uid == str(body.get("uid", ""))))).scalar_one_or_none()
            sid = body.get("sphere")
            if not x or x.owner != uid or x.status != "inv" or sid not in SPHERES:
                return web.json_response({"ok": False, "error": "Вещь не найдена на сервере"})
            if x.e >= len(ENCH_CHANCE):
                return web.json_response({"ok": False, "error": "Максимальная заточка"})
            sph = await sph_row(s, uid, sid)
            if (sph.n or 0) < 1:
                return web.json_response({"ok": False, "error": "Нет учтённой сферы"})
            sph.n -= 1
            if random.random() * 100 < ENCH_CHANCE[x.e]:
                x.e += 1
                result = "ok"
            elif sid == "sph_ti":
                result = "fail"
            else:
                x.status = "gone"
                result = "broken"
            await s.commit()
            return web.json_response({"ok": True, "result": result, "e": x.e, "sph": sph.n})

        if op == "gone":
            # вещь продана торговцу, вложена в кодекс или разрушена — больше не существует
            uids = [str(u)[:24] for u in (body.get("uids") or [])][:50]
            for x in (await s.execute(select(ItemInst).where(ItemInst.uid.in_(uids), ItemInst.owner == uid, ItemInst.status == "inv"))).scalars().all():
                x.status = "gone"
            sp = body.get("sph") or {}
            for sid in SPHERES:
                n = int(sp.get(sid, 0) or 0)
                if n > 0:
                    row = await sph_row(s, uid, sid)
                    row.n = max(0, (row.n or 0) - n)
            await s.commit()
            return web.json_response({"ok": True})
    raise web.HTTPBadRequest(text="bad op")


# ---------- для маркета ----------
async def escrow_for_market(s, uid, item):
    """Проверить и заблокировать предмет под лот. Возвращает (данные предмета с сервера, ошибка)."""
    if item.get("uid"):
        x = (await s.execute(select(ItemInst).where(ItemInst.uid == str(item["uid"])))).scalar_one_or_none()
        if not x or x.owner != uid or x.status != "inv":
            return None, "Эта вещь не подтверждена сервером, продать её за GRAM нельзя"
        x.status = "market"
        return {"id": x.item, "g": x.g, "e": x.e, "n": 1, "uid": x.uid}, None
    if item.get("id") in SPHERES:
        n = max(1, min(999, int(item.get("n", 1))))
        row = await sph_row(s, uid, item["id"])
        if (row.n or 0) < n:
            return None, f"Учтённых сфер только {row.n or 0}: остальные нельзя продать за GRAM"
        row.n -= n
        return {"id": item["id"], "g": 0, "e": 0, "n": n, "reg": True}, None
    return None, "За GRAM можно продавать только снаряжение и сферы, выбитые с мобов"


async def market_transfer(s, item, to_uid, status="inv"):
    """Передать предмет лота новому владельцу (покупка) или вернуть продавцу (снятие)."""
    if item.get("uid"):
        x = (await s.execute(select(ItemInst).where(ItemInst.uid == item["uid"]))).scalar_one_or_none()
        if x:
            x.owner, x.status = to_uid, status
    elif item.get("reg"):
        await add_spheres(s, to_uid, item["id"], int(item.get("n", 1)))


async def migrate_market_registry():
    """Разово: лоты, выставленные до реестра, снимаются, предметы возвращаются продавцам."""
    from models import MarketLot, Grant, Meta
    async with SessionLocal() as s:
        if (await s.execute(select(Meta).where(Meta.key == "market_registry"))).scalar_one_or_none():
            return
        lots = (await s.execute(select(MarketLot))).scalars().all()
        for lot in lots:
            it = json.loads(lot.item)
            s.add(Grant(tg_id=lot.seller_id, kind="item", payload=json.dumps({"amount": it.get("n", 1), "item": it["id"], "grade": it.get("g", 0)}),
                        by_admin="market", created=int(time.time())))
        await s.execute(MarketLot.__table__.delete())
        s.add(Meta(key="market_registry", value="1"))
        await s.commit()
        if lots:
            log.info("Маркет: %s старых лотов возвращены продавцам (переход на реестр вещей)", len(lots))


# предметы паков магазина GRAM, которые регистрируются сервером (их потом можно продать на маркете)
PACK_ITEMS = {"books": [("gear", "laser2", 2)], "legend": [("gear", "plasma", 3)], "spheres": [("sph", "sph_cu", 3)], "cores": [("sph", "sph_ti", 1)]}


async def mint_pack(s, owner, pack):
    out = []
    for kind, iid, v in PACK_ITEMS.get(pack, []):
        out.append(await mint_gear(s, owner, iid, v, source="shop") if kind == "gear" else await add_spheres(s, owner, iid, v))
    return out


def setup(app):
    app.router.add_post("/api/items/{op}", api_items)
