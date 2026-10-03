"""Защита PvP и передвижения: сервер сам ведёт CP и прочность игроков в бою и проверяет скорость.

Раньше прочность в PvP считал только телефон жертвы: изменённый клиент мог просто не отнимать урон
(«бессмертие») и никогда не присылать pvp_dead. Теперь:

* Пределы. Прочность, CP и броня из сообщения pos ограничены сверху тем, что вообще возможно на
  подтверждённом сервером уровне (через предел боевой мощи progress.bm_cap: БМ = броня×10 + прочность×1,5 + …).
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
def caps(bm_cap):
    """Максимально возможные прочность и броня при данном пределе боевой мощи."""
    max_hp = max(100, int(bm_cap / 1.5))
    return max_hp, max_hp * 10, max(10, int(bm_cap / 10))


def sanitize(info, bm_cap):
    """Ограничить прочность, CP и броню из pos возможным на уровне игрока. Админов не трогаем."""
    if info.get("admin"):
        return
    max_hp, max_cp, max_def = caps(bm_cap)
    info["mhp"] = max(1, min(info.get("mhp", 1), max_hp))
    info["mcp"] = max(1, min(info.get("mcp", 1), max_cp, info["mhp"] * 10))
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
    if (not mv or nloc != info.get("loc") or bool(ndead) != bool(info.get("dead"))):
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
