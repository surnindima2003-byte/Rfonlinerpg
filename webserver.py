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

import re
import secrets

from config import BOT_TOKEN, ADMIN_USERNAMES, DATA_EPOCH
from db import SessionLocal, engine
from models import Base, GameSave, Grant, Doc, Meta

GAME_FILE = Path(__file__).parent / "game.html"
MAX_SAVE_BYTES = 300_000
LOCS = {"lobby", "sector1", "scrapfields", "reactor_ruins", "iron_canyon"}
FACTIONS = {"aegis", "vex", "core"}
CLASSES = {"", "guard", "reaper", "sniper", "techno"}
GRANT_KINDS = {"scrap", "cores", "exp", "level", "item"}
GEAR_IDS = {
    "st_head", "st_weapon", "st_module", "st_armor", "st_core", "st_legs",
    "laser1", "laser2", "plasma", "sensor1", "sensor2", "plate1", "plate2",
    "tracks1", "tracks2", "servo", "shieldgen", "reactor1", "reactor2",
    "kit_s", "kit_l", "wire", "plate", "chip", "sph_cu", "sph_ti",
}
LIMITS = {"scrap": 1_000_000, "cores": 100_000, "exp": 1_000_000, "level": 50, "item": 50}

log = logging.getLogger("web")


# ---------- сброс базы ----------
def ensure_epoch():
    """Если метка сброса изменилась, удаляем ВСЕ таблицы и создаём заново (один раз)."""
    Meta.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        row = conn.execute(Meta.__table__.select().where(Meta.key == "epoch")).first()
        current = row.value if row else None
    if current == DATA_EPOCH:
        return
    log.warning("СБРОС БАЗЫ: эпоха %s -> %s, все данные удаляются", current, DATA_EPOCH)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(Meta.__table__.insert().values(key="epoch", value=DATA_EPOCH))


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
    return web.json_response({"ok": True, "epoch": DATA_EPOCH, "admin": user["admin"], "tg": {"id": user["id"], "username": user["username"]},
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
        s_ = data.get("S") if isinstance(data.get("S"), dict) else {}
        try:
            row.bm = max(0, min(10_000_000, int(data.get("bm", 0))))
            row.lvl = max(1, min(999, int(s_.get("level", 1))))
        except (TypeError, ValueError):
            pass
        new_nick = str(s_.get("name", ""))[:16]
        if new_nick and new_nick != row.nick:
            clash = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == new_nick.lower(), GameSave.tg_id != user["id"]))).scalar_one_or_none()
            if not clash:
                row.nick = new_nick
        row.cls = s_.get("cls") if s_.get("cls") in ("guard", "reaper", "sniper", "techno") else ""
        row.guild_id = str(s_.get("guildId", ""))[:64]
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


# ---------- гильдии: хранилище документов с проверкой прав ----------
SEG = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_DOC = 8_000


def parse_path(path):
    """guilds/{g} | guilds/{g}/{members|requests|log}/{id}. Возвращает (gid, sub, id) или None."""
    parts = str(path).split("/")
    if len(parts) == 2 and parts[0] == "guilds" and SEG.match(parts[1]):
        return parts[1], None, None
    if len(parts) == 4 and parts[0] == "guilds" and SEG.match(parts[1]) and parts[2] in ("members", "requests", "log") and SEG.match(parts[3]):
        return parts[1], parts[2], parts[3]
    return None


def parse_col(col):
    parts = str(col).split("/")
    if col == "guilds":
        return True
    return len(parts) == 3 and parts[0] == "guilds" and SEG.match(parts[1]) and parts[2] in ("members", "requests", "log")


async def doc_get(s, path):
    row = (await s.execute(select(Doc).where(Doc.path == path))).scalar_one_or_none()
    return (row, json.loads(row.data)) if row else (None, None)


async def doc_put(s, path, data):
    row, _ = await doc_get(s, path)
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw.encode()) > MAX_DOC:
        raise web.HTTPBadRequest(text="doc too big")
    if not row:
        row = Doc(path=path, col=path.rsplit("/", 1)[0])
        s.add(row)
    row.data, row.updated = raw, int(time.time() * 1000)


def deny(msg="нет прав"):
    raise web.HTTPForbidden(text=msg)


