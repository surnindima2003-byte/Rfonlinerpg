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
# осколки (как в game.html → SHARD): выпадают с мобов от 15 уровня по решению сервера, из них Кузнец собирает
# уникальные сеты классов. Учитываются штуками, продаются на маркете за GRAM.
SHARDS = ("sh_plasma", "sh_ion", "sh_solar", "sh_void", "sh_magma", "sh_cryo", "sh_toxin", "sh_blood")
# чертежи классов (как «свитки классов» в LIBERTY, патч 17 октября «Мир»; зеркало game.html → BP): по чертежу Кузнец
# собирает любую вещь уникального сета этого класса без осколков — нужны только лом и ядра (craft_uniq с bp).
# Падают только в Мире, с монстров спотов, у каждого спота свои (WORLD_BP, roll_world). Учитываются штуками
# и продаются на маркете за GRAM: поднять чертёж может робот любого класса. Порядок — как у CLASS_SHARDS
BLUEPRINTS = ("bp_guard", "bp_reaper", "bp_sniper", "bp_techno", "bp_ghost", "bp_glyph", "bp_medic")
# самоцветы (как в game.html → GEMS, «Кодекс самоцветов» LIBERTY): четыре вида, падают с врагов в Нижних и Глубинных
# цехах, Фарм-зоне, Сезонной зоне и в Мире по решению сервера (roll_gems) — только в живой игре. Учитываются штуками,
# продаются на маркете за GRAM (не дешевле GEM_MIN_PRICE за штуку — webserver.api_market). В Кодексе их сдают
# до GEM_MAX штук каждого вида (op "gem"): сданные списываются с учёта игрока навсегда, а счётчик сданных лежит
# в той же таблице балансов под «cx:<id>» (влезает в 12 символов столбца) — его не нарисовать на телефоне.
# Пятая — Монета удачи (LUCK, патч LIBERTY 17 октября «Мир»): сдаётся в тот же Кодекс рядом с камнями, но падает только
# в Мире — с любого монстра с шансом LUCK_PCT (с босса трона чаще: ×BOSS_LOOT, как весь ценный дроп главаря), — и
# принимается до LUCK_MAX штук (gem_cap); свои пороги — GEM_STEPS["l"]
GEMS = ("gm_emer", "gm_ruby", "gm_abys", "gm_jade", "gm_luck")
GEMS_COMMON = ("gm_emer", "gm_ruby")            # шанс как у медной сферы (обычная заточка)
GEMS_RARE = ("gm_abys", "gm_jade")              # шанс как у титановой сферы (безопасная заточка)
LUCK, LUCK_MAX, LUCK_PCT = "gm_luck", 200, 0.003
GEM_LOCS = ("sector1", "sector2", "farm1", "season1", "world")
GEM_MAX = 500
GEM_MIN_PRICE = 0.4                             # GRAM за штуку на маркете (как GEM_MIN_PRICE в игре)
GEM_CX = "cx:"
# пороги Кодекса: (сколько сдать, бонус) — зеркало game.html → GEM_STEPS (сверяет test_gems.py). «c» — частые, «r» — редкие,
# «l» — Монета удачи: 30 → +40 атаки, 80 → ещё +70, 200 → ещё +110 (всего +220)
GEM_STEPS = {
    "c": [(1, {"atk": 5}), (10, {"atk": 5, "def": 5}), (20, {"atk": 5, "def": 5}), (50, {"atk": 10}), (100, {"atk": 10, "def": 10}),
          (300, {"atk": 30, "def": 30, "hp": 3000}), (500, {"atk": 50, "def": 50, "rate": 0.2})],
    "r": [(1, {"atk": 10}), (10, {"atk": 10, "def": 10}), (30, {"atk": 20}), (60, {"atk": 20, "def": 20}), (100, {"atk": 30}),
          (150, {"atk": 30, "def": 30}), (300, {"atk": 50, "def": 50, "hp": 5000}), (500, {"atk": 100, "def": 100, "rate": 0.2})],
    "l": [(30, {"atk": 40}), (80, {"atk": 70}), (200, {"atk": 110})],
}
_gem_cx = {}                                    # uid -> {самоцвет: сдано} игроков в сети (заполняют op "state" и "gem")
REG_IDS = SPHERES + RUNES + DRONES + ARTIFACTS + WINGS + CLOAKS + SHARDS + GEMS + BLUEPRINTS      # всё, что учитывается штуками (без номера)
SOCKET_PREFIX = "s:"            # учёт рун, вставленных в вещи (строка «s:r_fortune» влезает в 12 символов столбца)


def gem_kind(gid):
    """Чьи пороги у этого вида: «c» — частый камень, «r» — редкий, «l» — Монета удачи."""
    return "l" if gid == LUCK else "c" if gid in GEMS_COMMON else "r"


def gem_cap(gid):
    """Сколько штук этого вида принимает Кодекс: камней — GEM_MAX, Монет удачи — LUCK_MAX."""
    return LUCK_MAX if gid == LUCK else GEM_MAX


def gem_bonus(cx):
    """Бонус сданных самоцветов {atk, def, hp, rate}: на каждый вид действуют все пороги до сданного числа."""
    out = {"atk": 0, "def": 0, "hp": 0, "rate": 0.0}
    for gid in GEMS:
        n = (cx or {}).get(gid, 0) or 0
        for need, bon in GEM_STEPS[gem_kind(gid)]:
            if n >= need:
                for k, v in bon.items():
                    out[k] += v
    out["rate"] = round(out["rate"], 2)
    return out


