"""Веб-сервер игры: страница мини-приложения, API сохранений и админки, WebSocket живого мира."""
import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from aiohttp import web, WSMsgType
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import select, func, update

import re
import secrets

from config import BOT_TOKEN, ADMIN_USERNAMES, MOD_USERNAMES, DATA_EPOCH, ENV_NAME
from config import (WORLD_HZ, VIEW_RADIUS, WS_MAX_PER_UID, WS_IN_RATE, WS_IN_BURST, WS_AUTH_TIMEOUT,
                    METRICS_TOKEN, LOADTEST, LOADTEST_UID_BASE)
from db import SessionLocal, engine, db_ping, dispose as db_dispose
import metrics
import realtime
from models import Base, GameSave, Grant, Doc, Meta, MarketLot, MarketHist, Player
import chipwar
import funnel
import mobguard
from db_atomic import insert_ignore
from game_data import STARTING_STATS
import gram
import items
import pvp
import pvpguard
import progress
import stats
import special_quests
import saveguard
import mobworld
import seasonpts
import worldboss
import tower
from config import WEBAPP_URL

GAME_FILE = Path(__file__).parent / "game.html"
GUIDE_FILE = Path(__file__).parent / "guide.html"
MAX_SAVE_BYTES = 300_000
LOCS = {"lobby", "sector1", "sector2", "scrapfields", "reactor_ruins", "iron_canyon", "arena_fear", "tower"}
SAFE_LOCS = {"lobby", "arena_fear", "tower"}          # здесь PvP нет никогда
FACTIONS = {"aegis", "vex", "core"}
CLASSES = {"", "guard", "reaper", "sniper", "techno", "ghost", "glyph", "medic"}
GRANT_KINDS = {"scrap", "cores", "exp", "level", "item"}
GEAR_IDS = items.GEAR_IDS | {"kit_s", "kit_l", "wire", "plate", "chip", "sph_cu", "sph_ti"}
LIMITS = {"scrap": 1_000_000, "cores": 100_000, "exp": 1_000_000, "level": 50, "item": 50}

log = logging.getLogger("web")


# ---------- сброс базы ----------
# Эти таблицы вайп НЕ трогает: деньги игроков (GRAM, звёзды, выводы, рефералы) и служебная статистика.
# Всё остальное — игровой прогресс (сохранения, вещи, рейтинг, маркет) — обнуляется.
KEEP_ON_WIPE = {"meta", "gram_wallets", "gram_tx", "gram_withdrawals", "referrals", "ref_earn", "star_payments", "season_prizes",
                "client_errors", "funnel_events", "first_seen"}


def ensure_epoch():
    """Если метка сброса изменилась, один раз обнуляем игровой прогресс. Деньги и статистика сохраняются."""
    Meta.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        row = conn.execute(Meta.__table__.select().where(Meta.key == "epoch")).first()
        current = row.value if row else None
    if current == DATA_EPOCH:
        return
    game_tables = [t for t in Base.metadata.sorted_tables if t.name not in KEEP_ON_WIPE]
    log.warning("СБРОС ПРОГРЕССА: эпоха %s -> %s. Обнуляются: %s. Сохраняются: %s",
                current, DATA_EPOCH, ", ".join(t.name for t in game_tables), ", ".join(sorted(KEEP_ON_WIPE)))
    Base.metadata.drop_all(engine, tables=game_tables)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if row:
            conn.execute(Meta.__table__.update().where(Meta.key == "epoch").values(value=DATA_EPOCH))
        else:
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
    return {"id": u.id, "username": username, "name": u.first_name or "Пилот", "start": str(data.start_param or "")[:64],
            "admin": username in ADMIN_USERNAMES, "mod": username in MOD_USERNAMES or username in ADMIN_USERNAMES}


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
_game_cache = {"mtime": 0, "body": b""}


async def game_page(request):
    """Игра отдаётся сжатой (gzip): грузится в несколько раз быстрее на мобильном интернете."""
    st = GAME_FILE.stat()
    if st.st_mtime != _game_cache["mtime"]:
        _game_cache.update(mtime=st.st_mtime, body=GAME_FILE.read_bytes())
    resp = web.Response(body=_game_cache["body"], content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-cache"})
    resp.enable_compression()
    return resp


async def guide_page(request):
    """Public game guide. Unlike the WebApp, it does not require Telegram auth."""
    return web.FileResponse(
        GUIDE_FILE,
        headers={"Cache-Control": "public, max-age=300"},
    )


async def health(request):
    return web.Response(text="ok")


async def live(request):
    """Процесс жив и event loop отвечает."""
    return web.Response(text="ok")


async def ready(request):
    """Готов принимать игроков: старт завершён, база отвечает, фоновые задачи живы."""
    problems = []
    if not STATE["ready"] or STATE["stopping"]:
        problems.append("starting" if not STATE["stopping"] else "stopping")
    for name in ("world", "cleanup"):
        t = STATE["tasks"].get(name)
        if not t or t.done():
            problems.append(f"task {name} down")
    if not await db_ping():
        problems.append("db")
    if problems:
        return web.json_response({"ok": False, "problems": problems}, status=503)
    return web.json_response({"ok": True, "players": len(hub.conns)})


async def metrics_page(request):
    """Метрики в JSON. Доступ: заголовок X-Metrics-Token или ?token=. Без METRICS_TOKEN эндпоинта нет."""
    token = request.headers.get("X-Metrics-Token") or request.query.get("token", "")
    if not METRICS_TOKEN or not secrets.compare_digest(token, METRICS_TOKEN):
        raise web.HTTPNotFound()
    hub.gauges()
    data = metrics.summary()
    data["env"] = ENV_NAME
    data["world_hz"] = WORLD_HZ
    data["view_radius"] = VIEW_RADIUS
    return web.json_response(data)


async def api_load(request):
    _, user = await read_auth(request)
    await stats.mark_seen(user["id"])
    funnel.mark(user["id"], "app_open")
    m = re.fullmatch(r"ref_(\d{3,15})", user.get("start", ""))
    if m:
        await gram.bind_referral(user["id"], int(m.group(1)))
    async with SessionLocal() as s:
        row = (await s.execute(select(GameSave).where(
            GameSave.tg_id == user["id"], GameSave.epoch == DATA_EPOCH
        ))).scalar_one_or_none()
        grants = (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.applied == False))).scalars().all()  # noqa: E712
        if row and (row.username != user["username"] or row.name != user["name"]):
            row.username, row.name = user["username"], user["name"]
            await s.commit()
    save = json.loads(row.data) if row and row.data else None
    return web.json_response({"ok": True, "epoch": DATA_EPOCH, "env": ENV_NAME, "admin": user["admin"], "tg": {"id": user["id"], "username": user["username"]},
                              "save": save, "grants": [grant_dict(g) for g in grants]})


