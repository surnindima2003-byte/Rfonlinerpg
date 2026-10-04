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
from sqlalchemy import select, func, update, case

from db import SessionLocal
from models import ItemInst, SphereBal, GameSave, LootDay, CraftDay
import metrics
import mobguard
from db_atomic import insert_ignore
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

log = logging.getLogger("items")

# снаряжение: (id, минимальный уровень) — зеркало ITEMS из игры, стартовые вещи не выпадают
# 9 видов × 5 поколений (1, 10, 20, 30, 40 ур.) — зеркало GEAR_FAM/TIERS из игры
GEAR_FAMS = ("hammer", "blades", "rifle", "staff", "sensor", "armor", "module", "core", "legs", "phaseblades", "glaive", "emitter")
TIER_LVL = {1: 1, 2: 5, 3: 10, 4: 15, 5: 20, 6: 25, 7: 30, 8: 35, 9: 40, 10: 45}
CLASS_WPN = {"guard": "hammer", "reaper": "blades", "sniper": "rifle", "techno": "staff", "ghost": "phaseblades", "glyph": "glaive", "medic": "emitter"}
GEAR = [(f"g_{f}_{t}", lv) for f in GEAR_FAMS for t, lv in TIER_LVL.items()]
GEAR_IDS = {g for g, _ in GEAR}
# старые вещи → новые; W1/W2 — оружие класса владельца 1-го/2-го поколения
LEGACY_GEAR = {"st_head": "g_sensor_1", "sensor1": "g_sensor_1", "sensor2": "g_sensor_3", "st_armor": "g_armor_1", "plate1": "g_armor_1",
               "plate2": "g_armor_3", "st_module": "g_module_1", "servo": "g_module_1", "shieldgen": "g_module_3", "st_core": "g_core_1",
               "reactor1": "g_core_1", "reactor2": "g_core_3", "st_legs": "g_legs_1", "tracks1": "g_legs_1", "tracks2": "g_legs_3",
               "st_weapon": "W1", "laser1": "W1", "laser2": "W1", "plasma": "W3"}


def weapon_id(cls, tier):
    return f"g_{CLASS_WPN.get(cls, 'hammer')}_{tier}"


def legacy_to_new(item_id, cls):
    m = LEGACY_GEAR.get(item_id)
    if not m:
        return item_id
    return weapon_id(cls, int(m[1])) if m[0] == "W" else m


async def migrate_gear_v2():
    """Один раз: было 5 поколений (1, 10, 20, 30, 40 ур.), стало 10 (каждые 5 ур.) — поколение t → 2t−1.
    Отметка в таблице meta не даёт перенести вещи повторно."""
    import json
    from models import MarketLot, MarketHist, Meta, Grant
    rx = re.compile(r"^g_([a-z]+)_([1-5])$")
    remap = lambda i: (lambda m: f"g_{m.group(1)}_{2 * int(m.group(2)) - 1}" if m else i)(rx.match(i or ""))
    moved = 0
    async with SessionLocal() as s:
        if (await s.execute(select(Meta).where(Meta.key == "gear_v2"))).scalar_one_or_none():
            return
        for r in (await s.execute(select(ItemInst))).scalars().all():
            new = remap(r.item)
            if new != r.item:
                r.item, moved = new, moved + 1
        for model in (MarketLot, MarketHist):
            for r in (await s.execute(select(model))).scalars().all():
                try:
                    it = json.loads(r.item)
                except (TypeError, ValueError):
                    continue
                new = remap(it.get("id"))
                if new != it.get("id"):
                    it["id"] = new
                    r.item, moved = json.dumps(it), moved + 1
        for gr in (await s.execute(select(Grant).where(Grant.kind == "item"))).scalars().all():
            try:
                pl = json.loads(gr.payload)
            except (TypeError, ValueError):
                continue
            new = remap(pl.get("item"))
            if new != pl.get("item"):
                pl["item"] = new
                gr.payload, moved = json.dumps(pl), moved + 1
        s.add(Meta(key="gear_v2", value=str(int(time.time()))))
        await s.commit()
    log.info("Снаряжение: 10 поколений, перенесено %s записей", moved)