def gem_bm(cx):
    """Сколько боевой мощи могут дать самоцветы (формула combatPower в игре) — с запасом ×2: их атаку и прочность
    дальше умножают руны, дрон, зелья и пассивки. Добавляется к пределу БМ в рейтинге (webserver.save)."""
    b = gem_bonus(cx)
    return int(2 * (b["atk"] * 12 + b["def"] * 10 + b["hp"] * 1.5 + b["rate"] * 500))


def remember_gems(uid, cx):
    """Запомнить сданные самоцветы игрока: из них пределы PvP (прочность и броня) и предел БМ в рейтинге."""
    import pvpguard
    _gem_cx[uid] = cx
    b = gem_bonus(cx)
    pvpguard.set_gems(uid, b["hp"], b["def"])


async def load_gem_cx(s, uid):
    """Сколько самоцветов игрок сдал в Кодекс — при входе в мир (рядом с купленными очками для пределов PvP)."""
    rows = (await s.execute(select(SphereBal.item, SphereBal.n).where(
        SphereBal.owner == uid, SphereBal.item.in_([GEM_CX + g for g in GEMS])))).all()
    got = {}
    for item, n in rows:
        g = str(item)[len(GEM_CX):]
        got[g] = got.get(g, 0) + (n or 0)                 # дубли строк (старая гонка) складываются, как в sph_row
    cx = {g: min(gem_cap(g), got.get(g, 0)) for g in GEMS}
    remember_gems(uid, cx)
    return cx


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
# Уникальные сеты классов (зеркало game.html → UNIQ_LVL, UNIQ_COST, CLASS_SHARDS; совпадение проверяет test_craft_contract.py).
# 5 вещей на класс: сенсор, корпус, модуль, реактор, ходовая. Цена вещи: лом, ядра и по n каждого из трёх осколков класса.
# Собранная вещь — снаряжение с номером (золотой грейд), её можно заточить и продать на маркете за GRAM.
UNIQ_LVL = 40
UNIQ_COST = {"head": [60000, 200, 80], "legs": [60000, 200, 80], "module": [80000, 300, 100], "core": [100000, 400, 120], "armor": [120000, 500, 150]}
CLASS_SHARDS = {"guard": ["sh_magma", "sh_solar", "sh_cryo"], "reaper": ["sh_blood", "sh_void", "sh_magma"],
                "sniper": ["sh_ion", "sh_solar", "sh_cryo"], "techno": ["sh_plasma", "sh_ion", "sh_void"],
                "ghost": ["sh_void", "sh_toxin", "sh_plasma"], "glyph": ["sh_solar", "sh_plasma", "sh_blood"],
                "medic": ["sh_toxin", "sh_cryo", "sh_ion"]}
UNIQ_GRADE = 3
UNIQ = {f"u_{cls}_{slot}": (UNIQ_LVL, {"scrap": c[0], "cores": c[1], **{sh: c[2] for sh in shards}})
        for cls, shards in CLASS_SHARDS.items() for slot, c in UNIQ_COST.items()}
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
    _, need = CRAFT[item_id]                    # уровень в CRAFT — справочный: собирать можно с 1-го уровня
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
def _have_any(S, item_id):
    """Сколько штук предмета в сумке и на складе сохранения."""
    return sum(int(x.get("n", 1) or 0) for key in ("inv", "store") for x in (S.get(key) or [])
               if isinstance(x, dict) and x.get("id") == item_id)


def _take_any(S, item_id, n):
    """Списать n штук: сначала из сумки, остаток — со склада."""
    left = n - min(n, _inv_count(S, item_id))
    _inv_take(S, item_id, n - left)
    if left > 0:
        store = S.get("store") or []
        for i in range(len(store) - 1, -1, -1):
            x = store[i]
            if left <= 0:
                break
            if not isinstance(x, dict) or x.get("id") != item_id:
                continue
            take = min(left, int(x.get("n", 1) or 0))
            x["n"] = int(x.get("n", 1) or 0) - take
            left -= take
            if x["n"] <= 0:
                store.pop(i)


def uniq_need(item_id, bp=False):
    """Что берёт Кузнец за уникальную вещь: лом, ядра и по n каждого из трёх осколков класса — или, по чертежу (bp),
    те же лом и ядра и один чертёж этого класса вместо осколков (как BP_NEED в игре)."""
    need = UNIQ[item_id][1]
    if not bp:
        return need
    return {"scrap": need["scrap"], "cores": need["cores"], "bp_" + item_id.split("_")[1]: 1}


def uniq_check(S, item_id, level, bp=False):
    """Можно ли собрать уникальную вещь по данным сохранения. Текст ошибки или None (S не меняется)."""
    # уровень не проверяется: уникальные вещи собираются и надеваются с 1-го уровня
    for k, n in uniq_need(item_id, bp).items():
        have = S.get(k, 0) if k in ("scrap", "cores") else _have_any(S, k)
        if not isinstance(have, (int, float)) or have < n:
            return "Сервер не видит нужных ресурсов. Подожди пару секунд и попробуй ещё раз"
    if len(S.get("inv") or []) >= INV_MAX_SRV:
        return "Освободи место в сумке"
    return None