async def check_write(s, op, path, data, uid):
    """Права ролей проверяет сервер: подделать их со страницы нельзя."""
    p = parse_path(path)
    if not p:
        raise web.HTTPBadRequest(text="bad path")
    gid, sub, did = p
    _, guild = await doc_get(s, f"guilds/{gid}")
    _, me = await doc_get(s, f"guilds/{gid}/members/{uid}")
    my_role = me.get("role") if me else None
    is_leader = bool(guild) and guild.get("leader") == uid

    if sub is None:  # сама гильдия
        if op == "set":
            if guild:
                deny()
            if data.get("leader") != uid:
                deny()
            name, tag = str(data.get("name", "")).strip().lower(), str(data.get("tag", "")).strip().upper()
            others = (await s.execute(select(Doc).where(Doc.col == "guilds"))).scalars().all()
            for o in others:
                od = json.loads(o.data)
                if str(od.get("name", "")).lower() == name or str(od.get("tag", "")).upper() == tag:
                    deny("название или тег заняты")
            return
        if not guild:
            deny()
        if op == "delete":
            if not is_leader:
                deny()
            return
        if op == "update":
            allowed = {"desc", "open", "minLvl", "emblem", "level", "spent", "leader", "leaderNick", "count"} if is_leader else ({"count", "spent"} if me else {"count"})
            if set(data) - allowed:
                deny()
            return
        deny()

    if not guild:
        deny("гильдии нет")
    if sub == "members":
        if op == "set":
            if did == uid:
                role = data.get("role")
                if role == "leader" and is_leader:
                    return
                if role == "member" and (guild.get("open") or is_leader):
                    return
                deny("гильдия по заявкам")
            if my_role in ("leader", "officer") and data.get("role") == "member":
                _, req = await doc_get(s, f"guilds/{gid}/requests/{did}")
                if req:
                    return
            deny()
        if op == "update":
            keys = set(data)
            if did == uid and keys <= {"donated", "xp", "lvl", "nick", "bm", "role"}:
                if "role" in keys and not (is_leader or data.get("role") == my_role):
                    deny()
                return
            if is_leader and keys <= {"role"}:
                return
            deny()
        if op == "delete":
            if did == uid or is_leader:
                return
            _, target = await doc_get(s, path)
            if my_role == "officer" and target and target.get("role") == "member":
                return
            deny()
    if sub == "requests":
        if op == "set" and did == uid:
            return
        if op == "delete" and (did == uid or my_role in ("leader", "officer")):
            return
        deny()
    if sub == "log":
        if op in ("set", "add") and me:
            return
        if op == "delete" and is_leader:
            return
        deny()
    deny()


def doc_view(path, data):
    return {"id": path.rsplit("/", 1)[1], "data": data}


async def api_db(request):
    body, user = await read_auth(request)
    uid = str(user["id"])
    op = body.get("op")
    async with SessionLocal() as s:
        if op == "get":
            path = str(body.get("path", ""))
            if not parse_path(path):
                raise web.HTTPBadRequest(text="bad path")
            _, data = await doc_get(s, path)
            return web.json_response({"ok": True, "exists": data is not None, "data": data})
        if op == "list":
            col = str(body.get("col", ""))
            if not parse_col(col):
                raise web.HTTPBadRequest(text="bad col")
            q = body.get("q") or {}
            rows = (await s.execute(select(Doc).where(Doc.col == col))).scalars().all()
            docs = [doc_view(r.path, json.loads(r.data)) for r in rows]
            for f, o, v in (q.get("where") or [])[:5]:
                if o == "==":
                    docs = [d for d in docs if d["data"].get(f) == v]
            if q.get("order"):
                f, direction = q["order"][0], q["order"][1] if len(q["order"]) > 1 else "asc"
                docs.sort(key=lambda d: (d["data"].get(f) is None, d["data"].get(f, 0)), reverse=direction == "desc")
            limit = int(q.get("limit") or 200)
            return web.json_response({"ok": True, "docs": docs[:min(limit, 200)]})
        if op in ("set", "update", "delete", "add"):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            if op == "add":
                col = str(body.get("col", ""))
                if not parse_col(col) or col == "guilds":
                    raise web.HTTPBadRequest(text="bad col")
                path = f"{col}/a{int(time.time()*1000):x}{secrets.token_hex(3)}"
            else:
                path = str(body.get("path", ""))
            await check_write(s, "add" if op == "add" else op, path, data, uid)
            if op == "delete":
                row, _ = await doc_get(s, path)
                if row:
                    await s.execute(Doc.__table__.delete().where(Doc.path == path))
            elif op == "update":
                row, cur = await doc_get(s, path)
                if cur is None:
                    raise web.HTTPNotFound(text="no doc")
                await doc_put(s, path, {**cur, **data})
            else:
                await doc_put(s, path, data)
            await s.commit()
            return web.json_response({"ok": True, "id": path.rsplit("/", 1)[1]})
    raise web.HTTPBadRequest(text="bad op")