async def api_save(request):
    body, user = await read_auth(request)
        # Открытая до сброса вкладка продолжает посылать старый локальный инвентарь.
    # Не принимаем его ни от обычного игрока, ни от администратора.
    if body.get("epoch") != DATA_EPOCH:
        raise web.HTTPConflict(text="stale epoch")
    data = body.get("data")
    raw = json.dumps(data, ensure_ascii=False)
    if not isinstance(data, dict) or len(raw.encode()) > MAX_SAVE_BYTES:
        raise web.HTTPBadRequest(text="bad save")
    async with SessionLocal() as s:
        row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        if not row:
            row = GameSave(tg_id=user["id"])
            s.add(row)
        elif not user["admin"] and saveguard.MODE != "off" and row.epoch == DATA_EPOCH and row.data:
            # проверка сохранения: структура и скачки лома/ядер/заточки относительно прошлого сохранения
            try:
                old = json.loads(row.data)
            except ValueError:
                old = None
            since = int(row.updated or 0)
            granted = {}
            for g in (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.created >= since))).scalars().all():
                try:
                    pl = json.loads(g.payload or "{}")
                    key = "sph" if g.kind == "item" and pl.get("item") in saveguard.SPHERES else g.kind
                    granted[key] = granted.get(key, 0) + int(pl.get("amount", 0))
                except (ValueError, TypeError, AttributeError):
                    pass
            data, notes = saveguard.check(user["id"], user["username"] or user["name"], old, data, time.time() - since, granted)
            if notes:
                log.warning("Сохранение uid=%s (%s): %s", user["id"], saveguard.MODE, "; ".join(f"{a}: {b}" for a, b in notes)[:500])
            if data is None:
                return web.json_response({"ok": False, "error": "save rejected"}, status=409)
            raw = json.dumps(data, ensure_ascii=False)
        row.epoch = DATA_EPOCH
        row.username, row.name, row.data, row.updated = user["username"], user["name"], raw, int(time.time())
        s_ = data.get("S") if isinstance(data.get("S"), dict) else {}
        try:
            # уровень и боевая мощь для рейтинга — не выше того, что подтвердил сервер (админам без ограничений)
            cap = 999 if user["admin"] else await progress.cap_of(s, user["id"])
            row.lvl = max(1, min(cap, int(s_.get("level", 1))))
            row.bm = max(0, min(10_000_000 if user["admin"] else progress.bm_cap(row.lvl), int(data.get("bm", 0))))
        except (TypeError, ValueError):
            pass
        new_nick = str(s_.get("name", ""))[:16]
        if new_nick and new_nick != row.nick:
            clash = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == new_nick.lower(), GameSave.tg_id != user["id"]))).scalar_one_or_none()
            if not clash:
                row.nick = new_nick
        row.cls = s_.get("cls") if s_.get("cls") in CLASSES and s_.get("cls") else ""
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


async def admin_set_name(user, body):
    """Админ меняет позывной игроку: проверка формата и занятости, запись в сохранение, обновление в игре."""
    name = str(body.get("name", "")).strip()
    if not valid_nick(name):
        return web.json_response({"ok": False, "error": "Позывной: 3–16 символов — буквы, цифры, пробел, _ или -"})
    if name.lower() in RESERVED_NICKS:
        return web.json_response({"ok": False, "error": "Этот позывной нельзя использовать"})
    target = str(body.get("target", "")).strip().lstrip("@").lower()
    async with SessionLocal() as s:
        if target in ("", "me"):
            row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        else:
            row = (await s.execute(select(GameSave).where(func.lower(GameSave.username) == target))).scalar_one_or_none()
        if not row:
            return web.json_response({"ok": False, "error": ("Игрок @" + target if target not in ("", "me") else "Ты") + " ещё не заходил в игру"})
        taken = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == name.lower(), GameSave.tg_id != row.tg_id))).scalar_one_or_none()
        if taken:
            return web.json_response({"ok": False, "error": "Этот позывной уже занят"})
        old = row.nick or ""
        row.nick = name
        try:                                                     # чтобы при следующем входе в сохранении был новый позывной
            data = json.loads(row.data) if row.data else None
            if isinstance(data, dict) and isinstance(data.get("S"), dict):
                data["S"]["name"] = name
                row.data = json.dumps(data, ensure_ascii=False)
        except ValueError:
            pass
        tg_id, uname = row.tg_id, row.username or ""
        await s.commit()
    for c in list(hub.by_uid.get(tg_id, [])):                   # игрок в сети — меняем сразу
        c.info["nick"] = name
    delivered = await push_to_player(tg_id, {"t": "rename", "name": name, "by": user["username"]})
    log.warning("Админ @%s сменил позывной игрока %s: «%s» → «%s»", user["username"], tg_id, old, name)
    return web.json_response({"ok": True, "online": delivered, "target": uname or str(tg_id), "name": name, "old": old})


async def api_admin_name(request):
    """Старый адрес смены позывного — для клиентов, которые загрузили прошлую версию страницы."""
    body, user = await read_auth(request)
    if not user["admin"]:
        raise web.HTTPForbidden(text="not admin")
    return await admin_set_name(user, body)


async def api_admin_grant(request):
    body, user = await read_auth(request)
    if not user["admin"]:
        raise web.HTTPForbidden(text="not admin")
    kind = body.get("kind")
    if kind == "name":                                           # смена позывного идёт через тот же маршрут
        return await admin_set_name(user, body)
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
        is_book = isinstance(item, str) and re.fullmatch(r"b[kp]_[a-z]{2,20}", item)
        if (item not in GEAR_IDS and not is_book) or grade not in (0, 1, 2, 3):
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
        if kind == "level":
            await progress.add_levels(s, tg_id, amount)          # выданные уровни сервер тоже засчитывает
        elif kind == "exp":
            row_ = await progress.prog_of(s, tg_id)
            row_.exp = (row_.exp or 0) + amount
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


# ---------- маркет: торговля между игроками за лом ----------
MARKET_FEE = 0.05
ITEM_ID = re.compile(r"^[a-z0-9_]{2,24}$")


def clean_item(it):
    if not isinstance(it, dict) or not ITEM_ID.match(str(it.get("id", ""))):
        return None
    try:
        return {"id": it["id"], "g": max(0, min(3, int(it.get("g", 0)))), "e": max(0, min(15, int(it.get("e", 0)))), "n": max(1, min(999, int(it.get("n", 1))))}
    except (TypeError, ValueError):
        return None


def lot_view(r):
    return {"id": r.id, "seller_id": str(r.seller_id), "seller": r.seller_nick, "item": json.loads(r.item), "price": gram.g(r.price), "ts": r.created * 1000}