def uniq_spend(S, item_id, bp=False):
    """Списать в сохранении лом, ядра и осколки (или чертёж) за уникальную вещь (после uniq_check)."""
    for k, n in uniq_need(item_id, bp).items():
        if k in ("scrap", "cores"):
            S[k] = S.get(k, 0) - n
        else:
            _take_any(S, k, n)


from mobguard import BASE_MOBS, LOC_MIN, DUNGEON_RANGE, WORLD_LOC, mob_level      # таблицы мобов — в mobguard (без базы)
SEASON_DROP = {"season1": 1.5}                  # сезонная зона: +50% к ценному дропу
# «Мир»: опыт за монстров ×3 (WORLD.XP в игре) — и убившему, и союзникам по пати (их доля считается с этого опыта).
# Снаряжение с монстров выше 50 ур. — как с 50-го (gearLv в игре): старшее поколение вещей — 45 ур., и без этого
# с топ-спотов (53–59) вещи не падали бы вовсе
WORLD_XP, WORLD_GEAR_LV = 3, 50
# босс трона «Железный Наместник» (worldthrone.py): добыча — как с главаря (шансы ×BOSS_LOOT), и каждому, кто её получил,
# ещё камни заточки: 4–6 медных сфер и с шансом 35% титановая. Долю опыта союзникам по пати босс не даёт —
# свою добычу и опыт получает каждый, кто снёс хотя бы 1% его прочности
THRONE_GIFT = (("sph_cu", 4, 6, 1.0), ("sph_ti", 1, 1, 0.35))
# чертежи классов на спотах Мира (как свитки классов в LIBERTY): у каждого спота свои — зеркало W_SPOTS → bp в игре.
# Шанс каждого чертежа — BP_PCT за монстра (с теми же множителями, что у сфер: VIP, билет, усталость лута)
WORLD_BP = {"tract": ("guard",), "glade": ("reaper",), "mill": ("sniper",), "ravine": ("techno",), "grove": ("medic",),
            "crypts": ("glyph",), "circle": ("ghost",), "eye": ("guard", "reaper", "sniper"), "cliff": ("techno", "medic"),
            "gate": ("glyph", "ghost")}
BP_PCT = 0.003
ENCH_CHANCE = [100, 100, 100, 75, 65, 55, 45, 38, 32, 26, 20, 15, 10, 7, 5]   # как в игре
P = lambda pct: pct / 100.0


def gear_per(lv):
    """Шанс выпадения вещи за одного моба по грейдам (серый, зелёный, синий, золотой) — как gearPer в игре.
    Шанс — на убийство, а не на каждую вещь: какая именно вещь, выбирается из пула случайно, поэтому
    размер пула шанс не меняет. Синее — только с мобов от 20 ур., золотое — от 31 ур."""
    if lv <= 10:
        return [1 / 2500, 1 / 25000, 0, 0]
    if lv <= 19:
        return [1 / 3000, 1 / 10000, 0, 0]
    if lv <= 30:
        return [0, 1 / 8000, 1 / 60000, 0]
    if lv <= 40:
        return [0, 0, 1 / 40000, 1 / 400000]
    return [0, 0, 1 / 30000, 1 / 250000]


SHARD_MIN_LVL = 15
SHARD_BASE = env_float("SHARD_BASE_PCT", 4.0)          # шанс осколка за моба 15 ур., %
SHARD_STEP = env_float("SHARD_STEP_PCT", 0.1)          # прибавка за каждый уровень моба выше 15, %
BOSS_LOOT, BOSS_GAP = 150, 40                   # главарь: шансы ×150, не чаще одного на игрока раз в 40 с
ELITE_LOOT = 2                                   # элитный моб (el…): ценный дроп ×2, кроме осколков (ELITE.LOOT в игре)
_boss_t = {}


def roll(lv, mult=1, shard_mult=None, gear_lv=None):
    """Бросок ценного лута за одного убитого моба (mult > 1 — главарь). shard_mult — свой множитель осколков
    (у элитных мобов осколки не умножаются), по умолчанию тот же mult. gear_lv — уровень, по которому выбираются
    вещи и их шансы (монстры Мира выше 50 ур. — как 50-й), по умолчанию уровень моба."""
    sm = mult if shard_mult is None else shard_mult
    out, s, gl = [], lv - 1, lv if gear_lv is None else gear_lv
    pool = [g for g, need in GEAR if gl - 10 <= need <= gl + 2]
    for grade, per in enumerate(gear_per(gl)):
        # шанс — на моба, потолок 50% (главарь ×150 и бонусы не делают вещь гарантированной)
        if per and pool and random.random() < min(0.5, per * mult):
            out.append({"kind": "gear", "id": random.choice(pool), "g": grade})
    if random.random() < min(0.9, P(0.0033 + 0.000036 * s) * mult):
        out.append({"kind": "sph", "id": "sph_cu", "n": 1})
    if random.random() < min(0.9, P(0.00033 + 0.0000036 * s) * mult):
        out.append({"kind": "sph", "id": "sph_ti", "n": 1})
    # осколки: с 15 уровня мобов, 4% на 15-м и +0,1% за каждый уровень выше; главарь — пачка побольше
    if lv >= SHARD_MIN_LVL and random.random() < min(0.6, P(SHARD_BASE + SHARD_STEP * (lv - SHARD_MIN_LVL)) * sm):
        n = random.randint(4, 9) if mult >= BOSS_LOOT else 1 + (random.random() < 0.35) + (random.random() < 0.1)
        out.append({"kind": "sph", "id": random.choice(SHARDS), "n": n})
    return out