async def migrate_gear():
    """Один раз: вещи старой системы в реестре и на маркете превращаются в новые."""
    import json
    from models import MarketLot, MarketHist
    moved = 0
    async with SessionLocal() as s:
        old = list(LEGACY_GEAR)
        rows = (await s.execute(select(ItemInst).where(ItemInst.item.in_(old)))).scalars().all()
        classes = {r.tg_id: r.cls for r in (await s.execute(select(GameSave))).scalars().all()} if rows else {}
        for r in rows:
            r.item = legacy_to_new(r.item, classes.get(r.owner, ""))
            moved += 1
        for model, owner_field in ((MarketLot, "seller_id"), (MarketHist, "tg_id")):
            for r in (await s.execute(select(model))).scalars().all():
                try:
                    it = json.loads(r.item)
                except (TypeError, ValueError):
                    continue
                if it.get("id") in LEGACY_GEAR:
                    if not classes:
                        classes = {x.tg_id: x.cls for x in (await s.execute(select(GameSave))).scalars().all()}
                    it["id"] = legacy_to_new(it["id"], classes.get(getattr(r, owner_field), ""))
                    r.item = json.dumps(it)
                    moved += 1
        await s.commit()
    if moved:
        log.info("Снаряжение: переведено на новую систему %s записей", moved)
SPHERES = ("sph_cu", "sph_ti")
# руны (как в game.html → RUNES). Учтённые руны хранятся в той же таблице балансов, что и сферы,
# и, как сферы, продаются на маркете за GRAM. Учёт идёт только через паки магазина и маркет.
RUNES = ("r_atk", "r_def", "r_hp", "r_crit", "r_spd", "r_aspd", "r_cpow", "r_regen", "r_war", "r_bastion", "r_storm", "r_fortune")
# дроны-компаньоны (как в game.html → DRONES): тоже предметы, учтённые сервером
DRONES = ("d_spark", "d_bolt", "d_hawk", "d_titan", "d_nova", "d_aegis", "d_phantom", "d_sol")
# артефакты (как в game.html → ARTS): надевается один, продаются на маркете
ARTIFACTS = ("a_reactor", "a_lens", "a_servo", "a_plate", "a_crown", "a_eye", "a_heart", "a_relic")
# крылья (как в game.html → WINGS): надеваются одни, видны другим игрокам, продаются на маркете
WINGS = ("wg_scrap", "wg_servo", "wg_ion", "wg_titan", "wg_seraph", "wg_void", "wg_phoenix", "wg_storm")
# плащи (как в game.html → CLOAKS): надевается один, виден другим игрокам, продаются на маркете
CLOAKS = ("ck_canvas", "ck_mesh", "ck_scout", "ck_bastion", "ck_royal", "ck_night", "ck_ember", "ck_aurora")
REG_IDS = SPHERES + RUNES + DRONES + ARTIFACTS + WINGS + CLOAKS      # всё, что учитывается штуками (без номера)
SOCKET_PREFIX = "s:"            # учёт рун, вставленных в вещи (строка «s:r_fortune» влезает в 12 символов столбца)