async def api_market(request):
    body, user = await read_auth(request)
    op = request.match_info["op"]
    uid = user["id"]
    async with SessionLocal() as s:
        if op == "list":
            rows = (await s.execute(select(MarketLot).order_by(MarketLot.created.desc()).limit(200))).scalars().all()
            return web.json_response({"ok": True, "lots": [lot_view(r) for r in rows]})
        if op == "mine":
            rows = (await s.execute(select(MarketLot).where(MarketLot.seller_id == uid).order_by(MarketLot.created.desc()))).scalars().all()
            hist = (await s.execute(select(MarketHist).where(MarketHist.tg_id == uid).order_by(MarketHist.ts.desc()).limit(60))).scalars().all()
            bought = gram.g(sum(x.price for x in hist if x.kind == "buy"))
            sold = gram.g(sum(x.price for x in hist if x.kind == "sell"))
            return web.json_response({"ok": True, "lots": [lot_view(r) for r in rows], "bought": bought, "sold": sold,
                                      "hist": [{"kind": x.kind, "item": json.loads(x.item), "price": gram.g(x.price), "other": x.other, "ts": x.ts * 1000} for x in hist]})
        if op == "create":
            item = clean_item(body.get("item"))
            try:
                price = int(round(float(body.get("price", 0)) * gram.NANO))     # цена в GRAM → нано-GRAM
            except (TypeError, ValueError):
                price = 0
            if not item or price < gram.NANO // 100 or price > 100_000 * gram.NANO:
                return web.json_response({"ok": False, "error": "Неверный лот"})
            count = (await s.execute(select(func.count()).select_from(MarketLot).where(MarketLot.seller_id == uid))).scalar()
            if count >= 20:
                return web.json_response({"ok": False, "error": "Не больше 20 лотов одновременно"})
            if isinstance(body.get("item"), dict) and body["item"].get("uid"):
                item["uid"] = str(body["item"]["uid"])[:24]
            real, err = await items.escrow_for_market(s, uid, item)     # только вещи из реестра сервера
            if err:
                return web.json_response({"ok": False, "error": err})
            save_row = (await s.execute(select(GameSave).where(GameSave.tg_id == uid))).scalar_one_or_none()
            nick = (save_row.nick if save_row and save_row.nick else user["name"])[:16]
            s.add(MarketLot(seller_id=uid, seller_nick=nick, item=json.dumps(real), price=price, created=int(time.time())))
            await s.commit()
            return web.json_response({"ok": True})
        if op in ("buy", "cancel"):
            try:
                lot_id = int(body.get("id"))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "Нет такого лота"})
            lot = (await s.execute(select(MarketLot).where(MarketLot.id == lot_id))).scalar_one_or_none()
            if not lot:
                return web.json_response({"ok": False, "error": "Лот уже продан или снят"})
            item = json.loads(lot.item)
            if op == "cancel":
                if lot.seller_id != uid:
                    return web.json_response({"ok": False, "error": "Это не твой лот"})
                if not await _take_lot(s, lot_id, seller=uid):
                    return web.json_response({"ok": False, "error": "Лот уже продан или снят"})
                await items.market_transfer(s, item, uid)                 # вещь возвращается продавцу
                await s.commit()
                return web.json_response({"ok": True, "item": item})
            if lot.seller_id == uid:
                return web.json_response({"ok": False, "error": "Нельзя купить свой лот"})
            buyer = (await s.execute(select(GameSave).where(GameSave.tg_id == uid))).scalar_one_or_none()
            buyer_nick = (buyer.nick if buyer and buyer.nick else user["name"])[:16]
            # кошельки создаём заранее: создание кошелька — отдельная транзакция, и она не должна
            # оказаться между «забрали лот» и «списали GRAM»
            bw = await gram.wallet_of(s, uid)
            await gram.wallet_of(s, lot.seller_id)
            # 1) забираем лот: DELETE … RETURNING. Из двух одновременных покупателей строку получит один,
            #    второй увидит 0 строк и ничего не заплатит
            if not await _take_lot(s, lot_id):
                return web.json_response({"ok": False, "error": "Лот уже продан или снят"})
            # 2) списываем GRAM атомарно; не хватило — откатываем всю операцию, лот остаётся на маркете
            from_locked = min(bw.locked or 0, lot.price)          # игровые GRAM из звёзд тратятся первыми
            if not await gram.move(s, uid, -lot.price, "market_buy", f"mkb:{lot_id}", "Маркет: покупка"):
                await s.rollback()
                return web.json_response({"ok": False, "error": "Не хватает GRAM"})
            if from_locked:
                await gram.adjust_locked(s, uid, -from_locked)
            payout = max(1, int(lot.price * (1 - MARKET_FEE)))
            await gram.move(s, lot.seller_id, payout, "market_sell", f"mks:{lot_id}", "Маркет: продажа")
            # игровые GRAM покупателя остаются игровыми и у продавца: через маркет звёзды не превратить в выводимые GRAM
            if from_locked:
                await gram.adjust_locked(s, lot.seller_id, int(from_locked * (1 - MARKET_FEE)))
            await items.market_transfer(s, item, uid)                     # вещь переходит покупателю
            now_ = int(time.time())
            s.add(MarketHist(tg_id=uid, kind="buy", item=lot.item, price=lot.price, other="@" + lot.seller_nick, ts=now_))
            s.add(MarketHist(tg_id=lot.seller_id, kind="sell", item=lot.item, price=lot.price, other="@" + buyer_nick, ts=now_))
            await s.commit()
            funnel.mark(uid, "market")
            funnel.mark(lot.seller_id, "market")
            gram_amt = lot.price / gram.NANO                              # задания сезона: покупки и продажи на маркете
            await seasonpts.add(uid, "mbuy", gram_amt)
            await seasonpts.add(lot.seller_id, "msell", gram_amt)
    if op == "buy":
        await push_to_player(lot.seller_id, {"t": "gram", "text": f"Маркет: лот продан, +{gram.g(payout)} GRAM", "refresh": True})
        return web.json_response({"ok": True, "item": item})
    raise web.HTTPBadRequest(text="bad op")


async def _take_lot(s, lot_id, seller=None):
    """Атомарно снять лот с маркета. True — именно этот запрос его забрал."""
    t = MarketLot.__table__
    q = t.delete().where(t.c.id == lot_id)
    if seller is not None:
        q = q.where(t.c.seller_id == seller)
    return (await s.execute(q.returning(t.c.id))).first() is not None


# ---------- TON Connect: манифест и иконка игры ----------
async def tonconnect_manifest(request):
    base = (WEBAPP_URL or f"{request.scheme}://{request.host}/").rstrip("/")
    return web.json_response({"url": base, "name": "MetalWar", "iconUrl": base + "/icon.png"}, headers={"Access-Control-Allow-Origin": "*"})


async def icon_png(request):
    return web.FileResponse(Path(__file__).parent / "icon.png", headers={"Access-Control-Allow-Origin": "*"})


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
hub = realtime.Hub(max_per_uid=WS_MAX_PER_UID)
clients = {}                 # ws -> данные игрока (совместимость со старым кодом; ведётся вместе с hub)
last_seen = {}               # tg_id -> время выхода из игры (для «заходил в …»)
STATE = {"ready": False, "stopping": False, "tasks": {}, "runner": None}


async def api_presence(request):
    """Кто из списка сейчас в игре и когда каждый заходил последний раз."""
    body, user = await read_auth(request)
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()][:200]
    out = {}
    if ids:
        async with SessionLocal() as s:
            rows = (await s.execute(select(GameSave.tg_id, GameSave.updated).where(GameSave.tg_id.in_(ids)))).all()
        db = {a: (b or 0) for a, b in rows}
        now_ = int(time.time())
        for i in ids:
            on = hub.is_online(i)
            out[str(i)] = {"online": on, "last": (now_ if on else max(db.get(i, 0), int(last_seen.get(i, 0)))) * 1000}
    return web.json_response({"ok": True, "p": out})
chat_history = deque(maxlen=60)
chat_seq = [0]               # монотонный номер сообщения чата

# ---------- пати (до 4 игроков, живёт в памяти сервера) ----------
PARTY_MAX = 4
parties = {}                 # id пати -> {"id", "leader", "members": [tg_id, ...]}
member_party = {}            # tg_id -> id пати
invites = {}                 # tg_id приглашённого -> {tg_id пригласившего: время}
last_heal = {}               # tg_id -> время последнего лечения
med_bucket = {}              # tg_id Ремонтника -> [запас лечения, время] (сколько он может вылечить в секунду)
med_shield = {}              # tg_id Ремонтника -> время последнего щита
MED_RANGE = 460              # с запасом к дальности умений на телефоне (380) и задержке позиций
MED_PVP = 0.6                # лечение по цели в PvP-бою слабее на 40%


def med_budget(uid, lvl):
    """Запас лечения Ремонтника: копится 20 + 6·уровень в секунду, не больше 8 секунд накопления."""
    rate = 20 + 6 * max(1, int(lvl or 1))
    now = time.time()
    b = med_bucket.get(uid)
    if not b:
        b = med_bucket[uid] = [rate * 8.0, now]
    b[0] = min(rate * 8.0, b[0] + (now - b[1]) * rate)
    b[1] = now
    return b


def online(uid):
    """Данные игрока в сети или None. Теперь через индекс, без перебора всех соединений."""
    return hub.info_of(uid)


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
    members = set(parties.get(pid, {}).get("members", []))
    text = realtime.encode({"t": "party", "party": party_payload(pid)})
    for uid in members | set(extra_uids):
        hub.to_uid(uid, text if uid in members else {"t": "party", "party": None})


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
        pvpguard.heal(other, amount, tgt.get("mhp", 1))
        await push_to_player(other, {"t": "healed", "from": info["nick"], "amount": amount})
    elif t in ("cheal", "cbuff"):
        # умения Ремонтника: лечение и щит. Себя — всегда, других — только пати/гильдию рядом в той же локации
        if info.get("cls") != "medic" or info.get("dead"):
            return
        tgt = info if other in (0, uid) else online(other)
        if not tgt or tgt.get("dead"):
            return
        if tgt is not info:
            pid = member_party.get(uid)
            friend = (pid and member_party.get(other) == pid) or (info.get("gt") and info.get("gt") == tgt.get("gt"))
            if not friend or tgt["loc"] != info["loc"]:
                return
            if ((tgt["x"] - info["x"]) ** 2 + (tgt["y"] - info["y"]) ** 2) ** 0.5 > MED_RANGE:
                return
        if t == "cheal":
            try:
                amount = int(d.get("amount", 0))
            except (TypeError, ValueError):
                return
            b = med_budget(uid, info.get("lvl"))
            mhp = tgt.get("mhp", 1)
            amount = min(amount, int(0.35 * mhp), int(b[0]))
            if amount < 1:
                return
            b[0] -= amount
            if pvpguard.in_combat(tgt["id"]):
                amount = max(1, int(amount * MED_PVP))
            pvpguard.heal(tgt["id"], amount, mhp)
            if tgt is not info:
                await push_to_player(tgt["id"], {"t": "healed", "from": info["nick"], "amount": amount, "q": 1})
        else:
            if time.time() - med_shield.get(uid, 0) < 12:
                return
            med_shield[uid] = time.time()
            try:
                v = max(0.0, min(float(d.get("v", 0)), 0.45))
                dur = max(0.0, min(float(d.get("dur", 0)), 9.0))
            except (TypeError, ValueError):
                return
            tgt["shield_v"], tgt["shield_until"] = v, time.time() + dur
            if tgt is not info:
                await push_to_player(tgt["id"], {"t": "cbuff", "k": "shield", "v": v, "dur": dur, "from": info["nick"]})
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