# зелья усиления — дорогой ресурс: в магазине их нет, падают очень редко (зеркало POT_DROP в игре).
# Шанс на моба; главарь ×POT_BOSS. Действуют бонусы к дропу (VIP, билет, улучшение, сезонная зона) и усталость лута
POT_DROP = (("pot_hp", 1 / 2000), ("pot_atk", 1 / 3000), ("pot_xp", 1 / 6000))
POT_BOSS = 10


# обычный дроп, который раньше бросал телефон: материалы, ремкомплект, энергоядра и книги умений.
# Формулы — как mobDrops в игре (сверяет test_server_drops.py); таблица мобов — ledger.mob_info.
# Усталость лута на него не действует (как раньше), бонусы к дропу — действуют.
KIT_ID = "kit_s"
BOOKS_SKILL = (
    "bk_shock", "bk_blades", "bk_plating", "bk_ram", "bk_drain", "bk_saw", "bk_overheat", "bk_hook", "bk_volley",
    "bk_burst", "bk_jet", "bk_overclock", "bk_plasma", "bk_emp", "bk_field", "bk_blink", "bk_phase", "bk_echo",
    "bk_siren", "bk_nulltrace", "bk_gravity", "bk_chain", "bk_seal", "bk_recall", "bk_mend", "bk_nanoswarm",
    "bk_aegis", "bk_surge",
)
BOOKS_PAS = (
    "bp_steelskin", "bp_retaliate", "bp_hunger", "bp_execute", "bp_eagle", "bp_longshot", "bp_overload",
    "bp_cyclotron", "bp_fieldrep", "bp_triage", "bp_hunter", "bp_shade", "bp_monolith", "bp_weave", "bp_swift",
    "bp_vital", "bp_fury", "bp_servo", "bp_cold", "bp_selfrepair", "bp_looter",
)


def soft_table(mob, loc):
    """Шансы обычного дропа за моба без бонусов: {id: вероятность} (главарь и элита уже учтены)."""
    import ledger
    info = ledger.mob_info(mob)
    if not info:
        return {}
    lv, _, loot, boss = info
    lv = lv or ledger.LOC_MIN.get(loc, 1)
    s = lv - 1
    k = BOSS_LOOT if boss else ELITE_LOOT if mob.startswith("el") else 1
    out = {}

    def add(item_id, pct):
        out[item_id] = min(0.9, P(pct) * k)

    base = 0.13 + 0.0079 * s
    for mid, c in loot.items():                                    # материалы: чем меньше вес, тем реже
        add(mid, round(base * max(0.3, min(1.0, c / 0.4)), 4))
    add(KIT_ID, round(0.0267 + 0.0015 * s, 4))
    if lv >= 3 or loc in ledger.LOC_CORES:
        add("cores", round(0.02 + 0.001 * s, 4))
    for b in BOOKS_SKILL:
        add(b, 0.0002 if lv > 10 else 0.0001)
    for b in BOOKS_PAS:
        add(b, 0.0001)
    return out


def roll_soft(mob, loc, mult=1):
    """Материалы, ремкомплект, ядра и книги за одного убитого моба: [{kind, id, n}]. mult — бонусы к дропу."""
    return [{"kind": "soft", "id": item_id, "n": 1} for item_id, ch in soft_table(mob, loc).items()
            if random.random() < min(0.9, ch * mult)]


def roll_pots(mult=1):
    """Зелья за одного убитого моба: [{kind, id, n}]. Не учитываются в реестре — ложатся в сумку как обычные вещи."""
    return [{"kind": "pot", "id": pid, "n": 1} for pid, ch in POT_DROP if random.random() < min(0.5, ch * mult)]


def roll_throne():
    """Камни заточки с босса трона — сверх обычного броска."""
    return [{"kind": "sph", "id": sid, "n": random.randint(lo, hi)} for sid, lo, hi, ch in THRONE_GIFT if random.random() < ch]


def roll_gems(lv, mult=1):
    """Самоцветы за одного убитого моба (вызывается только в GEM_LOCS). Каждый камень бросается отдельно:
    частые — с шансом медной сферы, редкие — титановой; mult — те же множители, что у сфер (главарь, VIP, билет).
    Монета удачи — не здесь: она падает только в Мире (roll_world)."""
    s, out = lv - 1, []
    chance = {"c": P(0.0033 + 0.000036 * s), "r": P(0.00033 + 0.0000036 * s)}
    for gid in GEMS_COMMON + GEMS_RARE:
        if random.random() < min(0.9, chance["c" if gid in GEMS_COMMON else "r"] * mult):
            out.append({"kind": "sph", "id": gid, "n": 1})
    return out


def world_spot(idx):
    """Спот Мира, на котором стоит общий моб номер idx: (id, ур. от, ур. до). None — не монстр спота (босс трона)."""
    n0 = 0
    for sid, n, lo, hi in mobguard.WORLD_SPOTS:
        if n0 <= idx < n0 + n:
            return sid, lo, hi
        n0 += n
    return None


