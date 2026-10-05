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

from config import BOT_TOKEN, ADMIN_USERNAMES, MOD_USERNAMES, ADMIN_IDS, MOD_IDS, INITDATA_MAX_AGE, DATA_EPOCH, ENV_NAME
from config import (WORLD_HZ, VIEW_RADIUS, VIEW_HYST, NEAR_RADIUS, WS_MAX_PER_UID, WS_IN_RATE, WS_IN_BURST, WS_AUTH_TIMEOUT,
                    METRICS_TOKEN, LOADTEST, LOADTEST_UID_BASE)
from db import SessionLocal, engine, db_ping, dispose as db_dispose
import metrics
import realtime
from models import Base, GameSave, Grant, Doc, Meta, MarketLot, MarketHist, Player, FirstSeen
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
import vip
from config import WEBAPP_URL

GAME_FILE = Path(__file__).parent / "game.html"
GUIDE_FILE = Path(__file__).parent / "guide.html"
MAX_SAVE_BYTES = 300_000
LOCS = {"lobby", "sector1", "sector2", "scrapfields", "reactor_ruins", "iron_canyon", "arena_fear", "tower", "farm1", "season1"}
SEASON_LOC = "season1"                                   # сезонная зона: вход только с билетом сезона
SAFE_LOCS = {"lobby", "arena_fear", "tower"}          # здесь PvP нет никогда
FACTIONS = {"aegis", "vex", "core"}
CLASSES = {"", "guard", "reaper", "sniper", "techno", "ghost", "glyph", "medic"}
GRANT_KINDS = {"scrap", "cores", "exp", "level", "item"}
GEAR_IDS = items.GEAR_IDS | {"kit_s", "kit_l", "wire", "plate", "chip", "sph_cu", "sph_ti"}
# всё, что админ может выдать: снаряжение, книги (по шаблону ниже), расходники, зелья и учтённые сервером вещи —
# сферы, руны, дроны, артефакты, крылья, плащи (их сервер ещё и записывает на счёт игрока, чтобы их можно было продать)
ADMIN_ITEMS = GEAR_IDS | {"pot_hp", "pot_atk", "pot_xp"} | set(items.REG_IDS)
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
# username → tg_id, за которым закреплена роль (в meta, переживает вайп). Раньше админом был любой, у кого
# сейчас этот username: если админ сменит ник в Telegram, занявший старый получил бы админку и бэкапы кошельков.
ROLE_PINS_KEY = "role_pins"
_role_pins = {}
_role_warned = {}


def role_ok(username, uid, names, ids):
    """Роль по id (ADMIN_IDS/MOD_IDS) или по username, закреплённому за первым вошедшим с ним id."""
    if uid in ids:
        return True
    if not username or username not in names:
        return False
    pinned = _role_pins.get(username)
    if pinned is None:
        _role_pins[username] = uid
        log.warning("Роль по username @%s закреплена за id %s", username, uid)
        try:
            asyncio.get_running_loop().create_task(save_role_pins())
        except RuntimeError:
            pass
        return True
    if pinned == uid:
        return True
    if time.time() - _role_warned.get(uid, 0) > 3600:
        _role_warned[uid] = time.time()
        log.error("Вход с username @%s, но роль закреплена за другим id (%s ≠ %s) — прав не даём", username, uid, pinned)
    return False


async def load_role_pins():
    async with SessionLocal() as s:
        row = (await s.execute(select(Meta).where(Meta.key == ROLE_PINS_KEY))).scalar_one_or_none()
    try:
        pins = json.loads(row.value) if row and row.value else {}
        _role_pins.update({str(k): int(v) for k, v in pins.items()})
    except (ValueError, TypeError):
        log.error("meta.%s испорчен — закрепления ролей начнутся заново", ROLE_PINS_KEY)


async def save_role_pins():
    from db_atomic import insert_ignore
    try:
        async with SessionLocal() as s:
            val = json.dumps(_role_pins)
            await s.execute(insert_ignore(Meta.__table__, key=ROLE_PINS_KEY, value=val))
            await s.execute(update(Meta).where(Meta.key == ROLE_PINS_KEY).values(value=val).execution_options(synchronize_session=False))
            await s.commit()
    except Exception:
        log.exception("не удалось сохранить закрепления ролей")


def admin_ids():
    """Все id администраторов: из ADMIN_IDS и закреплённые username (для рассылки бэкапов и т.п.)."""
    return set(ADMIN_IDS) | {uid for name, uid in _role_pins.items() if name in ADMIN_USERNAMES}


def _parse_init(init_data):
    if not init_data or len(init_data) > 4096:
        return None
    try:
        data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
    except ValueError:
        return None
    return data if data.user else None


def auth(init_data: str):
    """Возвращает игрока, если initData подписан нашим ботом и свежий, иначе None."""
    data = _parse_init(init_data)
    if not data or time.time() - data.auth_date.timestamp() > INITDATA_MAX_AGE:
        return None
    u = data.user
    username = (u.username or "").lower()
    admin = role_ok(username, u.id, ADMIN_USERNAMES, ADMIN_IDS)
    mod = admin or role_ok(username, u.id, MOD_USERNAMES, MOD_IDS)
    return {"id": u.id, "username": username, "name": u.first_name or "Пилот", "start": str(data.start_param or "")[:64],
            "admin": admin, "mod": mod}


def auth_expired(init_data):
    """Подпись настоящая, но устарела: игроку нужно открыть игру заново (а не «ошибка входа»)."""
    data = _parse_init(init_data)
    return bool(data) and time.time() - data.auth_date.timestamp() > INITDATA_MAX_AGE