CARD_NUM = {"atk": 1e6, "def": 1e6, "mhp": 1e7, "rate": 50, "crit": 100, "cpow": 50, "regen": 1e5, "bm": 1e8}
CARD_SLOTS = ("head", "weapon", "module", "armor", "core", "legs")
CARD_ID = re.compile(r"[a-z0-9_]{1,24}")


def clean_card(c):
    """Карточка пилота от телефона: только известные поля, числа в разумных пределах."""
    out = {}
    if not isinstance(c, dict):
        return out
    for k, hi in CARD_NUM.items():
        v = c.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            out[k] = round(max(0.0, min(float(v), hi)), 2)
    eq = c.get("eq")
    if isinstance(eq, dict):
        out["eq"] = {}
        for sl in CARD_SLOTS:
            it = eq.get(sl)
            if isinstance(it, list) and len(it) == 3 and isinstance(it[0], str) and CARD_ID.fullmatch(it[0]):
                try:
                    out["eq"][sl] = [it[0], max(0, min(int(it[1]), 3)), max(0, min(int(it[2]), 15))]
                except (TypeError, ValueError):
                    pass
    for k in ("dr", "art"):
        v = c.get(k)
        if isinstance(v, str) and CARD_ID.fullmatch(v):
            out[k] = v
    return out