# Крафт рун и дронов идёт на сервере: id -> (минимальный уровень, что нужно).
# Зеркало game.html → RUNES/DRONES (src "craft"); совпадение проверяет test_craft_contract.py.
CRAFT = {
    "r_atk":   (10, {"scrap": 8000, "cores": 30, "chip": 4}),
    "r_def":   (10, {"scrap": 8000, "cores": 30, "plate": 6}),
    "r_hp":    (10, {"scrap": 8000, "cores": 30, "plate": 4, "wire": 4}),
    "r_crit":  (15, {"scrap": 15000, "cores": 60, "chip": 8}),
    "r_spd":   (15, {"scrap": 15000, "cores": 60, "wire": 10}),
    "r_aspd":  (20, {"scrap": 25000, "cores": 100, "chip": 10, "wire": 6}),
    "r_cpow":  (20, {"scrap": 25000, "cores": 100, "chip": 10, "plate": 6}),
    "r_regen": (25, {"scrap": 40000, "cores": 150, "plate": 12, "wire": 12}),
    "d_spark": (5,  {"scrap": 5000, "cores": 20, "wire": 10}),
    "d_bolt":  (10, {"scrap": 15000, "cores": 50, "wire": 10, "plate": 10}),
    "d_hawk":  (20, {"scrap": 40000, "cores": 150, "wire": 15, "chip": 15}),
    "d_titan": (30, {"scrap": 120000, "cores": 400, "plate": 30, "chip": 20}),
    "a_reactor": (25, {"scrap": 60000, "cores": 200, "plate": 20, "wire": 20}),
    "a_lens":    (30, {"scrap": 90000, "cores": 300, "chip": 30}),
    "a_servo":   (35, {"scrap": 140000, "cores": 450, "wire": 40, "chip": 20}),
    "a_plate":   (40, {"scrap": 200000, "cores": 600, "plate": 60}),
    "wg_scrap":  (15, {"scrap": 20000, "cores": 80, "plate": 15}),
    "wg_servo":  (25, {"scrap": 60000, "cores": 220, "plate": 25, "wire": 20}),
    "wg_ion":    (35, {"scrap": 150000, "cores": 500, "wire": 40, "chip": 25}),
    "wg_titan":  (45, {"scrap": 260000, "cores": 800, "plate": 70, "chip": 30}),
    "ck_canvas": (12, {"scrap": 15000, "cores": 60, "wire": 12}),
    "ck_mesh":   (22, {"scrap": 45000, "cores": 170, "wire": 20, "plate": 15}),
    "ck_scout":  (32, {"scrap": 120000, "cores": 420, "wire": 35, "chip": 20}),
    "ck_bastion": (42, {"scrap": 230000, "cores": 720, "plate": 65, "chip": 25}),
}
INV_MAX_SRV = 60
# Сколько вещей можно собрать на сервере за сутки (UTC). Собранное продаётся за GRAM, а ресурсы для крафта
# берутся из сохранения с телефона, которое сервер проверяет лишь по скорости прироста. Лимит — потолок
# на случай, если подделку не поймали. 0 — без лимита. Админам лимита нет.
CRAFT_DAILY_MAX = env_int("CRAFT_DAILY_MAX", 20)