async def read_auth(request):
    try:
        body = await request.json(loads=realtime.loads)      # без NaN/Infinity, как и в WebSocket
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    user = auth(body.get("initData", ""))
    if not user:
        raise web.HTTPUnauthorized(text="session expired" if auth_expired(body.get("initData", "")) else "bad initData")
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
    data["near_radius"] = NEAR_RADIUS
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
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="bad save")
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw.encode()) > MAX_SAVE_BYTES:
        raise web.HTTPBadRequest(text="bad save")
    s_ = data.get("S") if isinstance(data.get("S"), dict) else {}
    fix = {}                                             # что сервер поправил в сохранении (телефон применит)
    # сохранение и серверный крафт этого игрока — строго по очереди (см. saveguard.player_lock)
    async with saveguard.player_lock(user["id"]):
        async with SessionLocal() as s:
            row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
            if not row:
                row = GameSave(tg_id=user["id"])
                s.add(row)
            old = None
            if row.data and row.epoch == DATA_EPOCH:
                try:
                    old = json.loads(row.data)
                except ValueError:
                    old = None
            # Счётчик крафтов: сервер увеличивает его при каждом крафте и пишет в сохранение.
            # Сохранение со счётчиком меньше серверного собрано на телефоне ДО ответа на крафт —
            # в нём ещё нет списанных ресурсов. Принять его значит отдать вещь бесплатно.
            old_craft = 0
            if isinstance(old, dict) and isinstance(old.get("S"), dict):
                try:
                    old_craft = int(old["S"].get("craftN", 0) or 0)
                except (TypeError, ValueError):
                    old_craft = 0
            if old_craft:
                if "craftN" in s_:
                    try:
                        new_craft = int(s_.get("craftN") or 0)
                    except (TypeError, ValueError):
                        new_craft = 0
                    if new_craft < old_craft:
                        metrics.inc("save.stale_craft")
                        return web.json_response({"ok": False, "error": "stale save"}, status=409)
                else:
                    s_["craftN"] = old_craft                 # старая версия страницы счётчик не знает — не теряем его
            if not user["admin"] and saveguard.MODE != "off":
                since = int(row.updated or 0) if old is not None else 0
                if old is None:
                    # первое сохранение (или первое после вайпа) раньше не проверялось вовсе: сравниваем с пустым,
                    # время — с первого входа в игру
                    first = (await s.execute(select(FirstSeen.ts).where(FirstSeen.tg_id == user["id"]))).scalar()
                    dt = time.time() - first if first else 60
                    base = {"S": {}}
                else:
                    dt = time.time() - since
                    base = old
                # проверка сохранения: структура и скачки лома/ядер/заточки относительно прошлого сохранения
                granted = {}
                for g in (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.created >= since))).scalars().all():
                    try:
                        pl = json.loads(g.payload or "{}")
                        key = "sph" if g.kind == "item" and pl.get("item") in saveguard.SPHERES else g.kind
                        granted[key] = granted.get(key, 0) + int(pl.get("amount", 0))
                    except (ValueError, TypeError, AttributeError):
                        pass
                data, notes = saveguard.check(user["id"], user["username"] or user["name"], base, data, dt, granted, fix_out=fix)
                if notes:
                    log.warning("Сохранение uid=%s (%s): %s", user["id"], saveguard.MODE, "; ".join(f"{a}: {b}" for a, b in notes)[:500])
                if data is None:
                    # причина — телефону, чтобы игрок понимал, что происходит (раньше отказ проходил молча)
                    names = {"сферы": "сферы", "заточка": "заточка", "chip": "микросхемы", "wire": "провода", "plate": "бронепластины"}
                    what = sorted({v for k, v in names.items() if any(b.startswith(k) for a, b in notes if a == "скачок")})
                    return web.json_response({"ok": False, "error": "save rejected", "what": what}, status=409)
                s_ = data.get("S") if isinstance(data.get("S"), dict) else {}
            raw = json.dumps(data, ensure_ascii=False)
            row.epoch = DATA_EPOCH
            row.username, row.name, row.data, row.updated = user["username"], user["name"], raw, int(time.time())
            try:
                # уровень и боевая мощь для рейтинга — не выше того, что подтвердил сервер (админам без ограничений)
                cap = 999 if user["admin"] else await progress.cap_of(s, user["id"])
                row.lvl = max(1, min(cap, int(s_.get("level", 1))))
                row.bm = max(0, min(10_000_000 if user["admin"] else progress.bm_cap(row.lvl), int(data.get("bm", 0))))
            except (TypeError, ValueError, OverflowError):
                pass
            new_nick = str(s_.get("name", "")).strip()[:16]
            # раньше ник из сохранения не проверялся вовсе: через него проходили «admin», пробелы и любые символы
            nick_changed = False
            if new_nick and new_nick != row.nick and valid_nick(new_nick) and not reserved_nick(new_nick):
                clash = (await s.execute(select(GameSave.tg_id).where(func.lower(GameSave.nick) == new_nick.lower(),
                                                                      GameSave.tg_id != user["id"]).limit(1))).first()
                if not clash:
                    row.nick, nick_changed = new_nick, True
            row.cls = s_.get("cls") if s_.get("cls") in CLASSES and s_.get("cls") else ""
            row.guild_id = str(s_.get("guildId", ""))[:64]
            await s.commit()
    if nick_changed:
        set_online_nick(user["id"], row.nick)
    return web.json_response({"ok": True, "fix": fix} if fix else {"ok": True})


async def api_ack(request):
    body, user = await read_auth(request)
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()][:100]
    async with SessionLocal() as s:
        rows = (await s.execute(select(Grant).where(Grant.tg_id == user["id"], Grant.id.in_(ids)))).scalars().all()
        for g in rows:
            g.applied = True
        await s.commit()
    return web.json_response({"ok": True})


def set_saved_nick(row, name, now_ms=None):
    """Новый позывной в строке сохранения: и в индексе (nick), и внутри сохранения (S.name).

    Метка времени сохранения тоже обновляется: иначе телефон игрока при входе оставит свою копию (она
    «новее»), пришлёт её со старым позывным — и сервер вернёт старый. Пустое сохранение не трогаем."""
    row.nick = name
    if not row.data:
        return
    try:
        data = json.loads(row.data)
    except ValueError:
        return
    if not isinstance(data, dict) or not isinstance(data.get("S"), dict):
        return
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    data["S"]["name"] = name
    data["S"]["_ts"] = now_ms
    data["ts"] = now_ms
    row.data = json.dumps(data, ensure_ascii=False)
    row.updated = now_ms // 1000


async def admin_set_name(user, body):
    """Админ меняет позывной игроку: проверка формата и занятости, запись в сохранение, обновление в игре."""
    name = str(body.get("name", "")).strip()
    if not valid_nick(name):
        return web.json_response({"ok": False, "error": "Позывной: 3–16 символов — буквы, цифры, пробел, _ или -"})
    if reserved_nick(name):
        return web.json_response({"ok": False, "error": "Этот позывной нельзя использовать"})
    target = str(body.get("target", "")).strip().lstrip("@").lower()
    async with SessionLocal() as s:
        if target in ("", "me"):
            row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        else:
            row = (await s.execute(select(GameSave).where(func.lower(GameSave.username) == target).order_by(GameSave.updated.desc()).limit(1))).scalars().first()
        if not row:
            return web.json_response({"ok": False, "error": ("Игрок @" + target if target not in ("", "me") else "Ты") + " ещё не заходил в игру"})
        taken = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == name.lower(), GameSave.tg_id != row.tg_id).limit(1))).scalars().first()
        if taken:
            return web.json_response({"ok": False, "error": "Этот позывной уже занят"})
        old = row.nick or ""
        set_saved_nick(row, name)                               # и при следующем входе в сохранении будет новый позывной
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
        if (item not in ADMIN_ITEMS and not is_book) or grade not in (0, 1, 2, 3):
            return web.json_response({"ok": False, "error": "Неизвестный предмет или грейд"})
        if item not in items.GEAR_IDS:
            grade = 0                                            # грейд бывает только у снаряжения
        payload.update(item=item, grade=grade)
        if item in items.REG_IDS:
            payload["reg"] = True                                # учтённая вещь: телефон добавит её в учёт, сервер — на счёт ниже

    target = str(body.get("target", "")).strip().lstrip("@").lower()
    async with SessionLocal() as s:
        if target in ("", "me"):
            tg_id, target_name = user["id"], user["username"] or user["name"]
        else:
            row = (await s.execute(select(GameSave).where(func.lower(GameSave.username) == target).order_by(GameSave.updated.desc()).limit(1))).scalars().first()
            if not row:
                return web.json_response({"ok": False, "error": "Игрок @" + target + " ещё не заходил в игру"})
            tg_id, target_name = row.tg_id, row.username
        g = Grant(tg_id=tg_id, kind=kind, payload=json.dumps(payload), by_admin=user["username"], created=int(time.time()))
        s.add(g)
        if kind == "item" and payload.get("reg"):
            await items.add_spheres(s, tg_id, payload["item"], amount)   # учтённые вещи от админа можно продать на маркете
        if kind == "level":
            await progress.add_levels(s, tg_id, amount)          # выданные уровни сервер тоже засчитывает
        elif kind == "exp":
            await progress.add_exp(s, tg_id, amount)
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