def clean_pos(d, info):
    """Берём из сообщения только допустимые поля, чтобы нельзя было прислать мусор другим игрокам."""
    try:
        loc = d.get("loc")
        new_loc = loc if loc in LOCS else info.get("loc")
        nx = round(max(0.0, min(8000.0, float(d.get("x", 0)))), 1)            # 0,1 px хватает, а снимок короче
        ny = round(max(0.0, min(8000.0, float(d.get("y", 0)))), 1)
        # скорость: прыжок дальше возможного не принимаем, телефону отправим поправку
        if pvpguard.check_move(info, nx, ny, new_loc, d.get("dead")):
            info["x"], info["y"] = nx, ny
        else:
            info["pos_fix"] = True
        info["loc"] = new_loc
        for k in ("ang", "aim"):
            info[k] = round(float(d.get(k, 0)), 2)
        info["moving"] = bool(d.get("moving"))
        info["dead"] = bool(d.get("dead"))
        nick = str(d.get("nick", ""))[:16].strip()
        info["nick"] = nick or info["name"][:16]
        info["fac"] = info.get("fac_srv") or ""                                    # фракцию задаёт сервер, а не сообщение
        cap = progress.cached_cap(info["id"]) or info.get("lvl_cap") or 1            # свежий предел: растёт по мере убийств
        if info.get("loadtest"):
            cap = 60
        info["lvl"] = max(1, min(999 if info.get("admin") else cap, int(d.get("lvl", 1))))
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
        info["df"] = int(d["df"]) if isinstance(d.get("df"), (int, float)) else None
        info["sth"] = 1 if d.get("sth") and info.get("cls") == "ghost" else 0          # Призрак в тени (видят и другие)
        info["dr"] = d.get("dr") if d.get("dr") in DRONE_IDS else ""                  # дрон-компаньон рядом с роботом
        info["gt"] = str(d.get("gt", ""))[:4]
        info["gn"] = str(d.get("gn", ""))[:20]
        info["gi"] = d.get("gi") if d.get("gi") in ("gear", "shield", "bolt", "crown", "claw", "star") else ""
        info["gc"] = d.get("gc") if isinstance(d.get("gc"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", d.get("gc")) else ""
        info["seen"] = time.time()
    except (TypeError, ValueError):
        pass
    # пределы прочности/CP/брони по уровню, в бою — серверный учёт CP и прочности
    pvpguard.sanitize(info, progress.bm_cap(info.get("lvl", 1)))
    pvpguard.on_report(info)


PUBLIC_KEYS = ("id", "nick", "fac", "lvl", "x", "y", "ang", "aim", "moving", "dead", "eq", "wpn", "cls", "gt", "gn", "gi", "gc",
               "hp", "mhp", "cp", "mcp", "bm", "admin", "sth", "dr")
# дроны-компаньоны (как в game.html → DRONES): другим игрокам показываем только известные виды
DRONE_IDS = {"d_spark", "d_bolt", "d_hawk", "d_titan", "d_nova", "d_aegis", "d_phantom", "d_sol"}


def public(info):
    return {k: info.get(k) for k in PUBLIC_KEYS} | {"kr": info.get("kr", 0), "fl": 1 if pvp.flagged(info) else 0}


async def push_to_player(tg_id, payload):
    """Поставить сообщение игроку в очередь. True — игрок в сети. Сеть не ждём."""
    return hub.to_uid(tg_id, payload)


# ---- ограничители частоты ----
class Bucket:
    """Токен-бакет: rate событий в секунду в среднем, burst — сколько можно подряд."""
    __slots__ = ("rate", "burst", "tokens", "t")

    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, time.monotonic()

    def take(self, n=1.0):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


chat_limits = {}             # (uid, канал) -> Bucket; живёт по uid, а не по вкладке: новая вкладка лимит не сбрасывает
chat_cids = {}               # uid -> deque последних клиентских id сообщений (защита от дублей при повторе)
CHAT_RATES = {"world": (0.5, 3), "dm": (1.0, 4)}


def chat_allowed(uid, ch):
    b = chat_limits.get((uid, ch))
    if not b:
        b = chat_limits[(uid, ch)] = Bucket(*CHAT_RATES[ch])
    return b.take()


# ---------- модерация чата: мут ----------
chat_mutes = {}              # tg_id -> {"until": unix, "by": ник модератора}; хранится в таблице meta
MUTE_CHOICES = {5, 15, 60, 180, 1440}


async def load_mutes():
    try:
        async with SessionLocal() as s_:
            row = (await s_.execute(select(Meta).where(Meta.key == "chat_mutes"))).scalar_one_or_none()
        data = json.loads(row.value) if row and row.value else {}
        now = time.time()
        chat_mutes.update({int(k): v for k, v in data.items() if v.get("until", 0) > now})
    except Exception:
        log.exception("не удалось загрузить муты чата")


async def save_mutes():
    now = time.time()
    for k in [k for k, v in chat_mutes.items() if v.get("until", 0) <= now]:
        chat_mutes.pop(k, None)
    value = json.dumps({str(k): v for k, v in chat_mutes.items()})
    async with SessionLocal() as s_:
        row = (await s_.execute(select(Meta).where(Meta.key == "chat_mutes"))).scalar_one_or_none()
        if row:
            row.value = value
        else:
            s_.add(Meta(key="chat_mutes", value=value))
        await s_.commit()


def muted_until(uid):
    m = chat_mutes.get(uid)
    return m["until"] if m and m["until"] > time.time() else 0


def chat_sys(text):
    """Системная строка в мировом чате (видна всем и попадает в историю)."""
    m = {"id": f"sys-{int(time.time() * 1000)}", "ch": "world", "sys": 1, "text": text, "ts": int(time.time() * 1000)}
    chat_history.append(m)
    hub.to_all({"t": "chat", "m": m})


async def handle_mute(d, info):
    """Мут выдают модераторы и админы. Модератор не может мутить админов и других модераторов."""
    if not info.get("mod"):
        return
    try:
        target, minutes = int(d.get("uid")), int(d.get("min", 0))
    except (TypeError, ValueError):
        return
    if target == info["id"] or (minutes and minutes not in MUTE_CHOICES):
        return
    tgt = online(target)
    t_admin = bool(tgt and tgt.get("admin"))
    t_mod = bool(tgt and tgt.get("mod"))
    if t_admin or (t_mod and not info.get("admin")):
        hub.to_uid(info["id"], {"t": "pinfo", "text": "Этого пилота замутить нельзя"})
        return
    nick = (tgt or {}).get("nick") or str(d.get("nick", ""))[:16] or "Пилот"
    if minutes:
        chat_mutes[target] = {"until": time.time() + minutes * 60, "by": info["nick"]}
        dur = f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч"
        chat_sys(f"🔇 {nick}: мут в чате на {dur} (модератор {info['nick']})")
    else:
        if not chat_mutes.pop(target, None):
            return
        chat_sys(f"🔈 {nick}: мут снят (модератор {info['nick']})")
    hub.to_uid(target, {"t": "muted", "until": int(muted_until(target) * 1000)})
    log.warning("Мут: %s (%s) -> uid=%s на %s мин", info["nick"], info["id"], target, minutes)
    try:
        await save_mutes()
    except Exception:
        log.exception("не удалось сохранить муты")


# ---------- жалобы на сообщения чата ----------
reports = deque(maxlen=100)          # последние жалобы (в памяти): видят модераторы и админы
report_t = {}                        # tg_id -> время последней жалобы (не чаще раза в 20 с)
report_seq = [0]
REPORT_REASONS = {"spam": "Спам", "insult": "Оскорбления", "ads": "Реклама", "other": "Другое"}


def report_view(r):
    return {k: r[k] for k in ("id", "ts", "uid", "nick", "text", "reason", "by", "n")}


async def handle_report(d, info):
    now = time.time()
    if now - report_t.get(info["id"], 0) < 20:
        hub.to_uid(info["id"], {"t": "pinfo", "text": "Жалобу можно отправлять не чаще раза в 20 секунд"})
        return
    try:
        target = int(d.get("uid"))
    except (TypeError, ValueError):
        return
    reason = d.get("reason") if d.get("reason") in REPORT_REASONS else "other"
    if target == info["id"]:
        return
    report_t[info["id"]] = now
    text = str(d.get("text", ""))[:200]
    # повторная жалоба на то же сообщение — увеличиваем счётчик, а не плодим записи
    for r in reports:
        if r["uid"] == target and r["text"] == text and not r.get("closed"):
            if info["nick"] not in r["by_all"]:
                r["by_all"].append(info["nick"])
                r["n"] = len(r["by_all"])
                r["by"] = ", ".join(r["by_all"][:3]) + (" и др." if r["n"] > 3 else "")
            break
    else:
        report_seq[0] += 1
        r = {"id": report_seq[0], "ts": int(now * 1000), "uid": target, "nick": str(d.get("nick", ""))[:16] or "Пилот",
             "text": text, "reason": REPORT_REASONS[reason], "by": info["nick"], "by_all": [info["nick"]], "n": 1}
        reports.append(r)
    hub.to_uid(info["id"], {"t": "pinfo", "text": "Жалоба отправлена модераторам. Спасибо!"})
    note = realtime.encode({"t": "report_new", "r": report_view(r), "open": sum(1 for x in reports if not x.get("closed"))})
    for c in list(hub.conns.values()):
        if c.info.get("mod"):
            c.push(note)
    metrics.inc("chat.report")


def handle_reports(d, info):
    """Модератор: список открытых жалоб или закрыть жалобу."""
    if not info.get("mod"):
        return
    if d.get("t") == "report_close":
        for r in reports:
            if r["id"] == d.get("id"):
                r["closed"] = True
    open_ = [report_view(r) for r in reversed(reports) if not r.get("closed")]
    hub.to_uid(info["id"], {"t": "reports", "list": open_[:50]})


async def handle_chat(d, info):
    text = str(d.get("text", "")).strip()[:200]
    ch = d.get("ch")
    if not text or ch not in ("world", "dm"):
        return
    uid = info["id"]
    until = muted_until(uid)
    if until:
        hub.to_uid(uid, {"t": "muted", "until": int(until * 1000)})
        metrics.inc("chat.muted_drop")
        return
    cid = str(d.get("cid", ""))[:40]
    if cid:
        seen = chat_cids.setdefault(uid, deque(maxlen=50))
        if cid in seen:                                # повтор после переподключения — уже доставлено
            hub.to_uid(uid, {"t": "chat_ack", "cid": cid, "dup": True})
            return
    if not chat_allowed(uid, ch):
        metrics.inc("chat.rate_limited")
        return
    if cid:
        chat_cids[uid].append(cid)
    now = time.time()
    chat_seq[0] += 1
    m = {"id": f"{uid}-{int(now * 1000)}", "seq": chat_seq[0], "ch": ch, "text": text, "nick": info["nick"], "fac": info["fac"],
         "lvl": info["lvl"], "uid": str(uid), "admin": info["admin"], "mod": bool(info.get("mod")) and not info["admin"], "ts": int(now * 1000)}
    await seasonpts.add(uid, "chat", 1)                        # задание сезона: сообщения в чат
    if ch == "world":
        chat_history.append(m)
        hub.to_all({"t": "chat", "m": m})
    else:
        to = str(d.get("to", ""))[:16]
        m["to"] = to
        payload = realtime.encode({"t": "chat", "m": m})
        try:
            to_uid = int(d.get("to_uid") or 0)                     # новый клиент адресует по uid — однозначно
        except (TypeError, ValueError):
            to_uid = 0
        if to_uid:
            hub.to_uid(to_uid, payload)
        else:                                                      # старый клиент — по нику, как раньше
            for c in list(hub.conns.values()):
                if c.info.get("nick") == to and c.uid != uid:
                    c.push(payload)
        hub.to_uid(uid, payload)
    if cid:
        hub.to_uid(uid, {"t": "chat_ack", "cid": cid, "id": m["id"], "seq": m["seq"]})
    metrics.inc(f"chat.{ch}")


async def handle_pvp(d, info):
    # удар по игроку: та же PvP-локация, рядом, не чаще 3 раз в секунду, урон не выше предела по уровню
    try:
        to, dmg = int(d.get("to")), int(d.get("dmg", 0))
    except (TypeError, ValueError):
        return
    tgt = online(to)
    if not tgt or to == info["id"] or info["loc"] in SAFE_LOCS or tgt["loc"] != info["loc"]:
        return
    if ((tgt["x"] - info["x"]) ** 2 + (tgt["y"] - info["y"]) ** 2) ** 0.5 > 460:
        return
    skill = bool(d.get("skill"))
    # у обычного удара и умения раздельные перезарядки: умение сразу после удара больше не теряется
    if not skill and time.time() - info.get("pvp_t", 0) < 0.3:
        metrics.inc("pvp.cooldown_drop")
        return
    if info.get("srv_dead_until", 0) > time.time() or tgt.get("srv_dead_until", 0) > time.time():
        return                                   # сервер уже засчитал смерть одного из них
    pid = member_party.get(info["id"])
    if (pid and member_party.get(to) == pid) or (info.get("gt") and info.get("gt") == tgt.get("gt")):
        return                                   # союзников не бьём
    if not pvp.can_fight(info, tgt):
        return                                   # защита новичков: до 10-го уровня PvP нет
    if skill and time.time() - info.get("pvp_sk", 0) < 0.8:
        metrics.inc("pvp.skill_cooldown_drop")
        return                                   # умения по игрокам — не чаще раза в 0,8 с
    if skill:
        info["pvp_sk"] = time.time()
    else:
        info["pvp_t"] = time.time()
    info["pvp_last"] = time.time()
    dmg = max(1, min(dmg, (40 + info["lvl"] * 8) * (4 if skill else 1)))
    if tgt.get("shield_until", 0) > time.time():
        dmg = max(1, int(dmg * (1 - tgt.get("shield_v", 0))))     # Щит-контур Ремонтника
    pvp.on_hit(info, tgt)
    pvp.record_hit(info["id"], to, dmg)
    dead = pvpguard.on_hit(tgt, dmg)                # сервер сам ведёт CP и прочность жертвы
    crit = bool(d.get("crit"))
    hit = {"t": "pvp_hit", "from": info["id"], "nick": info["nick"], "dmg": dmg, "crit": crit, "skill": skill}
    if d.get("hid"):
        hit["hid"] = str(d.get("hid"))[:24]
    # баланс 4 сезона: замедление (Страж/Жнец), оглушение (Жнец), снятие защиты (ЭМИ, Залп)
    if d.get("sl") and info.get("cls") in ("guard", "reaper") and not skill:
        hit["sl"] = 1
    if d.get("st") and info.get("cls") == "reaper" and not skill:
        hit["st"] = 1
    if d.get("br") and skill and info.get("cls") in ("techno", "sniper", "ghost"):
        hit["br"] = 1
    hub.to_uid(to, hit)
    broadcast_pvp_fx(info, tgt, dmg, crit, skill)
    metrics.inc("pvp.hit")
    if dead:
        await server_kill(tgt, info)


PVP_FX_RADIUS2 = 1100 ** 2


def broadcast_pvp_fx(att, vic, dmg, crit, skill):
    """Удар видят все рядом: снаряд, цифра урона, полоски прочности обновляются сразу."""
    fx = realtime.encode({"t": "pfx", "a": att["id"], "v": vic["id"], "d": dmg, "c": 1 if crit else 0, "s": 1 if skill else 0,
                          "k": att.get("cls", ""), "hp": vic.get("hp", 0), "cp": vic.get("cp", 0)})
    ax, ay, vx, vy = att.get("x", 0), att.get("y", 0), vic.get("x", 0), vic.get("y", 0)
    for c in list(hub.by_loc.get(att["loc"], ())):
        i = c.info
        x, y = i.get("x", 0), i.get("y", 0)
        if (x - ax) ** 2 + (y - ay) ** 2 <= PVP_FX_RADIUS2 or (x - vx) ** 2 + (y - vy) ** 2 <= PVP_FX_RADIUS2:
            c.push(fx)


async def server_kill(victim, killer):
    """По расчёту сервера прочность жертвы кончилась, а её телефон о смерти не сообщил."""
    if not pvp.claim_death(victim["id"], killer["id"]):
        return
    pvpguard.clear(victim["id"])
    victim["srv_dead_until"] = time.time() + 6
    victim["dead"] = True
    to_killer, to_victim = await pvp.on_death(victim, killer)
    hub.to_uid(killer["id"], to_killer)
    hub.to_uid(victim["id"], {"t": "pvp_force_dead", "by": killer["id"], "nick": killer["nick"]})
    hub.to_uid(victim["id"], to_victim)
    metrics.inc("pvp.death_by_server")
    log.warning("PvP: смерть засчитана сервером, uid=%s (телефон не сообщил)", victim["id"])


async def handle_pvp_dead(d, info):
    try:
        killer = online(int(d.get("by")))
    except (TypeError, ValueError):
        killer = None
    if not killer or killer["loc"] != info["loc"] or info["loc"] in SAFE_LOCS:
        return
    # засчитываем, только если сервер сам видел удары убийцы по этой цели, и только один раз
    if not pvp.claim_death(info["id"], killer["id"]):
        metrics.inc("pvp.death_rejected")
        return
    pvpguard.clear(info["id"])
    to_killer, to_victim = await pvp.on_death(info, killer)
    hub.to_uid(killer["id"], to_killer)
    hub.to_uid(info["id"], to_victim)
    metrics.inc("pvp.death")


async def handle_kills(d, info, conn):
    """Убийства мобов из сообщения pos. Ответ (выпавший лут) — сообщением kres с тем же номером пачки."""
    try:
        async with SessionLocal() as s:
            drops, cap, lf = await items.process_kills(s, info["id"], info, d.get("mk"))
    except Exception:
        log.exception("убийства uid=%s", info["id"])
        metrics.inc("kill.error")
        drops, cap, lf = [], info.get("lvl_cap"), 1.0
    conn.push(realtime.encode({"t": "kres", "n": d.get("kn"), "drops": drops, "lvlCap": cap}))


async def api_faction(request):
    """Одноразовый выбор фракции в игре (для тех, кто не выбрал её в боте через /start). Сменить потом нельзя."""
    body, user = await read_auth(request)
    fac = body.get("fac")
    if fac not in FACTIONS:
        return web.json_response({"ok": False, "error": "Нет такой фракции"})
    uid = user["id"]
    async with SessionLocal() as s:
        await s.execute(insert_ignore(Player.__table__, tg_id=uid, name=(user["name"] or "Пилот")[:64], faction=fac,
                                      current_zone="scrapfields", **STARTING_STATS))
        # строка могла существовать с пустой фракцией — заполняем, только если она всё ещё пустая
        await s.execute(update(Player).where(Player.tg_id == uid, Player.faction == "").values(faction=fac)
                        .execution_options(synchronize_session=False))
        cur = (await s.execute(select(Player.faction).where(Player.tg_id == uid))).scalar()
        await s.commit()
    if cur != fac:
        return web.json_response({"ok": False, "error": "Фракция уже выбрана", "fac": cur})
    for c in hub.by_uid.get(uid, []):
        c.info["fac_srv"] = c.info["fac"] = fac
    log.info("Фракция: игрок %s выбрал %s", uid, fac)
    return web.json_response({"ok": True, "fac": fac})


async def api_chipwar(request):
    body, user = await read_auth(request)
    op = request.match_info["op"]
    if op == "state":
        return web.json_response({"ok": True, **chipwar.status()})
    if not user["admin"]:
        raise web.HTTPForbidden(text="not admin")
    if op == "start":
        if chipwar.WAR.phase == "live":
            return web.json_response({"ok": False, "error": "Chip War уже идёт"})
        try:
            minutes = max(1, min(60, int(body.get("minutes", 10))))
        except (TypeError, ValueError):
            minutes = 10
        await chipwar.start_now(hub, minutes * 60)
        log.warning("Админ @%s запустил Chip War на %s мин", user["username"], minutes)
        return web.json_response({"ok": True})
    if op == "stop":
        if chipwar.WAR.phase != "live":
            return web.json_response({"ok": False, "error": "Chip War сейчас не идёт"})
        await chipwar.finish_now(hub, push_to_player)
        return web.json_response({"ok": True})
    raise web.HTTPBadRequest(text="bad op")


WS_TYPES = {"pos", "pinv", "pacc", "pdec", "pleave", "pkick", "heal", "cheal", "cbuff", "pxp", "card", "card_get", "pvp", "pvp_dead", "emote", "chat", "ping", "mute", "report", "reports", "report_close", "mhit", "mpos", "mctl", "wbhit", "wbpick", "twhit", "twdead"}


async def ws_handler(request):
    if STATE["stopping"]:
        raise web.HTTPServiceUnavailable(text="restarting")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=32 * 1024)
    await ws.prepare(request)
    metrics.inc("ws.opened")
    info = None
    conn = None
    in_limit = Bucket(WS_IN_RATE, WS_IN_BURST)
    dropped = 0
    loop = asyncio.get_running_loop()
    # не авторизовался за WS_AUTH_TIMEOUT секунд — закрываем
    auth_timer = loop.call_later(WS_AUTH_TIMEOUT, lambda: None if info else asyncio.ensure_future(ws.close(code=4001, message=b"auth timeout")))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            metrics.inc("ws.msgs_in")
            if not in_limit.take():
                dropped += 1
                metrics.inc("ws.in_rate_drop")
                if dropped > WS_IN_BURST * 5:                         # поток мусора — отключаем
                    metrics.inc("ws.flood_close")
                    await ws.close(code=4009, message=b"flood")
                    break
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("t")
            t0 = time.perf_counter()
            if info is None:
                if t != "auth":
                    continue
                user = auth(d.get("initData", ""))
                if not user:
                    metrics.inc("ws.auth_fail")
                    await ws.close(code=4001, message=b"bad auth")
                    break
                auth_timer.cancel()
                try:
                    proto = int(d.get("proto", 1))
                except (TypeError, ValueError):
                    proto = 1
                info = {**user, "loc": "lobby", "x": 500, "y": 640, "ang": 0, "aim": 0, "moving": False, "dead": False,
                        "nick": user["name"][:16], "fac": "", "fac_srv": "", "lvl": 1, "eq": {}, "wpn": "", "seen": time.time(), "kr": 0,
                        "proto": proto}
                if LOADTEST and user["id"] >= LOADTEST_UID_BASE:
                    info["loadtest"] = True
                try:
                    await pvp.load_karma(info)
                    async with SessionLocal() as s_:
                        info["lvl_cap"] = await progress.cap_of(s_, user["id"])
                        fac = (await s_.execute(select(Player.faction).where(Player.tg_id == user["id"]))).scalar()
                        await s_.commit()
                    info["fac_srv"] = info["fac"] = fac if fac in FACTIONS else ""
                except Exception:
                    # база медленная или недоступна — игрок всё равно входит, чат и мир работают
                    log.exception("не удалось загрузить карму/предел уровня uid=%s", user["id"])
                    metrics.inc("ws.auth_db_error")
                if ws.closed:
                    break
                conn = hub.add(ws, info)
                clients[ws] = info
                conn.push(realtime.encode({"t": "hello", "id": user["id"], "admin": user["admin"], "mod": user.get("mod", False), "mw": 2,
                                           "muted": int(muted_until(user["id"]) * 1000), "history": list(chat_history), "kr": info["kr"],
                                           "fac": info["fac_srv"], "cw": chipwar.status()}))
                funnel.mark(user["id"], "world")
                metrics.observe("ws.h.auth", (time.perf_counter() - t0) * 1000)
                continue
            if t == "pos":
                old_loc = info["loc"]
                clean_pos(d, info)
                if info["loc"] != old_loc:
                    hub.moved(conn, old_loc)
                    conn.push(realtime.encode(mobworld.state(info["loc"])))   # общие мобы: кто убит, кто ранен
                    if info["loc"] == worldboss.LOC:
                        conn.push(realtime.encode(worldboss.view()))
                        if worldboss.st["loot"]:
                            conn.push(realtime.encode({"t": "wbloot", "items": list(worldboss.st["loot"].values())}))
                if info.pop("pos_fix", False) and time.time() - info.get("fix_t", 0) > 1:
                    info["fix_t"] = time.time()                      # вернуть телефон на последнюю честную точку
                    conn.push(realtime.encode({"t": "pos_fix", "x": info["x"], "y": info["y"]}))
                # удары по мобам и убийства едут в том же сообщении: сервер гарантированно видит удары раньше убийства
                if d.get("mh"):
                    mobguard.on_hits(info["id"], info.get("lvl_cap") or info.get("lvl") or 1, d["mh"])
                if d.get("mk"):
                    await handle_kills(d, info, conn)
            elif t in ("pinv", "pacc", "pdec", "pleave", "pkick", "heal", "cheal", "cbuff", "pxp"):
                await handle_party(d, info)
            elif t == "pvp":
                await handle_pvp(d, info)
            elif t == "pvp_dead":
                await handle_pvp_dead(d, info)
            elif t == "card":
                # карточка пилота для окна «Инфо»: характеристики и снаряжение (только показ, на бой не влияет)
                if time.time() - info.get("card_t", 0) >= 4:
                    info["card_t"] = time.time()
                    info["card"] = clean_card(d.get("c"))
            elif t == "card_get":
                try:
                    tid = int(d.get("id", 0))
                except (TypeError, ValueError):
                    tid = 0
                ti = online(tid)
                if ti and time.time() - info.get("cardq_t", 0) >= 0.5:
                    info["cardq_t"] = time.time()
                    conn.push(realtime.encode({"t": "card", "id": tid, "nick": ti.get("nick", ""), "lvl": ti.get("lvl", 1), "cls": ti.get("cls", ""),
                                               "hp": ti.get("hp", 0), "mhp": ti.get("mhp", 1), "gt": ti.get("gt", ""), "gn": ti.get("gn", ""),
                                               "c": ti.get("card") or {}}))
            elif t == "emote":
                eid = str(d.get("id", ""))[:10]
                if re.fullmatch(r"[a-z]{2,10}", eid) and time.time() - info.get("emo_t", 0) > 2:
                    info["emo_t"] = time.time()
                    hub.to_loc(info["loc"], {"t": "emote", "from": info["id"], "id": eid}, skip_uid=info["id"])
            elif t == "mhit":
                mobworld.on_hits(info, d, hub)
            elif t == "wbhit":
                await worldboss.on_hit(info, d, hub, seasonpts)
            elif t == "twhit":
                await tower.on_hit(info, d, hub, push_to_player)
            elif t == "twdead":
                tower.on_dead(info)
            elif t == "wbpick":
                worldboss.on_pick(info, d, hub)
            elif t == "mpos":
                mobworld.on_pos(info, d)
            elif t == "mctl":
                mobworld.on_claim(info, d, hub)
            elif t == "mute":
                await handle_mute(d, info)
            elif t == "report":
                await handle_report(d, info)
            elif t in ("reports", "report_close"):
                handle_reports(d, info)
            elif t == "chat":
                await handle_chat(d, info)
            elif t == "ping":                                       # клиент может мерить задержку
                pong = {"t": "pong", "c": d.get("c"), "s": int(time.time() * 1000)}
                if info.get("admin") or info.get("mod"):           # панель отладки: состояние сервера видят только админы и модераторы
                    pong["srv"] = metrics.brief(len(clients))
                conn.push(realtime.encode(pong))
            if t in WS_TYPES:
                metrics.observe("ws.h." + t, (time.perf_counter() - t0) * 1000)
    except Exception:
        log.exception("ошибка обработчика WebSocket")
        metrics.inc("ws.handler_error")
    finally:
        auth_timer.cancel()
        hub.remove(ws)
        clients.pop(ws, None)
        metrics.inc("ws.closed")
        if info:
            last_seen[info["id"]] = time.time()
        if info and not online(info["id"]):
            try:
                await party_leave(info["id"])
            except Exception:
                log.exception("party_leave")
    return ws


# ---------- рассылка мира ----------
def _near(a, b, r2):
    return (a.get("x", 0) - b.get("x", 0)) ** 2 + (a.get("y", 0) - b.get("y", 0)) ** 2 <= r2


def world_tick(keepalive, full_tick=False):
    """Один шаг рассылки. Каждый игрок сериализуется ОДИН раз за шаг, а не для каждого получателя."""
    r2 = VIEW_RADIUS ** 2 if VIEW_RADIUS > 0 else 0
    for loc, members in list(hub.by_loc.items()):
        conns = [c for c in members if not c.closing]
        if not conns:
            continue
        frags = [(c, realtime.encode(public(c.info))) for c in conns]
        loc_js = json.dumps(loc)
        head = '{"t":"players","loc":' + loc_js + ',"list":['
        for c in conns:
            me, uid = c.info, c.uid
            if r2:
                vis = [(o.uid, f) for o, f in frags if o.uid != uid and _near(me, o.info, r2)]
            else:
                vis = [(o.uid, f) for o, f in frags if o.uid != uid]
            if not c.delta:
                c.push_snapshot(head + ",".join(f for _, f in vis) + "]}", keepalive)
                continue
            # протокол 2: только изменившиеся соседи и id ушедших; полный снимок — после смены локации и раз в 10 с
            view = dict(vis)
            base = c.sent_view
            full = base is None or full_tick
            if full:
                up, gone = list(view.values()), []
            else:
                up = [f for i, f in view.items() if base.get(i) != f]
                gone = [i for i in base if i not in view]
            if not full and not up and not gone:
                metrics.inc("world.delta_empty")
                continue
            text = ('{"t":"pd","loc":' + loc_js + ',"full":' + ("1" if full else "0") + ',"up":[' + ",".join(up) +
                    '],"gone":' + json.dumps(gone) + '}')
            c.push_snapshot(text, True, view)
            metrics.inc("world.delta_full" if full else "world.delta")
        metrics.inc("world.snapshots", len(conns))


async def world_loop():
    """WORLD_HZ раз в секунду рассылаем игрокам соседей по локации, раз в секунду — состав пати."""
    period = 1.0 / WORLD_HZ
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    tick = 0
    while True:
        next_t += period
        await asyncio.sleep(max(0.0, next_t - loop.time()))
        if loop.time() - next_t > 1.0:                      # сильно отстали — не догоняем пачкой
            next_t = loop.time()
        tick += 1
        t0 = time.perf_counter()
        try:
            if tick % WORLD_HZ == 0:
                for pid in list(parties):
                    await send_party(pid)
            mobworld.tick(hub)                              # общие мобы: прочность, смерть, возрождение
            worldboss.tick(hub)                             # мировой босс: расписание, удары по площади
            if tick % 5 == 0:
                await tower.tick(hub, seasonpts, push_to_player)   # Кровавая башня: запись, старт, итоги
            world_tick(keepalive=tick % WORLD_HZ == 0,      # раз в секунду шлём даже без изменений
                       full_tick=tick % (WORLD_HZ * 10) == 0)   # дельта-клиентам — полный снимок раз в 10 с
        except Exception:
            log.exception("world tick")
            metrics.inc("world.error")
        metrics.observe("world.tick", (time.perf_counter() - t0) * 1000)


# ---------- уборка временных данных ----------
async def cleanup_loop():
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            online_ids = set(hub.by_uid)
            for uid in list(invites):
                inv = {k: v for k, v in invites[uid].items() if now - v < 60}
                if inv:
                    invites[uid] = inv
                else:
                    invites.pop(uid, None)
            for uid in [u for u, t in last_heal.items() if now - t > 60]:
                last_heal.pop(uid, None)
            for uid in [u for u in med_bucket if u not in online_ids]:
                med_bucket.pop(uid, None)
            for uid in [u for u, t in med_shield.items() if now - t > 60]:
                med_shield.pop(uid, None)
            for uid in [u for u, t in last_seen.items() if now - t > 3 * 86400]:
                last_seen.pop(uid, None)                       # дальше «заходил в …» берётся из базы
            for key in [k for k in chat_limits if k[0] not in online_ids]:
                chat_limits.pop(key, None)
            for uid in [u for u in chat_cids if u not in online_ids]:
                chat_cids.pop(uid, None)
            for uid in [u for u in member_party if u not in online_ids]:
                await party_leave(uid)                         # пати без живых участников не висят вечно
            pvp.cleanup(online_ids)
            pvpguard.cleanup()
            for uid in [u for u, t in report_t.items() if now - t > 60]:
                report_t.pop(uid, None)
            mobguard.cleanup(online_ids)
            metrics.gauge("mem.dicts", {"invites": len(invites), "last_seen": len(last_seen), "chat_limits": len(chat_limits),
                                        "parties": len(parties), "pvp_pairs": len(pvp._pair_t), "pvp_hits": len(pvp._hits)})
        except Exception:
            log.exception("cleanup")


async def start_web(port: int):
    ensure_epoch()
    app = web.Application(client_max_size=512 * 1024)
    app.router.add_get("/", game_page)
    app.router.add_get("/guide.html", guide_page)
    app.router.add_get("/guide", guide_page)
    app.router.add_get("/health", health)
    app.router.add_get("/live", live)
    app.router.add_get("/ready", ready)
    app.router.add_get("/metrics", metrics_page)
    app.router.add_post("/api/state/load", api_load)
    app.router.add_post("/api/state/save", api_save)
    app.router.add_post("/api/grants/ack", api_ack)
    app.router.add_post("/api/admin/grant", api_admin_grant)
    app.router.add_post("/api/admin/name", api_admin_name)
    app.router.add_post("/api/db", api_db)
    app.router.add_post("/api/top", api_top)
    app.router.add_post("/api/name", api_name)
    app.router.add_post("/api/presence", api_presence)
    app.router.add_post("/api/market/{op}", api_market)
    app.router.add_get("/tonconnect-manifest.json", tonconnect_manifest)
    app.router.add_get("/icon.png", icon_png)
    gram.setup(app)
    items.setup(app)
    pvp.setup(app)
    stats.setup(app)
    special_quests.setup(app, read_auth, push_to_player, grant_dict)
    seasonpts.setup(app, read_auth, push_to_player)
    async def api_wboss(request):
        """Состояние мирового босса; админ может вызвать его вне расписания (?start=1) для проверки."""
        body, user = await read_auth(request)
        if body.get("start") and user["admin"] and not worldboss.st["active"]:
            worldboss.start(manual=True)
            hub.to_all({"t": "pinfo", "text": f"⚠ Мировой босс «{worldboss.NAME}» появился в Центральном ангаре!"})
        return web.json_response({"ok": True, **worldboss.view()})
    app.router.add_post("/api/wboss", api_wboss)
    tower.setup(app, read_auth, online)
    seasonpts.setup_rating(app, read_auth, push_to_player)
    app.router.add_post("/api/faction", api_faction)
    app.router.add_post("/api/chipwar/{op}", api_chipwar)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    STATE["runner"] = runner
    STATE["tasks"]["world"] = asyncio.create_task(world_loop())
    STATE["tasks"]["cleanup"] = asyncio.create_task(cleanup_loop())
    STATE["tasks"]["season"] = asyncio.create_task(seasonpts.flush_loop())
    STATE["tasks"]["lag"] = asyncio.create_task(metrics.loop_lag_monitor())
    await load_mutes()
    await gram.migrate_market_to_gram()
    await items.migrate_gear_v2()                          # сначала номера поколений, потом самые старые вещи
    await items.migrate_gear()
    await items.migrate_market_registry()
    STATE["tasks"]["deposit"] = asyncio.create_task(gram.deposit_watcher())
    STATE["tasks"]["chipwar"] = asyncio.create_task(chipwar.loop(hub, push_to_player, metrics))
    STATE["ready"] = True
    if LOADTEST:
        log.warning("РЕЖИМ НАГРУЗОЧНОГО ТЕСТА включён (staging, LOADTEST=1)")
    log.info("Игра доступна на порту %s (мир %s Гц, радиус видимости %s)", port, WORLD_HZ, VIEW_RADIUS or "вся локация")


async def stop_web(drain_s=2.0):
    """Плавная остановка: новых не пускаем, просим клиентов переподключиться, даём очередям уйти, закрываем."""
    if STATE["stopping"]:
        return
    STATE["stopping"] = True
    log.warning("Остановка сервера: %s игроков в сети", len(hub.conns))
    hub.to_all({"t": "reconnect", "text": "Сервер обновляется, переподключаемся…"})
    conns = list(hub.conns.values())
    try:
        await asyncio.wait_for(asyncio.gather(*(c.drain(drain_s) for c in conns), return_exceptions=True), drain_s + 1)
    except asyncio.TimeoutError:
        pass
    for c in conns:
        c.close(realtime.CLOSE_RESTART, "restart")
    await asyncio.sleep(0.3)
    for name, t in list(STATE["tasks"].items()):
        t.cancel()
    await asyncio.gather(*STATE["tasks"].values(), return_exceptions=True)
    if STATE["runner"]:
        await STATE["runner"].cleanup()
    try:
        await seasonpts.flush()                                      # очки сезона не теряются при перезапуске
    except Exception:
        log.exception("сезон: сохранение при остановке")
    db_dispose()
    log.warning("Сервер остановлен")