async def take_craft_slot(s, uid):
    """Атомарно занять одну попытку крафта на сегодня. False — лимит исчерпан (ничего не изменено)."""
    if CRAFT_DAILY_MAX <= 0:
        return True
    day = int(time.time() // 86400)
    await s.execute(insert_ignore(CraftDay.__table__, tg_id=uid, day=day, n=0))
    res = await s.execute(update(CraftDay).where(CraftDay.tg_id == uid, (CraftDay.day != day) | (CraftDay.n < CRAFT_DAILY_MAX))
                          .values(n=case((CraftDay.day == day, CraftDay.n + 1), else_=1), day=day)
                          .returning(CraftDay.n).execution_options(synchronize_session=False))
    return res.first() is not None


def _inv_count(S, item_id):
    return sum(int(x.get("n", 1) or 0) for x in (S.get("inv") or []) if isinstance(x, dict) and x.get("id") == item_id)


def _inv_take(S, item_id, n):
    """Убрать n штук из сумки сохранения (с конца, как removeItem в игре)."""
    inv = S.get("inv") or []
    for i in range(len(inv) - 1, -1, -1):
        x = inv[i]
        if n <= 0:
            break
        if not isinstance(x, dict) or x.get("id") != item_id:
            continue
        take = min(n, int(x.get("n", 1) or 0))
        x["n"] = int(x.get("n", 1) or 0) - take
        n -= take
        if x["n"] <= 0:
            inv.pop(i)


def craft_in_save(S, item_id, level):
    """Проверить и провести крафт в данных сохранения. Возвращает текст ошибки или None (S изменён)."""
    lvl, need = CRAFT[item_id]
    if level < lvl:
        return f"Нужен {lvl} уровень"
    for k, n in need.items():
        have = S.get(k, 0) if k in ("scrap", "cores") else _inv_count(S, k)
        if not isinstance(have, (int, float)) or have < n:
            return "Сервер не видит нужных ресурсов. Подожди пару секунд и попробуй ещё раз"
    inv = S.setdefault("inv", [])
    stack = next((x for x in inv if isinstance(x, dict) and x.get("id") == item_id), None)
    if item_id in DRONES and (stack or any(isinstance(x, dict) and x.get("id") == item_id for x in S.get("store") or [])):
        return "Этот дрон уже есть"
    if not stack and len(inv) >= INV_MAX_SRV:
        return "Освободи место в сумке"
    for k, n in need.items():
        if k in ("scrap", "cores"):
            S[k] = S.get(k, 0) - n
        else:
            _inv_take(S, k, n)
    if stack:
        stack["n"] = int(stack.get("n", 1) or 0) + 1
    else:
        inv.append({"id": item_id, "n": 1})
    return None
from mobguard import BASE_MOBS, LOC_MIN, DUNGEON_RANGE, mob_level      # таблицы мобов — в mobguard (без базы)
SEASON_DROP = {"season1": 1.5}                  # сезонная зона: +50% к ценному дропу
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


BOSS_LOOT, BOSS_GAP = 150, 40                   # главарь: шансы ×150, не чаще одного на игрока раз в 40 с
_boss_t = {}


def roll(lv, mult=1):
    """Бросок ценного лута за одного убитого моба (mult > 1 — главарь)."""
    out, s = [], lv - 1
    pool = [g for g, need in GEAR if lv - 10 <= need <= lv + 2]
    for grade, per in enumerate(gear_per(lv)):
        if per and random.random() < min(0.5, per * mult) * len(pool):
            out.append({"kind": "gear", "id": random.choice(pool), "g": grade})
    if random.random() < min(0.9, P(0.0033 + 0.000036 * s) * mult):
        out.append({"kind": "sph", "id": "sph_cu", "n": 1})
    if random.random() < min(0.9, P(0.00033 + 0.0000036 * s) * mult):
        out.append({"kind": "sph", "id": "sph_ti", "n": 1})
    return out


def new_uid():
    return "i" + secrets.token_hex(8)


async def mint_gear(s, owner, item_id, g, e=0, source="drop"):
    uid = new_uid()
    s.add(ItemInst(uid=uid, owner=owner, item=item_id, g=g, e=e, status="inv", source=source, created=int(time.time())))
    return {"id": item_id, "g": g, "e": e, "uid": uid}


async def sph_row(s, owner, sid):
    """Строка баланса сфер. Если из-за старой гонки строк стало несколько — сливаем их в одну."""
    rows = (await s.execute(select(SphereBal).where(SphereBal.owner == owner, SphereBal.item == sid).order_by(SphereBal.id))).scalars().all()
    if not rows:
        row = SphereBal(owner=owner, item=sid, n=0)
        s.add(row)
        await s.flush()                                  # нужен id для атомарных изменений
        return row
    row = rows[0]
    if len(rows) > 1:
        extra = sum(r.n or 0 for r in rows[1:])
        await s.execute(SphereBal.__table__.delete().where(SphereBal.id.in_([r.id for r in rows[1:]])))
        await s.execute(update(SphereBal).where(SphereBal.id == row.id).values(n=SphereBal.n + extra).execution_options(synchronize_session=False))
        row.n = (row.n or 0) + extra
        log.warning("Сферы: слиты дубли строк игрока %s (%s)", owner, sid)
    return row


async def add_spheres(s, owner, sid, n):
    row = await sph_row(s, owner, sid)
    res = await s.execute(update(SphereBal).where(SphereBal.id == row.id).values(n=SphereBal.n + n)
                          .returning(SphereBal.n).execution_options(synchronize_session=False))
    _sync(row, "n", res.first())
    return {"id": sid, "n": n, "reg": True}


async def take_spheres(s, owner, sid, n):
    """Атомарно списать n учтённых сфер. False — столько нет (ничего не списано)."""
    row = await sph_row(s, owner, sid)
    res = await s.execute(update(SphereBal).where(SphereBal.id == row.id, SphereBal.n >= n).values(n=SphereBal.n - n)
                          .returning(SphereBal.n).execution_options(synchronize_session=False))
    got = res.first()
    _sync(row, "n", got)
    return got is not None, row


async def drop_spheres(s, owner, sid, n):
    """Списать до n сфер (не ниже нуля) — вещь ушла из сумки."""
    row = await sph_row(s, owner, sid)
    res = await s.execute(update(SphereBal).where(SphereBal.id == row.id)
                          .values(n=case((SphereBal.n > n, SphereBal.n - n), else_=0))
                          .returning(SphereBal.n).execution_options(synchronize_session=False))
    _sync(row, "n", res.first())


def _sync(obj, field, returned):
    if returned is not None:
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(obj, field, returned[0])


async def set_item_status(s, item_uid, owner, from_status, to_status, new_owner=None):
    """Атомарно перевести вещь из одного состояния в другое. True — именно этот запрос её перевёл."""
    vals = {"status": to_status}
    if new_owner is not None:
        vals["owner"] = new_owner
    cond = [ItemInst.uid == item_uid, ItemInst.status == from_status]
    if owner is not None:
        cond.append(ItemInst.owner == owner)
    res = await s.execute(update(ItemInst).where(*cond).values(**vals).execution_options(synchronize_session=False))
    return res.rowcount == 1


# ---------- дневная «усталость» лута ----------
LOOT_FULL = env_int("LOOT_FULL_KILLS", 2500)     # до стольких убийств в сутки — полный шанс
LOOT_LOW = env_int("LOOT_LOW_KILLS", 6000)       # к этому числу шанс опускается до LOOT_FLOOR
LOOT_FLOOR = env_float("LOOT_FLOOR", 0.2)


def loot_factor(kills_today):
    """Множитель шанса ценного лута. Опыт, лом и обычные материалы не режутся — только то, что продаётся за GRAM."""
    if kills_today <= LOOT_FULL:
        return 1.0
    if kills_today >= LOOT_LOW:
        return LOOT_FLOOR
    return 1.0 - (1.0 - LOOT_FLOOR) * (kills_today - LOOT_FULL) / (LOOT_LOW - LOOT_FULL)


async def count_loot_kill(s, uid, weight):
    """Засчитать убийство в дневной счётчик. Возвращает число убийств за сутки ДО этого."""
    day = int(time.time() // 86400)
    await s.execute(insert_ignore(LootDay.__table__, tg_id=uid, day=day, kills=0))
    res = await s.execute(update(LootDay).where(LootDay.tg_id == uid)
                          .values(kills=case((LootDay.day == day, LootDay.kills + weight), else_=weight), day=day)
                          .returning(LootDay.kills).execution_options(synchronize_session=False))
    row = res.first()
    return (row[0] - weight) if row else 0


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
                                      "sph": {k: sph.get(k, 0) for k in REG_IDS}})

        if op == "craft":
            # руна или дрон собираются по последнему сохранению: ресурсы списываются там, вещь учитывается
            item_id = str(body.get("id", ""))
            if item_id not in CRAFT:
                return web.json_response({"ok": False, "error": "Такое нельзя собрать"})
            import progress
            import saveguard
            # строго по очереди с сохранениями и другими крафтами этого игрока: иначе два крафта подряд
            # (или сохранение, начатое до крафта) дают вещь без списания ресурсов
            async with saveguard.player_lock(uid):
                row = (await s.execute(select(GameSave).where(GameSave.tg_id == uid))).scalar_one_or_none()
                try:
                    data = json.loads(row.data) if row and row.data else None
                except ValueError:
                    data = None
                S = data.get("S") if isinstance(data, dict) else None
                if not isinstance(S, dict):
                    return web.json_response({"ok": False, "error": "Сохранение не найдено, попробуй через минуту"})
                try:
                    level = int(S.get("level", 1))
                except (TypeError, ValueError, OverflowError):
                    level = 1
                if not user.get("admin"):
                    level = min(level, await progress.cap_of(s, uid))
                err = craft_in_save(S, item_id, level)
                if err:
                    return web.json_response({"ok": False, "error": err})
                if not user.get("admin") and not await take_craft_slot(s, uid):
                    await s.rollback()
                    return web.json_response({"ok": False, "error": f"На сегодня собрано максимум ({CRAFT_DAILY_MAX}). Завтра можно снова"})
                try:
                    craft_n = int(S.get("craftN", 0) or 0) + 1
                except (TypeError, ValueError, OverflowError):
                    craft_n = 1
                S["craftN"] = craft_n                    # сохранения, собранные до этого крафта, сервер больше не примет
                # updated сравниваем для надёжности; главная защита от гонок — очередь выше
                res = await s.execute(update(GameSave).where(GameSave.tg_id == uid, GameSave.updated == row.updated)
                                      .values(data=json.dumps(data, ensure_ascii=False), updated=int(time.time()))
                                      .execution_options(synchronize_session=False))
                if res.rowcount != 1:
                    await s.rollback()
                    return web.json_response({"ok": False, "error": "Сохранение как раз обновлялось, нажми ещё раз"})
                await add_spheres(s, uid, item_id, 1)
                await s.commit()
            metrics.inc("craft." + ("drone" if item_id in DRONES else "artifact" if item_id in ARTIFACTS else "wings" if item_id in WINGS else "cloak" if item_id in CLOAKS else "rune"))
            return web.json_response({"ok": True, "id": item_id, "scrap": S.get("scrap", 0), "cores": S.get("cores", 0), "craftN": craft_n})

        if op in ("socket", "unsocket"):
            # руна вставлена в вещь или вынута: её учёт переходит в «гнездо» (s:<id>) и обратно.
            # Продаваемых рун от этого не становится больше, чем сервер выдал, — меняется только, где лежит учёт.
            rid = str(body.get("id", ""))
            if rid not in RUNES:
                return web.json_response({"ok": False, "error": "Это не руна"})
            src, dst = (rid, SOCKET_PREFIX + rid) if op == "socket" else (SOCKET_PREFIX + rid, rid)
            ok, _ = await take_spheres(s, uid, src, 1)
            if not ok:
                await s.rollback()
                return web.json_response({"ok": False, "error": "Нет учтённой руны"})
            await add_spheres(s, uid, dst, 1)
            await s.commit()
            return web.json_response({"ok": True})

        if op == "kill":
            # старый путь (HTTP) — для закэшированных клиентов; новые шлют убийства через WebSocket
            me = online(uid)
            drops, cap, lf = await process_kills(s, uid, me, body.get("kills") or [{"mob": body.get("mob"), "loc": body.get("loc")}])
            return web.json_response({"ok": True, "drops": drops, "lvlCap": cap})

        if op == "enchant":
            x = (await s.execute(select(ItemInst).where(ItemInst.uid == str(body.get("uid", ""))))).scalar_one_or_none()
            sid = body.get("sphere")
            if not x or x.owner != uid or x.status != "inv" or sid not in SPHERES:
                return web.json_response({"ok": False, "error": "Вещь не найдена на сервере"})
            if x.e >= len(ENCH_CHANCE):
                return web.json_response({"ok": False, "error": "Максимальная заточка"})
            ok, sph = await take_spheres(s, uid, sid, 1)
            if not ok:
                return web.json_response({"ok": False, "error": "Нет учтённой сферы"})
            e0 = x.e
            if random.random() * 100 < ENCH_CHANCE[e0]:
                vals, result = {"e": e0 + 1}, "ok"
            elif sid == "sph_ti":
                vals, result = ({"e": e0 - 1} if e0 > 0 else {}), "fail"          # безопасная заточка: при неудаче −1, не ниже +0
            else:
                vals, result = {"status": "gone"}, "broken"
            if vals:
                # вещь меняется, только если с момента чтения её никто не тронул (двойное нажатие, другая вкладка)
                res = await s.execute(update(ItemInst).where(ItemInst.uid == x.uid, ItemInst.owner == uid, ItemInst.status == "inv", ItemInst.e == e0)
                                      .values(**vals).execution_options(synchronize_session=False))
                if res.rowcount != 1:
                    await s.rollback()
                    return web.json_response({"ok": False, "error": "Вещь уже изменилась, попробуй ещё раз"})
                _sync(x, "e", (vals.get("e", x.e),))
            await s.commit()
            return web.json_response({"ok": True, "result": result, "e": x.e, "sph": sph.n})

        if op == "gone":
            # вещь продана торговцу, вложена в кодекс или разрушена — больше не существует
            uids = [str(u)[:24] for u in (body.get("uids") or [])][:50]
            if uids:
                await s.execute(update(ItemInst).where(ItemInst.uid.in_(uids), ItemInst.owner == uid, ItemInst.status == "inv")
                                .values(status="gone").execution_options(synchronize_session=False))
            sp = body.get("sph") or {}
            for sid in REG_IDS:
                try:
                    n = int(sp.get(sid, 0) or 0)
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    await drop_spheres(s, uid, sid, min(n, 100000))
            await s.commit()
            return web.json_response({"ok": True})
    raise web.HTTPBadRequest(text="bad op")