GUILD_ICONS = {"gear", "shield", "bolt", "crown", "claw", "star"}      # как G_ICONS в game.html
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def valid_emblem(e):
    """Эмблема гильдии: только известный значок и цвет вида #RRGGBB.
    Цвет вставляется в SVG у всех, кто смотрит список гильдий, поэтому произвольная строка здесь — XSS."""
    return (isinstance(e, dict) and set(e) <= {"icon", "color"} and e.get("icon") in GUILD_ICONS
            and isinstance(e.get("color"), str) and HEX_COLOR.fullmatch(e["color"]) is not None)


async def clean_guilds():
    """При запуске: приводим старые данные гильдий в порядок.
    * эмблемы, записанные до проверки, — к допустимым (иначе XSS остаётся в базе);
    * участники, заявки и журнал удалённых гильдий — удаляем (раньше они оставались навсегда);
    * число участников — пересчитываем (раньше его мог записать кто угодно)."""
    fixed = orphans = 0
    async with SessionLocal() as s:
        guilds = {}
        for row in (await s.execute(select(Doc).where(Doc.col == "guilds"))).scalars().all():
            try:
                data = json.loads(row.data)
            except ValueError:
                continue
            gid = row.path.split("/", 1)[1]
            guilds[gid] = row
            if isinstance(data, dict) and "emblem" in data and not valid_emblem(data["emblem"]):
                data["emblem"] = {"icon": "gear", "color": "#F2A93B"}
                row.data = json.dumps(data, ensure_ascii=False)
                fixed += 1
        for row in (await s.execute(select(Doc).where(Doc.col.like("guilds/%/%")))).scalars().all():
            parts = row.col.split("/")
            if len(parts) == 3 and parts[1] not in guilds:
                await s.execute(Doc.__table__.delete().where(Doc.path == row.path))
                orphans += 1
        for gid in guilds:
            await guild_recount(s, gid)
        await s.commit()
    if fixed or orphans:
        log.warning("Гильдии: исправлено эмблем %s, удалено записей удалённых гильдий %s", fixed, orphans)


def deny(msg="нет прав"):
    raise web.HTTPForbidden(text=msg)


# ---- правила гильдий: зеркало game.html (GUILD_BASE_MAX, gMax, upCost, VALID_NAME, VALID_TAG) ----
GUILD_BASE_MAX = 10
ROLES = {"member", "officer", "leader"}


def guild_max(g):
    try:
        return GUILD_BASE_MAX + (max(1, int(g.get("level") or 1)) - 1) * 5
    except (TypeError, ValueError):
        return GUILD_BASE_MAX


def guild_up_cost(level):
    return 500 * level


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int_in(v, lo, hi):
    return _num(v) and float(v).is_integer() and lo <= v <= hi


def valid_guild_name(v):
    return isinstance(v, str) and 3 <= len(v) <= 20 and v == v.strip() and all(ch.isalnum() or ch in " _-" for ch in v)


def valid_guild_tag(v):
    return isinstance(v, str) and 2 <= len(v) <= 4 and all(ch.isalnum() for ch in v)


def _clean_fields(data, rules):
    """Оставить только известные поля и проверить их. Неверное значение — 400 (честный клиент такого не шлёт)."""
    out = {}
    for k, v in data.items():
        check = rules.get(k)
        if check is None:
            continue                                    # лишние поля молча отбрасываем
        if not check(v):
            raise web.HTTPBadRequest(text=f"bad field {k}")
        out[k] = v
    return out


_short = lambda n: (lambda v: isinstance(v, str) and len(v) <= n)
_nonneg = lambda v: _num(v) and 0 <= v <= 1e12
GUILD_FIELDS = {"name": valid_guild_name, "tag": valid_guild_tag, "emblem": valid_emblem, "desc": _short(120),
                "open": lambda v: isinstance(v, bool), "minLvl": lambda v: _int_in(v, 1, 30), "level": lambda v: _int_in(v, 1, 10_000),
                # spent бывает меньше нуля: вклад ушедших остаётся в казне (казна = сумма вкладов − spent)
                "spent": lambda v: _num(v) and abs(v) <= 1e12, "count": _nonneg,
                "leader": _short(20), "leaderNick": _short(16), "created": _nonneg}
MEMBER_FIELDS = {"uid": _short(20), "nick": _short(16), "lvl": lambda v: _int_in(v, 1, 999), "fac": _short(8),
                 "role": lambda v: v in ROLES, "joined": _nonneg, "donated": _nonneg, "xp": _nonneg, "bm": _nonneg}
REQUEST_FIELDS = {"uid": _short(20), "nick": _short(16), "lvl": lambda v: _int_in(v, 1, 999), "fac": _short(8),
                  "ts": _nonneg, "bm": _nonneg}
LOG_FIELDS = {"text": _short(200), "ts": _nonneg}


async def guild_members(s, gid):
    rows = (await s.execute(select(Doc).where(Doc.col == f"guilds/{gid}/members"))).scalars().all()
    out = {}
    for r in rows:
        try:
            out[r.path.rsplit("/", 1)[1]] = json.loads(r.data)
        except ValueError:
            continue
    return out


async def other_guild(s, uid, gid):
    """Id другой существующей гильдии, где игрок уже состоит, или None. Состоять можно только в одной."""
    rows = (await s.execute(select(Doc.path).where(Doc.col.like("guilds/%/members"), Doc.path.like(f"guilds/%/members/{uid}")))).all()
    for (path,) in rows:
        parts = path.split("/")
        if len(parts) == 4 and parts[3] == str(uid) and parts[1] != gid:
            row, _ = await doc_get(s, f"guilds/{parts[1]}")
            if row:
                return parts[1]
    return None


