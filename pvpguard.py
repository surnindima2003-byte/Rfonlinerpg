"""Защита PvP и передвижения: сервер сам ведёт CP и прочность игроков в бою и проверяет скорость.

Раньше прочность в PvP считал только телефон жертвы: изменённый клиент мог просто не отнимать урон
(«бессмертие») и никогда не присылать pvp_dead. Теперь:

* Пределы. Прочность, CP и броня из сообщения pos ограничены сверху тем, что вообще возможно на
  подтверждённом сервером уровне при купленных паками очках навыка (таблицы HP_AB/DEF_AB по формуле игры).
* Бой. После первого удара игрок 10 секунд «в бою». Сервер сам вычитает урон из своих CP и прочности.
  Отчёты телефона в бою принимаются только вниз; рост прочности — лишь в пределах «бюджета лечения»
  (регенерация, ремкомплекты, подхил), CP в бою не восстанавливаются, как и в игре.
* Смерть. Если по расчёту сервера прочность кончилась, смерть засчитывается сервером, даже если телефон
  жертвы молчит. Урон по прочности сервер считает с запасом (HP_FACTOR): честный телефон всегда умирает
  раньше и сам сообщает о смерти, поэтому честных игроков сервер «раньше времени» не убивает.
* Скорость. Перемещение между двумя pos ограничено запасом пути (MOVE_RATE в секунду, MOVE_BURST на рывки
  и телепорт-умения). Переход между локациями, смерть и возрождение проверку сбрасывают.

Режимы (переменные окружения):
  PVP_GUARD  = on | shadow | off   (по умолчанию on)
  MOVE_GUARD = on | shadow | off   (по умолчанию shadow: сначала смотрим /metrics, потом включаем)
В режиме shadow нарушения только считаются в метриках (pvpguard.*, moveguard.*).
"""
import math
import os
import time

import metrics
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

PVP_MODE = os.getenv("PVP_GUARD", "on").strip().lower()
MOVE_MODE = os.getenv("MOVE_GUARD", "shadow").strip().lower()

COMBAT_SEC = 10.0          # столько секунд после удара игрок считается в бою
HP_FACTOR = 0.55           # доля урона по прочности, которую сервер засчитывает (клиент режет урон пассивками до 45%)
MOVE_RATE = env_float("MOVE_RATE", 460)     # пикселей в секунду (быстрейший класс с бонусами ~300)
MOVE_BURST = env_float("MOVE_BURST", 650)   # запас на рывок (1150 px/с × 0,15 с) и телепорт 360

_state = {}                # uid -> {"hp", "cp", "t", "heal", "heal_t"}


def _now():
    return time.monotonic()


