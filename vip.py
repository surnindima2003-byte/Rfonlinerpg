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


# ---------- награды уровней VIP: забираются через сервер ----------
# Раньше награду забирал телефон сам, и сервер не знал, сколько зелий, лома и сфер законно пришло.
# Теперь сервер отмечает полученные награды (документ vip/<id> в таблице docs) и записывает приход в учёт ресурсов.
# Зеркало VIP[i].rew в игре — только то, что сервер считает (сверяет test_pots_ledger.py); вещи и книги не считаются.
REWARDS = [
    {"pot_hp": 2, "pot_atk": 2},
    {"pot_hp": 4, "pot_atk": 4},
    {"pot_hp": 6, "pot_atk": 6, "pot_xp": 3, "sph_cu": 2},
    {"pot_hp": 8, "pot_atk": 8, "pot_xp": 4, "sph_cu": 5, "cores": 20},
    {"pot_hp": 10, "pot_atk": 10, "pot_xp": 5, "sph_ti": 1, "cores": 40},
    {"pot_hp": 12, "pot_atk": 12, "pot_xp": 6, "sph_ti": 2},
    {"pot_hp": 14, "pot_atk": 14, "pot_xp": 7, "sph_ti": 3, "scrap": 30000},
    {"pot_hp": 16, "pot_atk": 16, "pot_xp": 8, "sph_ti": 5},
    {"pot_hp": 18, "pot_atk": 18, "pot_xp": 9},
    {"pot_hp": 20, "pot_atk": 20, "pot_xp": 10, "sph_ti": 8, "cores": 150},
    {"pot_hp": 22, "pot_atk": 22, "pot_xp": 11},
    {"pot_hp": 24, "pot_atk": 24, "pot_xp": 12, "sph_ti": 12},
    {"pot_hp": 26, "pot_atk": 26, "pot_xp": 13},
    {"pot_hp": 28, "pot_atk": 28, "pot_xp": 14, "sph_ti": 20, "scrap": 200000},
    {"pot_hp": 30, "pot_atk": 30, "pot_xp": 15, "sph_ti": 30},
]


def _claimed_from(data):
    """Номера полученных наград из документа или сохранения (мусор отбрасывается)."""
    out = set()
    for x in data if isinstance(data, list) else []:
        if isinstance(x, int) and not isinstance(x, bool) and 0 <= x < len(LEVELS):
            out.add(x)
    return out


async def claim(s, uid, i, admin=False):
    """Забрать награду VIP номер i (с нуля). Возвращает текст ошибки или None. Коммит — за вызывающим."""
    import json
    from models import Doc, GameSave
    lv = await level(s, uid, admin)
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(LEVELS):
        return "Нет такой награды"
    if i >= lv:
        return f"Награда откроется на VIP {i + 1}"
    path = f"vip/{uid}"
    row = (await s.execute(select(Doc).where(Doc.path == path))).scalar_one_or_none()
    if row:
        try:
            claimed = _claimed_from(json.loads(row.data or "[]"))
        except ValueError:
            claimed = set()
    else:
        # первый раз через сервер: награды, уже полученные на телефоне до этого, берём из последнего сохранения
        claimed = set()
        raw = (await s.execute(select(GameSave.data).where(GameSave.tg_id == uid))).scalar_one_or_none()
        try:
            S = json.loads(raw).get("S") if raw else None
            claimed = _claimed_from(S.get("vipClaimed")) if isinstance(S, dict) else set()
        except (ValueError, AttributeError):
            claimed = set()
        row = Doc(path=path, col="vip", data="[]")
        s.add(row)
    if i in claimed:
        return "Награда уже получена"
    claimed.add(i)
    row.data, row.updated = json.dumps(sorted(claimed)), int(time.time())
    rew = REWARDS[i]
    import ledger
    import saveguard
    ledger.add(uid, {k: v for k, v in rew.items() if k in ("scrap", "cores") + ledger.POTS})
    saveguard.note_grant(uid, {**{k: v for k, v in rew.items() if k in ("scrap", "cores") + ledger.POTS},
                               "sph": rew.get("sph_cu", 0) + rew.get("sph_ti", 0)})
    return None


def setup(app, read_auth):
    from aiohttp import web
    from db import SessionLocal

    async def api_claim(request):
        body, user = await read_auth(request)
        try:
            i = int(body.get("i"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Нет такой награды"})
        import saveguard
        async with saveguard.player_lock(user["id"]):           # заодно с сохранением — по очереди
            async with SessionLocal() as s:
                err = await claim(s, user["id"], i, bool(user.get("admin")))
                if err:
                    return web.json_response({"ok": False, "error": err})
                await s.commit()
        return web.json_response({"ok": True})

    app.router.add_post("/api/vip/claim", api_claim)