# ---------- для маркета ----------
async def escrow_for_market(s, uid, item):
    """Проверить и заблокировать предмет под лот. Возвращает (данные предмета с сервера, ошибка).
    Блокировка атомарная: одну вещь нельзя выставить двумя одновременными запросами."""
    if item.get("uid"):
        x = (await s.execute(select(ItemInst).where(ItemInst.uid == str(item["uid"])))).scalar_one_or_none()
        if not x or x.owner != uid or x.status != "inv" or not await set_item_status(s, x.uid, uid, "inv", "market"):
            return None, "Эта вещь не подтверждена сервером, продать её за GRAM нельзя"
        return {"id": x.item, "g": x.g, "e": x.e, "n": 1, "uid": x.uid}, None
    if item.get("id") in REG_IDS:
        n = max(1, min(999, int(item.get("n", 1))))
        ok, row = await take_spheres(s, uid, item["id"], n)
        if not ok:
            what = "сфер" if item["id"] in SPHERES else "дронов" if item["id"] in DRONES else "артефактов" if item["id"] in ARTIFACTS else "крыльев" if item["id"] in WINGS else "плащей" if item["id"] in CLOAKS else "рун"
            return None, f"Учтённых {what} только {row.n or 0}: остальные нельзя продать за GRAM"
        return {"id": item["id"], "g": 0, "e": 0, "n": n, "reg": True}, None
    return None, "За GRAM можно продавать только снаряжение, сферы с мобов, руны, дронов и артефакты"