async def player_lvl(s, uid):
    """Уровень для порога гильдии — из сохранения (сервер ограничивает его подтверждённым уровнем)."""
    try:
        return int((await s.execute(select(GameSave.lvl).where(GameSave.tg_id == int(uid)))).scalar() or 1)
    except (TypeError, ValueError):
        return 1


async def check_write(s, op, path, data, uid):
    """Права ролей и правила гильдии проверяет сервер: подделать их со страницы нельзя.
    Возвращает очищенные данные для записи (лишние поля отброшены, серверные — подставлены)."""
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
            data = _clean_fields(data, GUILD_FIELDS)
            if data.get("leader") != uid or not valid_guild_name(data.get("name")) or not valid_guild_tag(data.get("tag")):
                deny()
            if await other_guild(s, uid, gid):
                deny("ты уже в гильдии")
            name, tag = data["name"].lower(), data["tag"].upper()
            others = (await s.execute(select(Doc).where(Doc.col == "guilds"))).scalars().all()
            for o in others:
                od = json.loads(o.data)
                if str(od.get("name", "")).lower() == name or str(od.get("tag", "")).upper() == tag:
                    deny("название или тег заняты")
            data.update(level=1, spent=0, count=1)       # новая гильдия всегда с нуля, что бы ни прислал телефон
            return data
        if not guild:
            deny()
        if op == "delete":
            if not is_leader:
                deny()
            return data
        if op == "update":
            data = _clean_fields(data, GUILD_FIELDS)
            data.pop("count", None)                      # число участников считает сервер сам
            if "level" in data or "spent" in data:
                # казну меняет только улучшение гильдии лидером: уровень +1 и ровно его цена из казны.
                # Остальные правки spent (выход, исключение) сервер делает сам при удалении участника.
                if "level" not in data:
                    data.pop("spent", None)
                else:
                    lvl, spent = int(guild.get("level") or 1), guild.get("spent") or 0
                    cost = guild_up_cost(lvl)
                    bank = sum((m.get("donated") or 0) for m in (await guild_members(s, gid)).values() if _num(m.get("donated"))) - spent
                    if not is_leader or data["level"] != lvl + 1 or data.get("spent") != spent + cost or bank < cost:
                        deny("улучшение не по правилам")
            if "leader" in data:
                if not is_leader:
                    deny()
                _, new_l = await doc_get(s, f"guilds/{gid}/members/{data['leader']}")
                if not new_l:
                    deny("новый лидер не состоит в гильдии")
            if not data:
                return data                              # например, старый «пересчёт» count — ничего не меняет
            if not is_leader:
                deny()
            return data
        deny()

    if not guild:
        deny("гильдии нет")
    if sub == "members":
        if op == "set":
            data = _clean_fields(data, MEMBER_FIELDS)
            role = data.get("role")
            _, existing = await doc_get(s, path)
            # вклад и опыт участника сервер переносит сам: новый участник начинает с нуля,
            # а перезапись своей записи не может «нарисовать» вклад в казну
            data["donated"] = (existing or {}).get("donated", 0)
            data["xp"] = (existing or {}).get("xp", 0)
            data["uid"] = did
            if not existing:
                if await other_guild(s, did, gid):
                    deny("пилот уже в другой гильдии")
                if len(await guild_members(s, gid)) >= guild_max(guild):
                    deny("в гильдии нет мест")
            if did == uid:
                if role == "leader" and is_leader:
                    return data
                if role == "member" and (existing or guild.get("open") or is_leader):
                    if not existing and not is_leader and await player_lvl(s, uid) < int(guild.get("minLvl") or 1):
                        deny("уровень ниже порога гильдии")
                    if existing and existing.get("role") != "member" and not is_leader:
                        deny()                           # офицер не «понижает» себя перезаписью — для этого есть update
                    return data
                deny("гильдия по заявкам")
            if my_role in ("leader", "officer") and role == "member" and not existing:
                _, req = await doc_get(s, f"guilds/{gid}/requests/{did}")
                if req:
                    return data
            deny()
        if op == "update":
            data = _clean_fields(data, MEMBER_FIELDS)
            data.pop("uid", None)
            keys = set(data)
            _, target = await doc_get(s, path)
            if not target:
                raise web.HTTPNotFound(text="no doc")
            if did == uid and keys <= {"donated", "xp", "lvl", "nick", "bm", "role"}:
                if "role" in keys and not (is_leader or data.get("role") == my_role):
                    deny()
                if "donated" in data and data["donated"] < (target.get("donated") or 0):
                    deny("вклад не уменьшается")         # иначе казна росла бы за счёт «возврата» вклада
                return data
            if is_leader and keys <= {"role"}:
                return data
            deny()
        if op == "delete":
            if did == uid or is_leader:
                return data
            _, target = await doc_get(s, path)
            if my_role == "officer" and target and target.get("role") == "member":
                return data
            deny()
    if sub == "requests":
        if op == "set" and did == uid:
            if await player_lvl(s, uid) < int(guild.get("minLvl") or 1):
                deny("уровень ниже порога гильдии")
            data = _clean_fields(data, REQUEST_FIELDS)
            data["uid"] = did
            return data
        if op == "delete" and (did == uid or my_role in ("leader", "officer")):
            return data
        deny()
    if sub == "log":
        if op in ("set", "add") and me:
            return _clean_fields(data, LOG_FIELDS)
        if op == "delete" and is_leader:
            return data
        deny()
    deny()


async def guild_delete_all(s, gid):
    """Гильдия удалена — удаляем и её участников, заявки, журнал. Раньше они оставались, и при повторном
    создании гильдии с тем же id старые участники снова получали свои роли (вплоть до офицера)."""
    t = Doc.__table__
    for sub in ("members", "requests", "log"):
        await s.execute(t.delete().where(t.c.col == f"guilds/{gid}/{sub}"))
    await s.execute(t.delete().where(t.c.path == f"guilds/{gid}"))