# ---------- позывной: проверка, что имя свободно ----------
RESERVED_NICKS = {"пилот", "pilot", "admin", "админ", "administrator", "администратор", "moderator", "модератор", "system", "система"}


def valid_nick(name):
    return 3 <= len(name) <= 16 and name == name.strip() and "  " not in name and all(ch.isalnum() or ch in "_- " for ch in name)


async def api_name(request):
    body, user = await read_auth(request)
    name = str(body.get("name", "")).strip()
    if not valid_nick(name):
        return web.json_response({"ok": False, "error": "3–16 символов: буквы, цифры, пробел, _ или -"})
    if name.lower() in RESERVED_NICKS:
        return web.json_response({"ok": False, "error": "Этот позывной нельзя использовать"})
    async with SessionLocal() as s:
        taken = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == name.lower(), GameSave.tg_id != user["id"]))).scalar_one_or_none()
        if taken:
            return web.json_response({"ok": False, "error": "Этот позывной уже занят"})
        row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        if not row:
            row = GameSave(tg_id=user["id"], username=user["username"], name=user["name"], data="", updated=int(time.time()))
            s.add(row)
        row.nick = name
        await s.commit()
    return web.json_response({"ok": True})


# ---------- рейтинг по боевой мощи ----------
async def api_top(request):
    body, user = await read_auth(request)
    kind = body.get("kind")
    async with SessionLocal() as s:
        guild_rows = (await s.execute(select(Doc).where(Doc.col == "guilds"))).scalars().all()
        guilds = {r.path.split("/")[1]: json.loads(r.data) for r in guild_rows}
        if kind == "players":
            rows = (await s.execute(select(GameSave).where(GameSave.bm > 0).order_by(GameSave.bm.desc()).limit(100))).scalars().all()
            me = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
            my_rank = None
            if me and me.bm > 0:
                my_rank = (await s.execute(select(func.count()).select_from(GameSave).where(GameSave.bm > me.bm))).scalar() + 1
            items = [{"id": str(r.tg_id), "nick": r.nick or r.name[:16], "lvl": r.lvl, "cls": r.cls, "bm": r.bm,
                      "tag": (guilds.get(r.guild_id) or {}).get("tag", "")} for r in rows]
            return web.json_response({"ok": True, "items": items, "me": {"rank": my_rank, "bm": me.bm if me else 0}})
        if kind == "guilds":
            member_rows = (await s.execute(select(Doc).where(Doc.col.like("guilds/%/members")))).scalars().all()
            uids = {}
            for r in member_rows:
                gid = r.path.split("/")[1]
                uid = r.path.rsplit("/", 1)[1]
                if uid.isdigit():
                    uids.setdefault(gid, []).append(int(uid))
            all_ids = [i for lst in uids.values() for i in lst]
            bms = {}
            if all_ids:
                for tg_id, bm in (await s.execute(select(GameSave.tg_id, GameSave.bm).where(GameSave.tg_id.in_(all_ids)))).all():
                    bms[tg_id] = bm or 0
            items = []
            for gid, g in guilds.items():
                members = uids.get(gid, [])
                items.append({"id": gid, "name": g.get("name", ""), "tag": g.get("tag", ""), "emblem": g.get("emblem"),
                              "level": g.get("level", 1), "count": len(members), "bm": sum(bms.get(m, 0) for m in members)})
            items.sort(key=lambda x: x["bm"], reverse=True)
            my_gid = next((gid for gid, lst in uids.items() if user["id"] in lst), None)
            my_rank = next((i + 1 for i, it in enumerate(items) if it["id"] == my_gid), None)
            return web.json_response({"ok": True, "items": items[:50], "me": {"rank": my_rank, "id": my_gid}})
    raise web.HTTPBadRequest(text="bad kind")