def roll_world(idx, lv, mult=1):
    """Добыча Мира сверх обычной за монстра номер idx (номер — из боя, засчитанного сервером: mobguard.fight_idx).
    Чертежи классов его спота — каждый со своим шансом BP_PCT, и Монета удачи — LUCK_PCT с любого монстра Мира.
    mult — те же множители, что у сфер: у босса трона — ×BOSS_LOOT, поэтому монета с него падает чаще.
    Уровень монстра должен подходить споту: тип моба присылает телефон, и под номером топ-спота «слабый» моб
    чертёж не даст."""
    out, spot = [], world_spot(idx)
    if spot and spot[1] <= lv <= spot[2]:
        for cls in WORLD_BP[spot[0]]:
            if random.random() < min(0.9, P(BP_PCT) * mult):
                out.append({"kind": "sph", "id": "bp_" + cls, "n": 1})
    if random.random() < min(0.9, P(LUCK_PCT) * mult):
        out.append({"kind": "sph", "id": LUCK, "n": 1})
    return out


def gem_give_amount(want, given, bal, cap=GEM_MAX):
    """Сколько самоцветов принять в Кодекс: просили want, уже сдано given, учтено у игрока bal, предел вида cap."""
    try:
        want = int(want)
    except (TypeError, ValueError, OverflowError):
        want = 1
    return max(0, min(max(1, want), cap - max(0, int(given or 0)), max(0, int(bal or 0))))


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
# Жёсткий потолок: от LOOT_LOW до этого числа шанс плавно уходит с LOOT_FLOOR в ноль. Раньше после 6000 убийств
# шанс оставался 20% навсегда, и бот на пределе скорости (2,5 убийства/с, ~200 000 в сутки по весу) набирал
# ~46 000 «полных» убийств против ~4 600 у живого игрока. Теперь за сутки не больше ~4 900 полных убийств у любого.
LOOT_ZERO = env_int("LOOT_ZERO_KILLS", 9000)


def loot_factor(kills_today):
    """Множитель шанса ценного лута. Опыт, лом и обычные материалы не режутся — только то, что продаётся за GRAM."""
    if kills_today <= LOOT_FULL:
        return 1.0
    if kills_today < LOOT_LOW:
        return 1.0 - (1.0 - LOOT_FLOOR) * (kills_today - LOOT_FULL) / (LOOT_LOW - LOOT_FULL)
    if LOOT_ZERO <= LOOT_LOW or kills_today >= LOOT_ZERO:
        return 0.0
    return LOOT_FLOOR * (LOOT_ZERO - kills_today) / (LOOT_ZERO - LOOT_LOW)


async def count_loot_kill(s, uid, weight):
    """Засчитать убийство в дневной счётчик. Возвращает число убийств за сутки ДО этого."""
    day = int(time.time() // 86400)
    await s.execute(insert_ignore(LootDay.__table__, tg_id=uid, day=day, kills=0))
    res = await s.execute(update(LootDay).where(LootDay.tg_id == uid)
                          .values(kills=case((LootDay.day == day, LootDay.kills + weight), else_=weight), day=day)
                          .returning(LootDay.kills).execution_options(synchronize_session=False))
    row = res.first()
    return (row[0] - weight) if row else 0


# ---------- улучшение «Бонус к дропу» ----------
# В игре: +0,1% к шансу дропа за уровень, до 300 уровней (+30%). Уровень хранится в сохранении (S.up.dropup),
# которое присылает телефон, поэтому сервер его ограничивает: не выше 300 и не больше очков навыка,
# которые можно получить к серверному уровню (3 за уровень + запас на очки из паков).
DROPUP_MAX, DROPUP_PER = 300, 0.001
DROPUP_PTS_SLACK = env_int("DROPUP_PTS_SLACK", 30)
DROPUP_TTL = 60
_dropup = {}                                    # tg_id -> (время, уровень из сохранения)


def dropup_level(raw, cap):
    """Уровень улучшения, который сервер признаёт: из сохранения, но не выше 300 и не выше очков к уровню cap."""
    try:
        lv = int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(lv, DROPUP_MAX, 3 * max(0, int(cap or 1) - 1) + DROPUP_PTS_SLACK))