# ---------- пределы ----------
# Честный максимум по формуле stat() из game.html (считает tools/stat_caps.js, сверяет test_pvp_caps.py).
# Для уровня L пара (a, b): прочность не больше a + b·u, где u — очки улучшения «Прочность». В a уже лучшее,
# что игрок может надеть и собрать: золотое +15 своего класса (и уникальный сет), комплект, полный кодекс, руны, лучшие дрон,
# артефакт, крылья, плащ, зелье и «Живучесть» 10. Сервер знает только уровень и купленные очки — остальное
# берётся по максимуму. Раньше предел шёл от предела боевой мощи: на 1 ур. 2 613 прочности и 392 брони
# (меньше, чем у честного игрока с кодексом), а на 30 ур. 32 000 прочности и 320 000 CP (больше любого честного).
HP_AB = [
    (6045, 74.517), (6071, 74.51), (6098, 74.517), (6125, 74.517), (7112, 75.97),
    (7139, 75.977), (7167, 75.97), (7197, 75.977), (7223, 75.977), (8554, 77.437),
    (8584, 77.444), (8612, 77.43), (8638, 77.437), (8667, 77.434), (10265, 78.897),
    (10294, 78.894), (10322, 78.897), (10351, 78.897), (10381, 78.897), (12169, 80.357),
    (12197, 80.357), (12229, 80.357), (12257, 80.357), (12288, 80.354), (14365, 81.814),
    (14392, 81.82), (14424, 81.824), (14455, 81.817), (14483, 81.82), (16777, 83.284),
    (16808, 83.28), (16839, 83.28), (16870, 83.28), (16898, 83.284), (19434, 84.74),
    (19465, 84.737), (19496, 84.744), (19528, 84.737), (19559, 84.74), (36989, 86.2),
    (37021, 86.204), (37050, 86.204), (37083, 86.204), (37112, 86.204), (37541, 87.664),
    (37572, 87.66), (37602, 87.664), (37634, 87.664), (37664, 87.664), (37694, 87.664),
]
DEF_AB = [
    (2021, 4.824), (2024, 4.824), (2027, 4.824), (2030, 4.824), (2091, 4.824),
    (2094, 4.824), (2098, 4.824), (2101, 4.824), (2104, 4.824), (2131, 4.824),
    (2134, 4.824), (2138, 4.824), (2141, 4.824), (2144, 4.824), (2222, 4.824),
    (2225, 4.824), (2228, 4.824), (2231, 4.82), (2233, 4.824), (2308, 4.824),
    (2311, 4.824), (2314, 4.824), (2318, 4.824), (2321, 4.824), (2399, 4.824),
    (2402, 4.824), (2405, 4.824), (2408, 4.824), (2411, 4.824), (2534, 4.824),
    (2539, 4.824), (2542, 4.82), (2544, 4.824), (2547, 4.824), (2632, 4.824),
    (2635, 4.824), (2640, 4.824), (2643, 4.824), (2646, 4.824), (4184, 5.544),
    (4187, 5.544), (4193, 5.544), (4196, 5.544), (4200, 5.544), (4230, 5.547),
    (4233, 5.547), (4239, 5.547), (4242, 5.547), (4246, 5.547), (4249, 5.547),
]
UP_MAX = 300                 # очков в одном улучшении («Прочность», «Броня», «CP»)
PTS_PER_LEVEL = 3           # очков навыка за уровень
CP_PER_POINT = 200           # улучшение «CP»: +200 за очко (раньше сервер его не учитывал и срезал CP)
PACK_PTS = {"p_std": 1, "p_elite": 5, "p_legend": 10, "p_epic": 20}   # очки навыка в паках магазина GRAM
CAP_SLACK = 1.03            # запас на округления в формулах игры
_pts = {}                   # uid -> очки, купленные паками (None/нет — ещё не загружены: предел по максимуму)


def set_points(uid, n):
    _pts[uid] = max(0, int(n or 0))


def add_pack(uid, pack):
    """Покупка пака: очки навыка из него сразу расширяют предел (иначе честный покупатель упёрся бы в него)."""
    if uid in _pts:
        _pts[uid] += PACK_PTS.get(pack, 0)


def points(lvl, bought):
    """Сколько очков игрок мог вложить в одно улучшение: 3 за уровень + купленные, не больше UP_MAX."""
    if bought is None:
        return UP_MAX
    return max(0, min(UP_MAX, PTS_PER_LEVEL * (max(1, lvl) - 1) + bought))


def caps(lvl, bought=None):
    """Наибольшие честные (прочность, CP, броня) на уровне lvl при bought купленных очках."""
    lvl = max(1, int(lvl or 1))
    L = min(len(HP_AB), lvl)                    # выше потолка уровней игры — как на последнем
    u = points(lvl, bought)
    a, b = HP_AB[L - 1]
    da, db = DEF_AB[L - 1]
    max_hp = int((a + b * u) * CAP_SLACK) + 20
    max_def = int((da + db * u) * CAP_SLACK) + 20
    return max_hp, max_hp * 10 + CP_PER_POINT * u, max_def


def sanitize(info, lvl):
    """Ограничить прочность, CP и броню из pos возможным на уровне игрока. Админов не трогаем."""
    if info.get("admin"):
        return
    bought = _pts.get(info.get("id"))
    max_hp, max_cp, max_def = caps(lvl, bought)
    cp_extra = CP_PER_POINT * points(lvl, bought)
    info["mhp"] = max(1, min(info.get("mhp", 1), max_hp))
    info["mcp"] = max(1, min(info.get("mcp", 1), max_cp, info["mhp"] * 10 + cp_extra))
    info["hp"] = max(0, min(info.get("hp", 0), info["mhp"]))
    info["cp"] = max(0, min(info.get("cp", 0), info["mcp"]))
    if info.get("df") is None:
        info["df_eff"] = max_def                 # старый клиент не присылает броню — считаем по максимуму (урон меньше)
    else:
        info["df_eff"] = max(0, min(int(info["df"]), max_def))


# ---------- бой ----------
def in_combat(uid):
    st = _state.get(uid)
    return bool(st) and _now() - st["t"] < COMBAT_SEC