# ---------- живой мир: WebSocket ----------
clients = {}                 # ws -> данные игрока
chat_history = deque(maxlen=60)

# ---------- пати (до 4 игроков, живёт в памяти сервера) ----------
PARTY_MAX = 4
parties = {}                 # id пати -> {"id", "leader", "members": [tg_id, ...]}
member_party = {}            # tg_id -> id пати
invites = {}                 # tg_id приглашённого -> {tg_id пригласившего: время}
last_heal = {}               # tg_id -> время последнего лечения


def online(uid):
    return next((i for i in clients.values() if i.get("id") == uid), None)


def party_payload(pid):
    pt = parties.get(pid)
    if not pt:
        return None
    members = []
    for uid in pt["members"]:
        i = online(uid) or {}
        members.append({"id": uid, "nick": i.get("nick", "?"), "lvl": i.get("lvl", 1), "cls": i.get("cls", ""),
                        "hp": i.get("hp", 0), "mhp": i.get("mhp", 1), "loc": i.get("loc", ""), "online": bool(i)})
    return {"id": pid, "leader": pt["leader"], "members": members}


async def send_party(pid, extra_uids=()):
    payload = {"t": "party", "party": party_payload(pid)}
    targets = set(parties.get(pid, {}).get("members", [])) | set(extra_uids)
    for uid in targets:
        await push_to_player(uid, payload if uid in parties.get(pid, {}).get("members", []) else {"t": "party", "party": None})


async def party_leave(uid, kicked=False):
    pid = member_party.pop(uid, None)
    if not pid or pid not in parties:
        return
    pt = parties[pid]
    if uid in pt["members"]:
        pt["members"].remove(uid)
    await push_to_player(uid, {"t": "party", "party": None})
    if kicked:
        await push_to_player(uid, {"t": "pinfo", "text": "Тебя исключили из пати"})
    if len(pt["members"]) <= 1:
        for rest in pt["members"]:
            member_party.pop(rest, None)
            await push_to_player(rest, {"t": "party", "party": None})
            await push_to_player(rest, {"t": "pinfo", "text": "Пати распущена"})
        parties.pop(pid, None)
        return
    if pt["leader"] == uid:
        pt["leader"] = pt["members"][0]
    await send_party(pid)