async def guild_recount(s, gid):
    """Число участников гильдии считает сервер (раньше его мог записать кто угодно)."""
    row, g = await doc_get(s, f"guilds/{gid}")
    if not row:
        return
    n = len(await guild_members(s, gid))
    if g.get("count") != n:
        g["count"] = n
        row.data = json.dumps(g, ensure_ascii=False)


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
            q = body.get("q") if isinstance(body.get("q"), dict) else {}
            rows = (await s.execute(select(Doc).where(Doc.col == col))).scalars().all()
            docs = []
            for r in rows:
                try:
                    d_ = json.loads(r.data)
                except ValueError:
                    continue
                if isinstance(d_, dict):
                    docs.append(doc_view(r.path, d_))
            # кривой запрос (не тройки, не число, разные типы при сортировке) раньше давал ошибку 500
            where = q.get("where") if isinstance(q.get("where"), list) else []
            for w in where[:5]:
                if isinstance(w, list) and len(w) == 3 and isinstance(w[0], str) and w[1] == "==":
                    docs = [d for d in docs if d["data"].get(w[0]) == w[2]]
            order = q.get("order")
            if isinstance(order, list) and order and isinstance(order[0], str):
                f = order[0]
                desc = len(order) > 1 and order[1] == "desc"

                def key(d, f=f):
                    v = d["data"].get(f)
                    if v is None:
                        return (1, 0, 0, "")                   # без поля — в конце
                    if _num(v):
                        return (0, 0, -v if desc else v, "")
                    return (0, 1, 0, str(v))
                docs.sort(key=key)
                if desc:                                       # строки тоже по убыванию, пустые — всё равно в конце
                    nums = [d for d in docs if key(d)[:2] == (0, 0)]
                    strs = sorted((d for d in docs if key(d)[:2] == (0, 1)), key=lambda d: str(d["data"].get(f)), reverse=True)
                    docs = nums + strs + [d for d in docs if key(d)[0] == 1]
            try:
                limit = max(1, min(200, int(q.get("limit") or 200)))
            except (TypeError, ValueError, OverflowError):
                limit = 200
            return web.json_response({"ok": True, "docs": docs[:limit]})
        if op in ("set", "update", "delete", "add"):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            if op == "add":
                col = str(body.get("col", ""))
                if not parse_col(col) or col == "guilds":
                    raise web.HTTPBadRequest(text="bad col")
                path = f"{col}/a{int(time.time()*1000):x}{secrets.token_hex(3)}"
            else:
                path = str(body.get("path", ""))
            data = await check_write(s, "add" if op == "add" else op, path, data, uid)
            gid, sub, did = parse_path(path)
            if op == "delete":
                row, cur = await doc_get(s, path)
                if row:
                    if sub is None:
                        await guild_delete_all(s, gid)
                    else:
                        await s.execute(Doc.__table__.delete().where(Doc.path == path))
                    if sub == "members":
                        _, g = await doc_get(s, f"guilds/{gid}")
                        if g and g.get("leader") == did:
                            # лидер удалил свою запись (старый клиент так распускает гильдию): без лидера
                            # гильдия не остаётся — распускаем её целиком
                            await guild_delete_all(s, gid)
                        elif g:
                            # вклад ушедшего остаётся в казне: казна = сумма вкладов участников − spent
                            donated = (cur or {}).get("donated") or 0
                            if _num(donated) and donated:
                                g["spent"] = (g.get("spent") or 0) - donated
                                await doc_put(s, f"guilds/{gid}", g)
            elif op == "update":
                row, cur = await doc_get(s, path)
                if cur is None:
                    raise web.HTTPNotFound(text="no doc")
                if data:
                    await doc_put(s, path, {**cur, **data})
            else:
                if sub is None:
                    await guild_delete_all(s, gid)       # на случай старых записей с тем же id
                await doc_put(s, path, data)
            if sub == "members":
                await guild_recount(s, gid)
            await s.commit()
            if sub in (None, "members"):
                affected = [int(did)] if sub == "members" and did.isdigit() else \
                    [c.uid for c in list(hub.conns.values()) if c.info.get("gid") == gid]
                if sub == "members" or op == "delete":
                    affected += [c.uid for c in list(hub.conns.values()) if c.info.get("gid") == gid]
                await refresh_guild(affected)              # тег над головой и «союзник или нет» — сразу
            return web.json_response({"ok": True, "id": path.rsplit("/", 1)[1]})
    raise web.HTTPBadRequest(text="bad op")


# ---------- позывной: проверка, что имя свободно ----------
RESERVED_NICKS = {"пилот", "pilot", "admin", "админ", "administrator", "администратор", "moderator", "модератор", "system", "система"}
# кириллица, похожая на латиницу (и цифры, похожие на буквы): «Аdmin» с русской А — тот же «admin»
_LOOKALIKE = str.maketrans({"а": "a", "е": "e", "ё": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k",
                            "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
                            "0": "o", "1": "i", "l": "i", "3": "e", "4": "a", "5": "s", "_": "", "-": "", " ": ""})


def nick_skeleton(name):
    return str(name).lower().translate(_LOOKALIKE)


_RESERVED_SKELETONS = {nick_skeleton(n) for n in RESERVED_NICKS}


def reserved_nick(name):
    return nick_skeleton(name) in _RESERVED_SKELETONS


def valid_nick(name):
    return 3 <= len(name) <= 16 and name == name.strip() and "  " not in name and all(ch.isalnum() or ch in "_- " for ch in name)


async def api_name(request):
    body, user = await read_auth(request)
    name = str(body.get("name", "")).strip()
    if not valid_nick(name):
        return web.json_response({"ok": False, "error": "3–16 символов: буквы, цифры, пробел, _ или -"})
    if reserved_nick(name):
        return web.json_response({"ok": False, "error": "Этот позывной нельзя использовать"})
    async with saveguard.player_lock(user["id"]), SessionLocal() as s:     # по очереди с сохранением: строка создаётся один раз
        taken = (await s.execute(select(GameSave).where(func.lower(GameSave.nick) == name.lower(), GameSave.tg_id != user["id"]).limit(1))).scalars().first()
        if taken:
            return web.json_response({"ok": False, "error": "Этот позывной уже занят"})
        row = (await s.execute(select(GameSave).where(GameSave.tg_id == user["id"]))).scalar_one_or_none()
        if not row:
            row = GameSave(tg_id=user["id"], username=user["username"], name=user["name"], data="", updated=int(time.time()))
            s.add(row)
        row.nick = name
        await s.commit()
    set_online_nick(user["id"], name)
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
            await seasonpts.add_trade(uid, lot.seller_id, gram_amt)    # с лимитом на пару: без «стирки» между своими аккаунтами
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
            # гильдия — по записям участников на сервере (guild_id в сохранении присылает телефон, его можно подделать)
            gid_of = {}
            ids = {str(r.tg_id) for r in rows}
            for (path,) in (await s.execute(select(Doc.path).where(Doc.col.like("guilds/%/members")))).all():
                parts = path.split("/")
                if len(parts) == 4 and parts[3] in ids and parts[1] in guilds:
                    gid_of.setdefault(parts[3], parts[1])
            items = [{"id": str(r.tg_id), "nick": r.nick or r.name[:16], "lvl": r.lvl, "cls": r.cls, "bm": r.bm,
                      "tag": (guilds.get(gid_of.get(str(r.tg_id))) or {}).get("tag", "")} for r in rows]
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
# Командный опыт — только тем, кто в пределах экрана от убившего. Мир рисуется 1:1 в CSS-пикселях, робот стоит
# по центру: половина экрана телефона ≈ 195 × 420 px. Небольшой запас — на задержку позиций по сети.
PXP_HALF_W = 220
PXP_HALF_H = 440