async def market_transfer(s, item, to_uid, status="inv"):
    """Передать предмет лота новому владельцу (покупка) или вернуть продавцу (снятие)."""
    if item.get("uid"):
        if not await set_item_status(s, item["uid"], None, "market", status, new_owner=to_uid):
            log.warning("Маркет: вещь %s не была в статусе market", item["uid"])
    elif item.get("reg"):
        await add_spheres(s, to_uid, item["id"], int(item.get("n", 1)))


# ---------- обработка убийств (WebSocket и HTTP) ----------
async def process_kills(s, uid, me, kills):
    """Пачка убийств [{mob, loc, iid}]. Возвращает (выпавшие вещи, допустимый уровень, множитель лута).
    Коммитит сессию сам."""
    import progress
    import pvp
    import chipwar
    import funnel
    cap = await progress.cap_of(s, uid)                  # уровень — серверный, а не присланный игроком
    import vip
    drops, lf, vip_lv, ticket = [], 1.0, None, None
    if not isinstance(kills, list):
        kills = []
    for i, k in enumerate(kills[:40]):
        if not isinstance(k, dict):
            continue
        mob, loc = str(k.get("mob", ""))[:24], str(k.get("loc", ""))[:24]
        lv = mob_level(mob, loc)
        if not me or me.get("loc") != loc or lv is None or lv > (cap or 1) + 12 or not allow_kill(uid):
            metrics.inc("kill.rejected_basic")
            continue                                   # не в этой локации, слишком сильный моб или слишком часто
        if not mobguard.allow_kill(uid, k.get("iid"), mob):
            continue                                   # сервер не видел боя с этим мобом (режим MOB_GUARD=on)
        boss = mob.startswith("db")
        mult = 1
        if boss:
            if time.time() - _boss_t.get(uid, 0) < BOSS_GAP:
                continue                               # главари не могут умирать слишком часто
            _boss_t[uid], mult = time.time(), BOSS_LOOT
        await pvp.mob_killed(uid, me)
        cap = await progress.add_kill(s, uid, lv, boss)
        import seasonpts
        ctr = seasonpts.kill_counter(loc)
        if ctr:
            await seasonpts.add(uid, ctr, 1)                # задания сезона: убийства на этажах и в полях
        before = await count_loot_kill(s, uid, 10 if boss else 1)
        lf = loot_factor(before)
        bonus = chipwar.loot_mult(me.get("fac"))
        if vip_lv is None:
            vip_lv = await vip.level(s, uid, bool(me.get("admin")))
        if ticket is None:
            import seasonpts
            ticket = await seasonpts.has_ticket(uid)
        for d in roll(lv, mult * lf * bonus * vip.drop_mult(vip_lv) * (1.6 if ticket else 1) * SEASON_DROP.get(loc, 1)):   # билет сезона: +60% к дропу   # VIP-бонус к дропу считает сервер
            item = await mint_gear(s, uid, d["id"], d["g"]) if d["kind"] == "gear" else await add_spheres(s, uid, d["id"], d["n"])
            item["i"] = i
            drops.append(item)
        metrics.inc("kill.ok")
        funnel.mark(uid, "kill1")
        if boss:
            funnel.mark(uid, "boss1")
    await s.commit()
    if drops:
        funnel.mark(uid, "loot1")
        log.info("Лут: игрок %s → %s", uid, [d["id"] for d in drops])
    if me:
        me["lvl_cap"] = cap
    for lvl_mark in (5, 10, 20):
        if (cap or 1) >= lvl_mark:
            funnel.mark(uid, f"lvl{lvl_mark}")
    return drops, cap, round(lf, 2)


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
# "W2" — оружие класса покупателя 2-го поколения, "W3" — 3-го
PACK_ITEMS = {"books": [("gear", "W2", 2)], "legend": [("gear", "W3", 3)], "spheres": [("sph", "sph_cu", 3)], "cores": [("sph", "sph_ti", 1)],
              # паки рун (как в game.html → PACKS, вкладка «Руны»): руны выдаёт и учитывает сервер
              "rn_base": [("sph", "r_atk", 2), ("sph", "r_def", 2), ("sph", "r_hp", 2)],
              "rn_pro": [("sph", "r_crit", 2), ("sph", "r_cpow", 2), ("sph", "r_aspd", 2), ("sph", "r_spd", 2)],
              "rn_war": [("sph", "r_war", 2)], "rn_bastion": [("sph", "r_bastion", 2)],
              "rn_storm": [("sph", "r_storm", 2)], "rn_fortune": [("sph", "r_fortune", 2)],
              # дроны из магазина тоже учитываются сервером — их можно перепродать на маркете
              "dr_nova": [("sph", "d_nova", 1)], "dr_aegis": [("sph", "d_aegis", 1)],
              "dr_phantom": [("sph", "d_phantom", 1)], "dr_sol": [("sph", "d_sol", 1)],
              # артефакты из магазина
              "ar_crown": [("sph", "a_crown", 1)], "ar_eye": [("sph", "a_eye", 1)],
              "ar_heart": [("sph", "a_heart", 1)], "ar_relic": [("sph", "a_relic", 1)],
              # крылья из магазина (как в game.html → PACKS, вкладка «Крылья»)
              "wn_seraph": [("sph", "wg_seraph", 1)], "wn_void": [("sph", "wg_void", 1)],
              "wn_phoenix": [("sph", "wg_phoenix", 1)], "wn_storm": [("sph", "wg_storm", 1)],
              # плащи из магазина (вкладка «Плащи»)
              "cl_royal": [("sph", "ck_royal", 1)], "cl_night": [("sph", "ck_night", 1)],
              "cl_ember": [("sph", "ck_ember", 1)], "cl_aurora": [("sph", "ck_aurora", 1)]}


async def mint_pack(s, owner, pack):
    out = []
    cls = (await s.execute(select(GameSave.cls).where(GameSave.tg_id == owner))).scalar() or ""
    for kind, iid, v in PACK_ITEMS.get(pack, []):
        if kind == "gear" and iid.startswith("W"):
            iid = weapon_id(cls, int(iid[1]))
        out.append(await mint_gear(s, owner, iid, v, source="shop") if kind == "gear" else await add_spheres(s, owner, iid, v))
    return out


def setup(app):
    app.router.add_post("/api/items/{op}", api_items)
