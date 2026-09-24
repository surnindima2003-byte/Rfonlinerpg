"""Веб-сервер игры: страница мини-приложения, API сохранений и админки, WebSocket живого мира."""
import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from aiohttp import web, WSMsgType
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import select, func

from config import BOT_TOKEN, ADMIN_USERNAMES
from db import SessionLocal
from models import GameSave, Grant

GAME_FILE = Path(__file__).parent / "game.html"
MAX_SAVE_BYTES = 300_000
LOCS = {"lobby", "scrapfields", "reactor_ruins", "iron_canyon"}
FACTIONS = {"aegis", "vex", "core"}
GRANT_KINDS = {"scrap", "cores", "exp", "level", "item"}
GEAR_IDS = {
    "st_head", "st_weapon", "st_module", "st_armor", "st_core", "st_legs",
    "laser1", "laser2", "plasma", "sensor1", "sensor2", "plate1", "plate2",
    "tracks1", "tracks2", "servo", "shieldgen", "reactor1", "reactor2",
    "kit_s", "kit_l", "wire", "plate", "chip",
}
LIMITS = {"scrap": 1_000_000, "cores": 100_000, "exp": 1_000_000, "level": 50, "item": 50}

log = logging.getLogger("web")


# ---------- проверка игрока по подписи Telegram ----------
def auth(init_data: str):
    """Возвращает игрока, если initData подписан нашим ботом и свежий, иначе None."""
    if not init_data or len(init_data) > 4096:
        return None
    try:
        data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
    except ValueError:
        return None
    if not data.user or time.time() - data.auth_date.timestamp() > 7 * 86400:
        return None
    u = data.user
    username = (u.username or "").lower()
    return {"id": u.id, "username": username, "name": u.first_name or "Пилот",
            "admin": username in ADMIN_USERNAMES}


async def read_auth(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    user = auth(body.get("initData", ""))
    if not user:
        raise web.HTTPUnauthorized(text="bad initData")
    return body, user


def grant_dict(g: Grant):
    return {"id": g.id, "kind": g.kind, "payload": json.loads(g.payload or "{}"), "by": g.by_admin}


# ---------- страницы и API ----------
async def game_page(request):
    return web.FileResponse(GAME_FILE, headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.Response(text="ok")


async def api_load(request):
    _, user = await read_auth(request)
    async with SessionLocal() as s:
        row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        grants = (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.applied == False))).scalars().all()  # noqa: E712
        if row and (row.username != user["username"] or row.name != user["name"]):
            row.username, row.name = user["username"], user["name"]
            await s.commit()
    save = json.loads(row.data) if row and row.data else None
    return web.json_response({"ok": True, "admin": user["admin"], "tg": {"id": user["id"], "username": user["username"]},
                              "save": save, "grants": [grant_dict(g) for g in grants]})


async def api_save(request):
    body, user = await read_auth(request)
    data = body.get("data")
    raw = json.dumps(data, ensure_ascii=False)
    if not isinstance(data, dict) or len(raw.encode()) > MAX_SAVE_BYTES:
        raise web.HTTPBadRequest(text="bad save")
    async with SessionLocal() as s:
        row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        if not row:
            row = GameSave(tg_id=user["id"])
            s.add(row)
        row.username, row.name, row.data, row.updated = user["username"], user["name"], raw, int(time.time())
        await s.commit()
    return web.json_response({"ok": True})


async def api_ack(request):
    body, user = await read_auth(request)
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()][:100]
    async with SessionLocal() as s:
        rows = (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.id.in_(ids)))).scalars().all()
        for g in rows:
            g.applied = True
        await s.commit()
    return web.json_response({"ok": True})


async def api_admin_grant(request):
    body, user = await read_auth(request)
    if not user["admin"]:
        raise web.HTTPForbidden(text="not admin")
    kind = body.get("kind")
    if kind not in GRANT_KINDS:
        raise web.HTTPBadRequest(text="bad kind")
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount < 1 or amount > LIMITS[kind]:
        return web.json_response({"ok": False, "error": f"Количество: от 1 до {LIMITS[kind]}"})
    payload = {"amount": amount}
    if kind == "item":
        item, grade = body.get("item"), body.get("grade", 0)
        if item not in GEAR_IDS or grade not in (0, 1, 2, 3):
            return web.json_response({"ok": False, "error": "Неизвестный предмет или грейд"})
        payload.update(item=item, grade=grade)

    target = str(body.get("target", "")).strip().lstrip("@").lower()
    async with SessionLocal() as s:
        if target in ("", "me"):
            tg_id, target_name = user["id"], user["username"] or user["name"]
        else:
            row = (await s.execute(select(GameSave).where(func.lower(GameSave.username) == target))).scalar_one_or_none()
            if not row:
                return web.json_response({"ok": False, "error": "Игрок @" + target + " ещё не заходил в игру"})
            tg_id, target_name = row.tg_id, row.username
        g = Grant(tg_id=tg_id, kind=kind, payload=json.dumps(payload), by_admin=user["username"], created=int(time.time()))
        s.add(g)
        await s.commit()
        gd = grant_dict(g)
    log.info("Админ @%s выдал %s %s игроку %s", user["username"], kind, payload, target_name)
    delivered = await push_to_player(tg_id, {"t": "grant", "grant": gd})
    return web.json_response({"ok": True, "online": delivered, "target": target_name})