# Разница уровней в пати: до 10 — опыт полностью, от 10 до 30 — плавно урезается, больше 30 — не даётся.
# Так высокий уровень не может «таскать» твинка. Уровни — серверные (info["lvl"] ограничен пределом сервера).
PXP_FULL_GAP = 10
PXP_MAX_GAP = 30


def pxp_gap_mult(lvl_a, lvl_b):
    """Доля командного опыта по разнице уровней убившего и получателя: 1.0 … 0.0."""
    gap = abs(int(lvl_a or 1) - int(lvl_b or 1))
    if gap <= PXP_FULL_GAP:
        return 1.0
    if gap > PXP_MAX_GAP:
        return 0.0
    return round(1 - (gap - PXP_FULL_GAP) / (PXP_MAX_GAP - PXP_FULL_GAP + 1), 3)


PXP_SHARE = 0.4             # союзникам рядом — 40% базового опыта за убийство


async def share_party_xp(info, base_exp):
    """Опыт пати за убийства, которые сервер засчитал (items.process_kills): союзникам на экране, живым,
    с поправкой на разницу уровней. Базовый опыт моба — без бонусов VIP и событий убившего."""
    pid = member_party.get(info["id"])
    if not pid or pid not in parties:
        return
    share = int(base_exp * PXP_SHARE)
    if share <= 0:
        return
    for m in list(parties[pid]["members"]):
        i = online(m)
        if m != info["id"] and i and not i.get("dead") and in_party_view(info, i):     # далеко или мёртв — опыта нет
            got = int(share * pxp_gap_mult(info.get("lvl"), i.get("lvl")))              # большая разница уровней — меньше или ноль
            if got > 0:
                await push_to_player(m, {"t": "pxp", "amount": got, "from": info["nick"]})


def in_party_view(a, b):
    """Союзник b виден на экране у a (одна локация, по прямоугольнику экрана, не по кругу)."""
    if a.get("loc") != b.get("loc"):
        return False
    try:
        return abs(b["x"] - a["x"]) <= PXP_HALF_W and abs(b["y"] - a["y"]) <= PXP_HALF_H
    except (KeyError, TypeError):
        return False


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


def set_online_nick(uid, nick):
    """Ник над головой и в чате задаёт сервер (из сохранения), а не каждое сообщение телефона."""
    for c in list(hub.by_uid.get(uid, [])):
        c.info["nick"] = nick or c.info["name"][:16]


async def guild_of(s, uid):
    """Гильдия игрока по документам гильдий на сервере: (id, тег, название, значок, цвет) или None."""
    rows = (await s.execute(select(Doc.path).where(Doc.col.like("guilds/%/members"), Doc.path.like(f"guilds/%/members/{uid}"))
                            .order_by(Doc.updated.desc()))).all()
    for (path,) in rows:
        parts = path.split("/")
        if len(parts) != 4 or parts[3] != str(uid):
            continue
        _, g = await doc_get(s, f"guilds/{parts[1]}")
        if not isinstance(g, dict):
            continue                                        # гильдию удалили, а запись участника осталась
        e = g.get("emblem") if valid_emblem(g.get("emblem")) else {}
        return parts[1], str(g.get("tag", ""))[:4], str(g.get("name", ""))[:20], e.get("icon", ""), e.get("color", "")
    return None


def apply_guild(uid, g):
    for c in list(hub.by_uid.get(uid, [])):
        i = c.info
        i["gid"], i["gt"], i["gn"], i["gi"], i["gc"] = g if g else ("", "", "", "", "")


async def refresh_guild(uids):
    """Перечитать гильдию игроков в сети (после вступления, выхода, исключения, правки гильдии)."""
    uids = [u for u in set(uids) if hub.is_online(u)]
    if not uids:
        return
    try:
        async with SessionLocal() as s:
            for uid in uids:
                apply_guild(uid, await guild_of(s, uid))
    except Exception:
        log.exception("гильдия: не удалось обновить %s", uids)


PVP_COMBAT_SEC = 10          # столько секунд после своего удара или удара по тебе нельзя сменить локацию


def pvp_combat_left(info):
    left = max(info.get("pvp_last", 0) + PVP_COMBAT_SEC - time.time(), pvpguard.combat_left(info["id"]))
    return max(0, int(left + 0.999))


def in_pvp_combat(info):
    """Игрок недавно бил другого игрока или его били (админов не держим)."""
    return not info.get("admin") and pvp_combat_left(info) > 0


def same_guild(a, b):
    """Союзники по гильдии — по id гильдии, который знает сервер (тег присылал телефон, его можно было подделать)."""
    return bool(a.get("gid")) and a.get("gid") == b.get("gid")


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
        if pid and pid not in parties:
            member_party.pop(uid, None)
            pid = None
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
        if member_party.get(uid) and member_party.get(uid) == member_party.get(other):
            return                                     # уже в одной пати (встречные приглашения)
        opid = member_party.get(other)
        if opid and opid in parties and parties[opid]["leader"] != other:
            # пригласивший с тех пор вступил в чужую пати и больше не лидер — так приглашать нельзя
            return await push_to_player(uid, {"t": "pinfo", "text": "Приглашение устарело"})
        if opid and opid in parties and len(parties[opid]["members"]) >= PARTY_MAX:
            return await push_to_player(uid, {"t": "pinfo", "text": "В пати уже 4 игрока"})
        if uid in member_party:
            # Выход из своей пати может её распустить — в том числе ту, куда нас зовут (встречные приглашения).
            # Раньше после этого шёл parties[pid] по уже удалённой пати: KeyError и обрыв соединения.
            await party_leave(uid)
        pid = member_party.get(other)
        if not pid or pid not in parties:
            pid = f"p{other}-{int(time.time())}"
            parties[pid] = {"id": pid, "leader": other, "members": [other]}
            member_party[other] = pid
        if len(parties[pid]["members"]) >= PARTY_MAX:
            return await push_to_player(uid, {"t": "pinfo", "text": "В пати уже 4 игрока"})
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
        if pid in parties and parties[pid]["leader"] == uid and other in parties[pid]["members"] and other != uid:
            await party_leave(other, kicked=True)
    elif t == "heal":
        # общий «Подхил» в пати отключён: лечить союзников может только Ремонтник (cheal)
        return
    elif t in ("cheal", "cbuff"):
        # умения Ремонтника: лечение и щит. Себя — всегда, других — только пати/гильдию рядом в той же локации
        if info.get("cls") != "medic" or info.get("dead"):
            return
        tgt = info if other in (0, uid) else online(other)
        if not tgt or tgt.get("dead"):
            return
        if tgt is not info:
            pid = member_party.get(uid)
            friend = (pid and member_party.get(other) == pid) or same_guild(info, tgt)
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
        # раньше сумму опыта присылал телефон (до 500, без ограничения частоты) — это был бесконечный опыт
        # для пати. Теперь опыт пати начисляет сервер сам за подтверждённые убийства (share_party_xp).
        metrics.inc("party.pxp_ignored")
        return


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
    for k in ("dr", "art", "wg", "ck"):
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
        # ник задаёт сервер (set_online_nick): раньше его брали из каждого pos — можно было писать в чат как «Админ»
        info["fac"] = info.get("fac_srv") or ""                                    # фракцию задаёт сервер, а не сообщение
        cap = progress.cached_cap(info["id"]) or info.get("lvl_cap") or 1            # свежий предел: растёт по мере убийств
        if info.get("loadtest"):
            cap = 60
        # не выше серверного предела и не ниже подтверждённого сервером уровня:
        # иначе телефон присылал lvl 1 и уходил от PvP как «новичок»
        floor = 1 if info.get("admin") or info.get("loadtest") else min(progress.cached_floor(info["id"]) or 1, cap)
        info["lvl"] = max(floor, min(999 if info.get("admin") else cap, int(d.get("lvl", 1))))
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
        info["wg"] = d.get("wg") if d.get("wg") in WING_IDS else ""                   # крылья за спиной
        info["ck"] = d.get("ck") if d.get("ck") in CLOAK_IDS else ""                  # плащ
        # гильдия над головой (gt/gn/gi/gc) — от сервера (apply_guild), а не из сообщения
        info["seen"] = time.time()
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    # пределы прочности/CP/брони по уровню, в бою — серверный учёт CP и прочности
    pvpguard.sanitize(info, progress.bm_cap(info.get("lvl", 1)))
    pvpguard.on_report(info)