async def dropup_mult(s, uid, cap):
    """Множитель ценного дропа от улучшения (1.0 … 1.3). Сохранение читается не чаще раза в минуту."""
    hit = _dropup.get(uid)
    if hit and time.time() - hit[0] < DROPUP_TTL:
        raw = hit[1]
    else:
        raw = 0
        data = (await s.execute(select(GameSave.data).where(GameSave.tg_id == uid))).scalar_one_or_none()
        if data:
            try:
                S = json.loads(data).get("S")                    # сохранение: {"S": {..., "up": {"dropup": N}}}
                up = S.get("up") if isinstance(S, dict) else None
                raw = up.get("dropup", 0) if isinstance(up, dict) else 0
            except (ValueError, AttributeError):
                raw = 0
        _dropup[uid] = (time.time(), raw)
    return 1 + DROPUP_PER * dropup_level(raw, cap)


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
            gemcx = {k: min(gem_cap(k), sph.get(GEM_CX + k, 0) or 0) for k in GEMS}
            remember_gems(uid, gemcx)
            return web.json_response({"ok": True, "items": [{"uid": x.uid, "id": x.item, "g": x.g, "e": x.e} for x in items],
                                      "sph": {k: sph.get(k, 0) for k in REG_IDS}, "gemcx": gemcx})

        if op == "gem":
            # Кодекс → «Самоцветы»: сданные камни списываются с учёта игрока и навсегда прибавляются к счётчику
            # (до GEM_MAX, Монеты удачи — до LUCK_MAX). Принимаются только учтённые сервером (выпавшие с врагов
            # или купленные на маркете), как и на маркете.
            gid = str(body.get("id", ""))
            if gid not in GEMS:
                return web.json_response({"ok": False, "error": "Это не самоцвет"})
            cap = gem_cap(gid)
            cx = await sph_row(s, uid, GEM_CX + gid)
            bal = await sph_row(s, uid, gid)
            k = gem_give_amount(body.get("n", 1), cx.n, bal.n, cap)
            if k <= 0:
                given, have = min(cap, cx.n or 0), bal.n or 0          # прочитать до отката: он сбрасывает объекты сессии
                await s.rollback()
                return web.json_response({"ok": False, "cx": given, "sph": have,
                                          "error": f"Собрано максимум: {cap}" if given >= cap else
                                          "Нет учтённых монет: сдать можно только выпавшие в Мире или купленные на маркете" if gid == LUCK else
                                          "Нет учтённых самоцветов: сдать можно только выпавшие с врагов или купленные на маркете"})
            ok, bal = await take_spheres(s, uid, gid, k)
            if not ok:
                await s.rollback()
                return web.json_response({"ok": False, "error": "Самоцветы как раз изменились, попробуй ещё раз"})
            # счётчик растёт, только если не перевалит за предел: два одновременных запроса не сдадут лишнего
            res = await s.execute(update(SphereBal).where(SphereBal.id == cx.id, SphereBal.n + k <= cap).values(n=SphereBal.n + k)
                                  .returning(SphereBal.n).execution_options(synchronize_session=False))
            got = res.first()
            if got is None:
                await s.rollback()
                return web.json_response({"ok": False, "error": "Самоцветы как раз изменились, попробуй ещё раз"})
            await s.commit()
            remember_gems(uid, {**_gem_cx.get(uid, {}), gid: got[0]})
            metrics.inc("gem.codex", k)
            return web.json_response({"ok": True, "id": gid, "given": k, "cx": got[0], "sph": bal.n or 0})

        if op == "craft":
            # руна или дрон собираются по последнему сохранению: ресурсы списываются там, вещь учитывается
            item_id = str(body.get("id", ""))
            if item_id in UNIQ:
                return await craft_uniq(s, uid, user, item_id, bool(body.get("bp")))
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
                import ledger                                                    # обломки: половина цены продажи, не меньше 5
                ledger.add(uid, {"scrap": max(5, round(ledger.gear_sell(x.item, x.g, e0) * 0.5))})
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
                gone = (await s.execute(select(ItemInst.item, ItemInst.g, ItemInst.e).where(
                    ItemInst.uid.in_(uids), ItemInst.owner == uid, ItemInst.status == "inv"))).all()
                await s.execute(update(ItemInst).where(ItemInst.uid.in_(uids), ItemInst.owner == uid, ItemInst.status == "inv")
                                .values(status="gone").execution_options(synchronize_session=False))
                # учёт ресурсов: проданная торговцу вещь даёт лом по цене продажи (вложенная в кодекс — ничего,
                # но сервер их не различает: берём наибольшее)
                import ledger
                ledger.add(uid, {"scrap": sum(ledger.gear_sell(it, g, e) for it, g, e in gone)})
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