# ---------- живой мир: WebSocket ----------
clients = {}                 # ws -> данные игрока
chat_history = deque(maxlen=60)


def clean_pos(d, info):
    """Берём из сообщения только допустимые поля, чтобы нельзя было прислать мусор другим игрокам."""
    try:
        loc = d.get("loc")
        if loc in LOCS:
            info["loc"] = loc
        for k in ("x", "y"):
            info[k] = max(0.0, min(3000.0, float(d.get(k, 0))))
        for k in ("ang", "aim"):
            info[k] = round(float(d.get(k, 0)), 2)
        info["moving"] = bool(d.get("moving"))
        info["dead"] = bool(d.get("dead"))
        nick = str(d.get("nick", ""))[:16].strip()
        info["nick"] = nick or info["name"][:16]
        info["fac"] = d.get("fac") if d.get("fac") in FACTIONS else "aegis"
        info["lvl"] = max(1, min(999, int(d.get("lvl", 1))))
        eq = d.get("eq") or {}
        info["eq"] = {k: int(v) for k, v in eq.items() if k in {"head", "weapon", "module", "armor", "core", "legs"} and v in (0, 1, 2, 3)}
        info["wpn"] = str(d.get("wpn", ""))[:12]
        info["seen"] = time.time()
    except (TypeError, ValueError):
        pass


def public(info):
    return {k: info.get(k) for k in ("id", "nick", "fac", "lvl", "x", "y", "ang", "aim", "moving", "dead", "eq", "wpn", "admin")}


async def push_to_player(tg_id, payload):
    sent = False
    for ws, info in list(clients.items()):
        if info.get("id") == tg_id and not ws.closed:
            try:
                await ws.send_json(payload)
                sent = True
            except Exception:
                pass
    return sent


async def broadcast(payload, only=None):
    for ws, info in list(clients.items()):
        if ws.closed or (only and not only(info)):
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=32 * 1024)
    await ws.prepare(request)
    info = None
    last_chat = 0.0
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = d.get("t")
            if info is None:
                if t != "auth":
                    continue
                user = auth(d.get("initData", ""))
                if not user:
                    await ws.close()
                    break
                info = {**user, "loc": "lobby", "x": 500, "y": 640, "ang": 0, "aim": 0, "moving": False, "dead": False,
                        "nick": user["name"][:16], "fac": "aegis", "lvl": 1, "eq": {}, "wpn": "", "seen": time.time()}
                clients[ws] = info
                await ws.send_json({"t": "hello", "id": user["id"], "admin": user["admin"], "history": list(chat_history)})
                continue
            if t == "pos":
                clean_pos(d, info)
            elif t == "chat":
                now = time.time()
                text = str(d.get("text", "")).strip()[:200]
                ch = d.get("ch")
                if not text or ch not in ("world", "dm") or now - last_chat < 2:
                    continue
                last_chat = now
                m = {"id": f"{info['id']}-{int(now * 1000)}", "ch": ch, "text": text, "nick": info["nick"], "fac": info["fac"],
                     "lvl": info["lvl"], "uid": str(info["id"]), "admin": info["admin"], "ts": int(now * 1000)}
                if ch == "world":
                    chat_history.append(m)
                    await broadcast({"t": "chat", "m": m})
                else:
                    to = str(d.get("to", ""))[:16]
                    m["to"] = to
                    await broadcast({"t": "chat", "m": m}, only=lambda i: i["nick"] == to or i["id"] == info["id"])
    finally:
        clients.pop(ws, None)
    return ws


async def world_loop():
    """10 раз в секунду рассылаем каждому игроку остальных пилотов в его локации."""
    while True:
        await asyncio.sleep(0.1)
        by_loc = {}
        for info in clients.values():
            by_loc.setdefault(info["loc"], []).append(info)
        for ws, info in list(clients.items()):
            others = [public(o) for o in by_loc.get(info["loc"], []) if o is not info and o["id"] != info["id"]]
            if ws.closed:
                continue
            try:
                await ws.send_json({"t": "players", "loc": info["loc"], "list": others})
            except Exception:
                pass


async def start_web(port: int):
    app = web.Application(client_max_size=512 * 1024)
    app.router.add_get("/", game_page)
    app.router.add_get("/health", health)
    app.router.add_post("/api/state/load", api_load)
    app.router.add_post("/api/state/save", api_save)
    app.router.add_post("/api/grants/ack", api_ack)
    app.router.add_post("/api/admin/grant", api_admin_grant)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    asyncio.create_task(world_loop())
    log.info("Игра доступна на порту %s", port)