def combat_left(uid):
    """Сколько секунд игрок ещё «в бою» после последнего удара по нему (0 — не в бою)."""
    st = _state.get(uid)
    return max(0.0, COMBAT_SEC - (_now() - st["t"])) if st else 0.0


def _heal_budget(st, mhp):
    """Запас лечения: копится (20 + 5% прочности) в секунду, не больше 200 + 25% прочности."""
    now = _now()
    cap = 200 + 0.25 * mhp
    st["heal"] = min(cap, st["heal"] + (now - st["heal_t"]) * (20 + 0.05 * mhp))
    st["heal_t"] = now
    return st["heal"]


def on_report(info):
    """Вызывать после разбора pos. В бою отчёт телефона принимается только в пределах правил."""
    uid = info["id"]
    if not in_combat(uid):
        _state.pop(uid, None)
        return
    st = _state[uid]
    mhp = info.get("mhp", 1)
    rep_hp, rep_cp = info.get("hp", 0), info.get("cp", 0)
    if rep_hp <= st["hp"]:
        st["hp"] = rep_hp                        # честный телефон обычно показывает меньше — верим
    else:
        grow = min(rep_hp - st["hp"], _heal_budget(st, mhp))
        st["heal"] -= grow
        st["hp"] += grow
        if rep_hp - st["hp"] > 1:
            metrics.inc("pvpguard.hp_clamped")
    # CP в бою растут только от лечения: в 10 раз больше прироста прочности (баланс 4 сезона)
    healed = max(0.0, st["hp"] - st.get("hp_prev", st["hp"]))
    st["cp"] = min(rep_cp, st["cp"] + healed * 10) if rep_cp > st["cp"] else rep_cp
    st["hp_prev"] = st["hp"]
    st["hp"] = min(st["hp"], mhp)
    info["hp"], info["cp"] = int(st["hp"]), int(st["cp"])


def on_hit(victim, dmg):
    """Сервер вычитает удар из своих CP и прочности жертвы. Возвращает True, если по его расчёту это смерть."""
    uid = victim["id"]
    now = _now()
    st = _state.get(uid)
    if not st or now - st["t"] >= COMBAT_SEC:
        st = _state[uid] = {"hp": float(victim.get("hp", 0)), "cp": float(victim.get("cp", 0)), "t": now,
                            "heal": 150 + 0.1 * victim.get("mhp", 1), "heal_t": now}   # запас на ремкомплект в начале боя
    st["t"] = now
    eff = max(1.0, dmg - victim.get("df_eff", 0) * 0.5)
    absorbed = min(st["cp"], eff)
    st["cp"] -= absorbed
    st["hp"] -= (eff - absorbed) * HP_FACTOR
    victim["cp"], victim["hp"] = int(max(0, st["cp"])), int(max(0, st["hp"]))
    if st["hp"] <= 0:
        metrics.inc("pvpguard.server_death")
        return PVP_MODE == "on"
    return False


def heal(uid, amount, mhp):
    """Подхил от союзника сервер видит сам — сразу добавляем к своей прочности."""
    st = _state.get(uid)
    if st:
        st["hp"] = min(float(mhp), st["hp"] + amount)


def clear(uid):
    _state.pop(uid, None)


# ---------- скорость ----------
def check_move(info, nx, ny, nloc, ndead):
    """True — перемещение правдоподобно. Состояние хранится в info["mv"]."""
    if MOVE_MODE == "off" or info.get("admin"):
        return True
    now = _now()
    mv = info.get("mv")
    # Запас сбрасывается только при смене локации. Раньше его сбрасывала и смена флага dead, который присылает
    # телефон: чередуя dead, можно было телепортироваться в любую точку даже при MOVE_GUARD=on.
    # Честному клиенту это не мешает: погибший робот стоит на месте, а возрождается уже в ангаре (смена локации).
    if not mv or nloc != info.get("loc"):
        info["mv"] = {"tok": MOVE_BURST, "t": now}
        return True
    mv["tok"] = min(MOVE_BURST, mv["tok"] + (now - mv["t"]) * MOVE_RATE)
    mv["t"] = now
    dist = math.hypot(nx - info.get("x", nx), ny - info.get("y", ny))
    if dist <= mv["tok"]:
        mv["tok"] -= dist
        return True
    metrics.inc("moveguard.too_fast")
    if MOVE_MODE != "on":
        mv["tok"] = 0
        return True
    return False


def cleanup():
    now = _now()
    for uid in [u for u, st in _state.items() if now - st["t"] > COMBAT_SEC * 3]:
        _state.pop(uid, None)