PUBLIC_KEYS = ("id", "nick", "fac", "lvl", "x", "y", "ang", "aim", "moving", "dead", "eq", "wpn", "cls", "gt", "gn", "gi", "gc",
               "hp", "mhp", "cp", "mcp", "bm", "admin", "sth", "dr", "wg", "ck")
# дроны-компаньоны (как в game.html → DRONES): другим игрокам показываем только известные виды
DRONE_IDS = {"d_spark", "d_bolt", "d_hawk", "d_titan", "d_nova", "d_aegis", "d_phantom", "d_sol"}
# крылья (как в game.html → WINGS): другим игрокам показываем только известные виды
WING_IDS = {"wg_scrap", "wg_servo", "wg_ion", "wg_titan", "wg_seraph", "wg_void", "wg_phoenix", "wg_storm"}
# плащи (как в game.html → CLOAKS)
CLOAK_IDS = {"ck_canvas", "ck_mesh", "ck_scout", "ck_bastion", "ck_royal", "ck_night", "ck_ember", "ck_aurora"}


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
    if (pid and member_party.get(to) == pid) or same_guild(info, tgt):
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


KILLS_BACKLOG = 30          # пачек убийств в очереди одного игрока — больше уже не нормальная игра


def queue_kills(d, info, conn):
    """Убийства — в фоне, по очереди для каждого игрока.

    Раньше цикл сообщений игрока ждал, пока засчитаются до 40 убийств (несколько запросов к базе на каждое).
    Всё это время его позиции не обрабатывались: для остальных он замирал, а потом «прыгал».
    Удары (mh) по-прежнему учитываются сразу, до постановки в очередь, — сервер видит их раньше убийства.
    Порядок пачек сохраняется: asyncio.Lock отдаёт очередь ждущим по порядку."""
    lock = info.get("_klock")
    if lock is None:
        lock = info["_klock"] = asyncio.Lock()
    if info.get("_kq", 0) >= KILLS_BACKLOG:
        metrics.inc("kill.backlog_drop")
        conn.push(realtime.encode({"t": "kres", "n": d.get("kn"), "drops": [], "lvlCap": info.get("lvl_cap")}))
        return
    info["_kq"] = info.get("_kq", 0) + 1

    async def run():
        try:
            async with lock:
                await handle_kills(d, info, conn)
        except Exception:
            log.exception("убийства в фоне uid=%s", info.get("id"))
        finally:
            info["_kq"] -= 1
    asyncio.create_task(run())


async def handle_kills(d, info, conn):
    """Убийства мобов из сообщения pos. Ответ (выпавший лут) — сообщением kres с тем же номером пачки."""
    exp = []
    try:
        async with SessionLocal() as s:
            drops, cap, lf = await items.process_kills(s, info["id"], info, d.get("mk"), exp)
    except Exception:
        log.exception("убийства uid=%s", info["id"])
        metrics.inc("kill.error")
        drops, cap, lf, exp = [], info.get("lvl_cap"), 1.0, []
    conn.push(realtime.encode({"t": "kres", "n": d.get("kn"), "drops": drops, "lvlCap": cap}))
    if exp:
        await share_party_xp(info, sum(exp))


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
                d = realtime.loads(msg.data)                   # NaN/Infinity/гигантские числа — отбрасываем
            except (ValueError, RecursionError):
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
                    if auth_expired(d.get("initData", "")):
                        await ws.close(code=4003, message=b"session expired")      # телефон не будет переподключаться
                    else:
                        await ws.close(code=4001, message=b"bad auth")
                    break
                auth_timer.cancel()
                try:
                    proto = int(d.get("proto", 1))
                except (TypeError, ValueError):
                    proto = 1
                info = {**user, "loc": "lobby", "x": 500, "y": 640, "ang": 0, "aim": 0, "moving": False, "dead": False,
                        "nick": user["name"][:16], "fac": "", "fac_srv": "", "lvl": 1, "eq": {}, "wpn": "", "seen": time.time(), "kr": 0,
                        "proto": proto, "gid": "", "gt": "", "gn": "", "gi": "", "gc": ""}
                if LOADTEST and user["id"] >= LOADTEST_UID_BASE:
                    info["loadtest"] = True
                try:
                    await pvp.load_karma(info)
                    async with SessionLocal() as s_:
                        info["lvl_cap"] = await progress.cap_of(s_, user["id"])
                        fac = (await s_.execute(select(Player.faction).where(Player.tg_id == user["id"]))).scalar()
                        nick = (await s_.execute(select(GameSave.nick).where(GameSave.tg_id == user["id"]))).scalar()
                        g = await guild_of(s_, user["id"])
                        await s_.commit()
                    info["fac_srv"] = info["fac"] = fac if fac in FACTIONS else ""
                    if nick:
                        info["nick"] = nick[:16]
                    if g:
                        info["gid"], info["gt"], info["gn"], info["gi"], info["gc"] = g
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
            try:
                await handle_ws_message(d, t, info, conn)
            except Exception:
                # раньше любая ошибка в обработчике одного сообщения закрывала всё соединение игрока
                log.exception("ws: ошибка обработки t=%s uid=%s", t, info["id"])
                metrics.inc("ws.msg_error")
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
            progress.forget(info["id"])
            try:
                await party_leave(info["id"])
            except Exception:
                log.exception("party_leave")
    return ws