async def handle_party(d, info):
    t, uid = d.get("t"), info["id"]
    try:
        other = int(d.get("to") or d.get("from") or d.get("id") or 0)
    except (TypeError, ValueError):
        other = 0
    if t == "pinv":
        tgt = online(other)
        if not tgt or other == uid:
            return await push_to_player(uid, {"t": "pinfo", "text": "Игрок не в сети"})
        if other in member_party:
            return await push_to_player(uid, {"t": "pinfo", "text": "Игрок уже в пати"})
        pid = member_party.get(uid)
        if pid and (parties[pid]["leader"] != uid):
            return await push_to_player(uid, {"t": "pinfo", "text": "Приглашать может только лидер пати"})
        if pid and len(parties[pid]["members"]) >= PARTY_MAX:
            return await push_to_player(uid, {"t": "pinfo", "text": "В пати уже 4 игрока"})
        invites.setdefault(other, {})[uid] = time.time()
        await push_to_player(other, {"t": "pinv", "from": {"id": uid, "nick": info["nick"], "lvl": info["lvl"], "cls": info.get("cls", "")}})
        await push_to_player(uid, {"t": "pinfo", "text": "Приглашение отправлено"})
    elif t == "pacc":
        ts = invites.get(uid, {}).pop(other, None)
        if not ts or time.time() - ts > 60 or not online(other):
            return await push_to_player(uid, {"t": "pinfo", "text": "Приглашение устарело"})
        pid = member_party.get(other)
        if not pid:
            pid = f"p{other}-{int(time.time())}"
            parties[pid] = {"id": pid, "leader": other, "members": [other]}
            member_party[other] = pid
        if len(parties[pid]["members"]) >= PARTY_MAX:
            return await push_to_player(uid, {"t": "pinfo", "text": "В пати уже 4 игрока"})
        if uid in member_party:
            await party_leave(uid)
        parties[pid]["members"].append(uid)
        member_party[uid] = pid
        await send_party(pid)
    elif t == "pdec":
        if invites.get(uid, {}).pop(other, None):
            await push_to_player(other, {"t": "pinfo", "text": info["nick"] + " отклонил приглашение"})
    elif t == "pleave":
        await party_leave(uid)
    elif t == "pkick":
        pid = member_party.get(uid)
        if pid and parties[pid]["leader"] == uid and other in parties[pid]["members"] and other != uid:
            await party_leave(other, kicked=True)
    elif t == "heal":
        pid = member_party.get(uid)
        tgt = online(other)
        if not pid or not tgt or member_party.get(other) != pid or other == uid:
            return
        if tgt["loc"] != info["loc"] or ((tgt["x"] - info["x"]) ** 2 + (tgt["y"] - info["y"]) ** 2) ** 0.5 > 380:
            return
        if time.time() - last_heal.get(uid, 0) < 10:
            return
        last_heal[uid] = time.time()
        try:
            amount = int(d.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        amount = max(1, min(amount, 20 + 5 * info["lvl"], 400))
        await push_to_player(other, {"t": "healed", "from": info["nick"], "amount": amount})
    elif t == "pxp":
        pid = member_party.get(uid)
        if not pid:
            return
        try:
            amount = max(0, min(int(d.get("amount", 0)), 500))
        except (TypeError, ValueError):
            return
        share = amount * 4 // 10
        if share <= 0:
            return
        for m in parties[pid]["members"]:
            i = online(m)
            if m != uid and i and i["loc"] == info["loc"]:
                await push_to_player(m, {"t": "pxp", "amount": share, "from": info["nick"]})


def clean_pos(d, info):
    """Берём из сообщения только допустимые поля, чтобы нельзя было прислать мусор другим игрокам."""
    try:
        loc = d.get("loc")
        if loc in LOCS:
            info["loc"] = loc
        for k in ("x", "y"):
            info[k] = max(0.0, min(4000.0, float(d.get(k, 0))))
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
        info["cls"] = d.get("cls") if d.get("cls") in CLASSES else ""
        # гильдия над головой: тег, название, эмблема
        info["hp"] = max(0, min(100000, int(d.get("hp", 0))))
        info["mhp"] = max(1, min(100000, int(d.get("mhp", 1))))
        info["cp"] = max(0, min(1000000, int(d.get("cp", 0))))
        info["mcp"] = max(1, min(1000000, int(d.get("mcp", 1))))
        info["bm"] = max(0, min(10_000_000, int(d.get("bm", 0))))
        info["gt"] = str(d.get("gt", ""))[:4]
        info["gn"] = str(d.get("gn", ""))[:20]
        info["gi"] = d.get("gi") if d.get("gi") in ("gear", "shield", "bolt", "crown", "claw", "star") else ""
        info["gc"] = d.get("gc") if isinstance(d.get("gc"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", d.get("gc")) else ""
        info["seen"] = time.time()
    except (TypeError, ValueError):
        pass


def public(info):
    return {k: info.get(k) for k in ("id", "nick", "fac", "lvl", "x", "y", "ang", "aim", "moving", "dead", "eq", "wpn", "cls", "gt", "gn", "gi", "gc", "hp", "mhp", "cp", "mcp", "bm", "admin")}


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
            elif t in ("pinv", "pacc", "pdec", "pleave", "pkick", "heal", "pxp"):
                await handle_party(d, info)
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
        if info and not online(info["id"]):
            await party_leave(info["id"])
    return ws


async def world_loop():
    """10 раз в секунду рассылаем каждому игроку остальных пилотов в его локации, раз в секунду — состав пати."""
    tick = 0
    while True:
        await asyncio.sleep(0.1)
        tick += 1
        if tick % 10 == 0:
            for pid in list(parties):
                await send_party(pid)
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
    ensure_epoch()
    app = web.Application(client_max_size=512 * 1024)
    app.router.add_get("/", game_page)
    app.router.add_get("/health", health)
    app.router.add_post("/api/state/load", api_load)
    app.router.add_post("/api/state/save", api_save)
    app.router.add_post("/api/grants/ack", api_ack)
    app.router.add_post("/api/admin/grant", api_admin_grant)
    app.router.add_post("/api/db", api_db)
    app.router.add_post("/api/top", api_top)
    app.router.add_post("/api/name", api_name)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    asyncio.create_task(world_loop())
    log.info("Игра доступна на порту %s", port)