async def craft_uniq(s, uid, user, item_id, bp=False):
    """Кузнец: уникальная вещь класса из осколков — или по чертежу класса (bp). Осколки и чертёж списываются
    с серверного учёта (их нельзя нарисовать на телефоне), лом и ядра — из последнего сохранения; вещь получает
    номер и пишется в сохранение."""
    import progress
    import saveguard
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
        err = uniq_check(S, item_id, level, bp)
        if err:
            return web.json_response({"ok": False, "error": err})
        for sh, n in uniq_need(item_id, bp).items():
            if sh in SHARDS or sh in BLUEPRINTS:
                ok, bal = await take_spheres(s, uid, sh, n)
                if not ok:
                    await s.rollback()
                    if sh in BLUEPRINTS:
                        return web.json_response({"ok": False, "error": "Нет учтённого чертежа: он должен выпасть в Мире или быть куплен на маркете"})
                    return web.json_response({"ok": False, "error": f"Учтённых осколков ({sh}) только {bal.n or 0} из {n}. "
                                                                    "Осколки должны выпасть с мобов или быть куплены на маркете"})
        if not user.get("admin") and not await take_craft_slot(s, uid):
            await s.rollback()
            return web.json_response({"ok": False, "error": f"На сегодня собрано максимум ({CRAFT_DAILY_MAX}). Завтра можно снова"})
        uniq_spend(S, item_id, bp)
        item = await mint_gear(s, uid, item_id, UNIQ_GRADE, 0, source="craft")
        S.setdefault("inv", []).append({"id": item_id, "n": 1, "g": UNIQ_GRADE, "e": 0, "uid": item["uid"]})
        try:
            craft_n = int(S.get("craftN", 0) or 0) + 1
        except (TypeError, ValueError, OverflowError):
            craft_n = 1
        S["craftN"] = craft_n
        res = await s.execute(update(GameSave).where(GameSave.tg_id == uid, GameSave.updated == row.updated)
                              .values(data=json.dumps(data, ensure_ascii=False), updated=int(time.time()))
                              .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            await s.rollback()
            return web.json_response({"ok": False, "error": "Сохранение как раз обновлялось, нажми ещё раз"})
        await s.commit()
    metrics.inc("craft.uniq_bp" if bp else "craft.uniq")
    log.info("Кузнец: игрок %s собрал %s (%s)%s", uid, item_id, item["uid"], " по чертежу" if bp else "")
    return web.json_response({"ok": True, "id": item_id, "item": item, "scrap": S.get("scrap", 0), "cores": S.get("cores", 0), "craftN": craft_n})


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
            what = "сфер" if item["id"] in SPHERES else "осколков" if item["id"] in SHARDS else "монет удачи" if item["id"] == LUCK else "самоцветов" if item["id"] in GEMS else "чертежей" if item["id"] in BLUEPRINTS else "дронов" if item["id"] in DRONES else "артефактов" if item["id"] in ARTIFACTS else "крыльев" if item["id"] in WINGS else "плащей" if item["id"] in CLOAKS else "рун"
            return None, f"Учтённых {what} только {row.n or 0}: остальные нельзя продать за GRAM"
        return {"id": item["id"], "g": 0, "e": 0, "n": n, "reg": True}, None
    return None, "За GRAM можно продавать только снаряжение, сферы, осколки, самоцветы и чертежи с мобов, руны, дронов, артефакты, крылья и плащи"


async def market_transfer(s, item, to_uid, status="inv"):
    """Передать предмет лота новому владельцу (покупка) или вернуть продавцу (снятие)."""
    if item.get("uid"):
        if not await set_item_status(s, item["uid"], None, "market", status, new_owner=to_uid):
            log.warning("Маркет: вещь %s не была в статусе market", item["uid"])
    elif item.get("reg"):
        await add_spheres(s, to_uid, item["id"], int(item.get("n", 1)))


# ---------- обработка убийств (WebSocket и HTTP) ----------
async def process_kills(s, uid, me, kills, exp_out=None):
    """Пачка убийств [{mob, loc, iid}]. Возвращает (выпавшие вещи, допустимый уровень, множитель лута).
    exp_out — список: в него добавляется базовый опыт каждого засчитанного убийства (для опыта пати).
    Коммитит сессию сам."""
    import progress
    import pvp
    import funnel
    cap = await progress.cap_of(s, uid)                  # уровень — серверный, а не присланный игроком
    import vip
    drops, lf, vip_lv, ticket, dup = [], 1.0, None, None, None
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
        # Мир: номер монстра из боя, который засчитал сервер, — по нему спот и его чертежи (проверка ниже бой забудет).
        # Без такого боя (повтор заявки, убийство «своими силами», когда сервер молчал) чертежей и монеты нет
        widx = mobguard.fight_idx(uid, k.get("iid"), mob) if loc == WORLD_LOC else None
        if not mobguard.allow_kill(uid, k.get("iid"), mob):
            continue                                   # сервер не видел боя с этим мобом (режим MOB_GUARD=on)
        boss = mobguard.is_boss(mob)                  # главарь подземелья или босс трона в Мире
        mult = 1
        if boss:
            if time.time() - _boss_t.get(uid, 0) < BOSS_GAP:
                continue                               # главари не могут умирать слишком часто
            _boss_t[uid], mult = time.time(), BOSS_LOOT
        await pvp.mob_killed(uid, me)
        xm = WORLD_XP if loc == WORLD_LOC else 1
        cap = await progress.add_kill(s, uid, lv, boss, mult=xm)
        if exp_out is not None and mob != mobguard.THRONE_BOSS:
            exp_out.append(progress.mob_exp(lv, boss) * xm)
        import seasonpts
        ctr = seasonpts.kill_counter(loc)
        if ctr:
            await seasonpts.add(uid, ctr, 1)                # задания сезона: убийства на этажах и в полях
        before = await count_loot_kill(s, uid, 10 if boss else 1)
        lf = loot_factor(before)
        if vip_lv is None:
            vip_lv = await vip.level(s, uid, bool(me.get("admin")))
        if ticket is None:
            import seasonpts
            ticket = await seasonpts.has_ticket(uid)
        # учёт ресурсов: сколько лома/материалов/ядер мог дать этот моб (ledger.py)
        import ledger
        vrow = vip.LEVELS[vip_lv - 1] if vip_lv else (0, 0, 0, 0)
        ledger.add(uid, ledger.kill_income(mob, loc, vip_scrap=vrow[2],
                                           drop_bonus=vrow[3] + (0.6 if ticket else 0) + (0.5 if loc == "season1" else 0)))
        if dup is None:
            dup = await dropup_mult(s, uid, cap)                        # улучшение «Бонус к дропу» (до +30%)
        base_mult = mult * lf * vip.drop_mult(vip_lv) * dup * (1.6 if ticket else 1) * SEASON_DROP.get(loc, 1)   # билет сезона: +60% к дропу   # VIP-бонус к дропу считает сервер
        em = ELITE_LOOT if mob.startswith("el") else 1                  # элитная зона: ценный дроп ×2 (осколки — как обычно)
        for d in roll(lv, base_mult * em, shard_mult=base_mult, gear_lv=min(lv, WORLD_GEAR_LV) if loc == WORLD_LOC else None):
            item = await mint_gear(s, uid, d["id"], d["g"]) if d["kind"] == "gear" else await add_spheres(s, uid, d["id"], d["n"])
            item["i"] = i
            drops.append(item)
        pot_mult = (POT_BOSS if boss else 1) * lf * vip.drop_mult(vip_lv) * dup * (1.6 if ticket else 1) * SEASON_DROP.get(loc, 1)
        for d in roll_pots(pot_mult):                                   # зелья: решает сервер, в реестр не идут
            drops.append({"id": d["id"], "n": d["n"], "i": i})
        soft_mult = vip.drop_mult(vip_lv) * dup * (1.6 if ticket else 1) * SEASON_DROP.get(loc, 1)
        for d in roll_soft(mob, loc, soft_mult):                        # материалы, ремкомплект, ядра, книги
            drops.append({"id": d["id"], "n": d["n"], "i": i})
        if mob == mobguard.THRONE_BOSS:                                 # босс трона: ещё и камни заточки
            for d in roll_throne():
                item = await add_spheres(s, uid, d["id"], d["n"])
                item["i"] = i
                drops.append(item)
        if loc in GEM_LOCS:                                             # самоцветы: цеха, Фарм-зона, Сезонная зона и Мир
            for d in roll_gems(lv, base_mult):
                item = await add_spheres(s, uid, d["id"], d["n"])
                item["i"] = i
                drops.append(item)
        if widx is not None:                                            # Мир: чертежи классов спота и Монета удачи
            for d in roll_world(widx, lv, base_mult):
                item = await add_spheres(s, uid, d["id"], d["n"])
                item["i"] = i
                drops.append(item)
                metrics.inc("world.luck" if d["id"] == LUCK else "world.blueprint")
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


# Что из паков магазина GRAM учитывает сервер (зеркало PACKS в game.html; совпадение проверяет test_pack_items.py).
# Учтённое можно продать на маркете за GRAM. Записи:
#   ("gset", поколение, грейд, заточка) — набор брони: сенсор, корпус, ходовая, модуль, реактор (как gsetIds в игре);
#   ("wcls", поколение, грейд, заточка) — оружие класса покупателя;
#   ("sph", id, сколько) — сферы, руны, дроны, артефакты, крылья, плащи (учёт штуками).
# Раньше снаряжение и сферы из паков «Паки» и «Допы» сервер не учитывал вовсе (в таблице были ключи
# books/legend/spheres/cores, которых нет в магазине): заплативший GRAM не мог продать купленное.
GSET_FAMS = ("sensor", "armor", "legs", "module", "core")
PACK_ITEMS = {"p_base": [("gset", 2, 1, 0), ("wcls", 2, 1, 0)],
              "p_std": [("gset", 4, 1, 5), ("wcls", 4, 1, 5)],
              "p_elite": [("gset", 6, 2, 3), ("wcls", 6, 2, 3), ("sph", "sph_ti", 5)],
              "p_legend": [("gset", 8, 2, 8), ("wcls", 8, 2, 8), ("sph", "sph_ti", 15)],
              "p_epic": [("gset", 10, 3, 8), ("wcls", 10, 3, 8), ("sph", "sph_ti", 30), ("sph", "sph_cu", 100)],
              "d_start": [("gset", 3, 2, 3), ("wcls", 3, 2, 3)], "d_base": [("gset", 5, 2, 5), ("wcls", 5, 2, 5)],
              "d_adv": [("gset", 7, 3, 5), ("wcls", 7, 3, 5)], "d_sup": [("gset", 8, 3, 7), ("wcls", 8, 3, 7)],
              "d_top": [("gset", 9, 3, 9), ("wcls", 9, 3, 9)], "d_admin": [("gset", 10, 3, 10), ("wcls", 10, 3, 10)],
              "x_books": [("wcls", 5, 2, 0)],
              "u1": [("sph", "sph_ti", 10), ("sph", "sph_cu", 50)], "u2": [("sph", "sph_ti", 20), ("sph", "sph_cu", 100)],
              "u3": [("sph", "sph_ti", 45), ("sph", "sph_cu", 200)],
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
    """Учесть содержимое пака. Каждая запись ответа помечена rw — какую строку пака она заменяет
    (телефон выдаёт эти вещи с номерами сервера и не дублирует их своими)."""
    out = []
    cls = (await s.execute(select(GameSave.cls).where(GameSave.tg_id == owner))).scalar() or ""
    for entry in PACK_ITEMS.get(pack, []):
        kind, a, b = entry[:3]
        c = entry[3] if len(entry) > 3 else 0
        if kind == "gset":
            for fam in GSET_FAMS:
                it = await mint_gear(s, owner, f"g_{fam}_{a}", b, c, source="shop")
                it["rw"] = "gset"
                out.append(it)
        elif kind == "wcls":
            it = await mint_gear(s, owner, weapon_id(cls, a), b, c, source="shop")
            it["rw"] = "wcls"
            out.append(it)
        else:
            it = await add_spheres(s, owner, a, b)
            it["rw"] = a
            out.append(it)
    return out


def setup(app):
    app.router.add_post("/api/items/{op}", api_items)


def cleanup(online_ids):
    """Раз в минуту: таймеры главарей и жетоны частоты убийств ушедших игроков (раньше копились навсегда)."""
    now = time.time()
    for uid in [u for u, t in _boss_t.items() if now - t > BOSS_GAP]:
        _boss_t.pop(uid, None)
    for uid in [u for u in _bucket if u not in online_ids]:
        _bucket.pop(uid, None)
    for uid in [u for u in _dropup if u not in online_ids]:
        _dropup.pop(uid, None)
    for uid in [u for u in _gem_cx if u not in online_ids]:          # при следующем входе их снова пришлёт op "state"
        _gem_cx.pop(uid, None)