async def handle_ws_message(d, t, info, conn):
    """Одно сообщение живого мира от вошедшего игрока. Ошибка здесь не рвёт соединение (см. ws_handler)."""
    if t == "pos":
        old_loc = info["loc"]
        new_loc = d.get("loc")
        if new_loc in LOCS and new_loc != old_loc and in_pvp_combat(info) and not d.get("dead") and not info.get("dead"):
            # раньше в бою можно было просто прислать loc «lobby» и оказаться в безопасной зоне
            d = {**d, "loc": old_loc, "x": info["x"], "y": info["y"]}
            metrics.inc("pvp.flee_blocked")
            if time.time() - info.get("lf_t", 0) > 1:
                info["lf_t"] = time.time()
                conn.push(realtime.encode({"t": "loc_fix", "loc": old_loc, "x": info["x"], "y": info["y"],
                                           "text": f"В бою локацию не покинуть ещё {pvp_combat_left(info)} с"}))
        if d.get("loc") == SEASON_LOC and old_loc != SEASON_LOC and not info.get("admin"):
            if not await seasonpts.has_ticket(info["id"]):           # без билета в сезонную зону не пускаем
                d = {**d, "loc": old_loc}
                if time.time() - info.get("sz_warn", 0) > 10:
                    info["sz_warn"] = time.time()
                    conn.push(realtime.encode({"t": "pinfo", "text": "Сезонная зона: нужен билет сезона"}))
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
            queue_kills(d, info, conn)
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


# ---------- рассылка мира ----------
def _near(a, b, r2):
    return (a.get("x", 0) - b.get("x", 0)) ** 2 + (a.get("y", 0) - b.get("y", 0)) ** 2 <= r2


def _diff(old, new):
    if old is None or old.keys() != new.keys():
        return None
    ch = {k: v for k, v in new.items() if old.get(k) != v}
    ch["id"] = new["id"]
    return realtime.encode(ch)


def _pub_state(c, slow_tick):
    """Публичное состояние игрока на этом шаге (хранится в info, считается только при изменениях):
    _pv/_pf/_pd — версия, строка целиком, данные; _pp — изменения за этот шаг (от версии _pv-1);
    _kv/_kp — версия на прошлом «медленном» шаге и изменения от неё (для дальних соседей, раз в секунду)."""
    i = c.info
    d = public(i)
    if d != i.get("_pd"):
        i["_pp"] = _diff(i.get("_pd"), d)
        i["_pv"] = i.get("_pv", 0) + 1
        i["_pf"] = realtime.encode(d)
        i["_pd"] = d
    if slow_tick:
        if i.get("_kd_v") != i["_pv"]:
            i["_kp"] = _diff(i.get("_kd"), d)
            i["_kv"] = i.get("_kd_v")                    # от какой версии считаны изменения
            i["_kd"], i["_kd_v"] = d, i["_pv"]
    return i


def world_tick(keepalive, full_tick=False):
    """Один шаг рассылки. Каждый игрок сериализуется ОДИН раз за шаг (и только если изменился).

    Протокол 2 (все текущие клиенты) — два уровня интереса:
      * соседи ближе NEAR_RADIUS — каждый шаг; если телефон знает предыдущую версию, уходят только изменившиеся
        поля (обычно x, y, ang, aim — около 60 байт вместо ~350), телефон сливает их (Object.assign);
      * дальние — раз в секунду (для миникарты), тоже только изменения с прошлой секунды.
    Новые соседи, пропущенные версии и раз в 10 с — целиком. VIEW_RADIUS (если задан) — жёсткая отсечка.
    Замер на 100 бегущих игроках в одной локации: было ~394 КБ/с на игрока, стало ~10–15 КБ/с."""
    r2 = VIEW_RADIUS ** 2 if VIEW_RADIUS > 0 else 0
    r2_out = (VIEW_RADIUS * VIEW_HYST) ** 2 if VIEW_RADIUS > 0 else 0
    n2 = NEAR_RADIUS ** 2 if NEAR_RADIUS > 0 else 0
    for loc, members in list(hub.by_loc.items()):
        conns = [c for c in members if not c.closing]
        if not conns:
            continue
        states = [(o, _pub_state(o, keepalive)) for o in conns]
        loc_js = json.dumps(loc)
        head = '{"t":"players","loc":' + loc_js + ',"list":['
        for c in conns:
            me, uid = c.info, c.uid
            base = c.sent_view if c.delta else None
            if r2:
                # кто уже на экране, пропадает чуть дальше, чем появляется: без мигания на границе видимости
                vis = [(o, st) for o, st in states if o.uid != uid and
                       _near(me, o.info, r2_out if base is not None and o.uid in base else r2)]
            else:
                vis = [(o, st) for o, st in states if o.uid != uid]
            if not c.delta:
                c.push_snapshot(head + ",".join(st["_pf"] for _, st in vis) + "]}", keepalive)
                continue
            full = base is None or full_tick
            view, up = {}, []
            for o, st in vis:
                i, ver = o.uid, st["_pv"]
                if full:
                    view[i] = ver
                    up.append(st["_pf"])
                    continue
                have = base.get(i)
                if have == ver:
                    view[i] = ver
                    continue
                if have is not None and n2 and not keepalive and not _near(me, o.info, n2):
                    view[i] = have                       # дальний сосед: обновим на «медленном» шаге
                    continue
                view[i] = ver
                if have == ver - 1 and st.get("_pp") is not None:
                    up.append(st["_pp"])
                elif keepalive and have is not None and have == st.get("_kv") and st.get("_kp") is not None:
                    up.append(st["_kp"])
                else:
                    up.append(st["_pf"])
            gone = [] if full else [i for i in base if i not in view]
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
            mobworld.cleanup(online_ids)
            saveguard.cleanup(online_ids)
            # временные словари модулей: без этого они росли с каждым новым игроком до перезапуска
            items.cleanup(online_ids)
            progress.cleanup(online_ids)
            seasonpts.cleanup(online_ids)
            stats.cleanup()
            vip.cleanup()
            worldboss.cleanup()
            tower.cleanup()
            special_quests.cleanup()
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
    await load_role_pins()
    await clean_guilds()
    await gram.migrate_market_to_gram()
    await items.migrate_gear_v2()                          # сначала номера поколений, потом самые старые вещи
    await items.migrate_gear()
    await items.migrate_market_registry()
    STATE["tasks"]["deposit"] = asyncio.create_task(gram.deposit_watcher())
    STATE["tasks"]["chipwar"] = asyncio.create_task(chipwar.loop(hub, push_to_player, metrics))
    STATE["ready"] = True
    if LOADTEST:
        log.warning("РЕЖИМ НАГРУЗОЧНОГО ТЕСТА включён (staging, LOADTEST=1)")
    log.info("Игра доступна на порту %s (мир %s Гц, радиус видимости %s, ближняя зона %s)", port, WORLD_HZ,
             VIEW_RADIUS or "вся локация", NEAR_RADIUS or "вся локация")


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
