"""Серверный учёт ресурсов: сколько лома, ядер и материалов игрок МОГ получить с прошлого сохранения.

Раньше проверка сохранений разрешала прирост «не больше 1200 лома в секунду» — щедрый потолок, под которым
подделка проходила спокойно. Здесь приход считается по событиям, которые сервер подтвердил сам:

  * убийства мобов (items.process_kills) — дроп по тем же формулам, что в game.html (mobDrops/rollDrops),
    с наибольшими множителями: лом — максимум диапазона × (1 + потолок бонусов снаряжения + VIP игрока),
    материалы и ядра — шанс × (1 + бонусы к дропу) × запас LEDGER_K на везение;
  * продажа и разрушение учтённого снаряжения (items: op «gone», неудачная заточка) — цена как sellPrice.

Расход (зелья, ремонт, улучшения) не нужен: он только уменьшает ресурсы. Приход копится до принятого
сохранения и обнуляется. Режим RES_LEDGER: off | shadow (только наблюдение — пишет в «Статистику», что срезал бы)
| on (шаг 3: заменит потолок по времени). Модуль без базы и сети — его проверяют тесты (test_ledger.py).
"""
import os
import time
from collections import deque

import metrics

MODE = os.getenv("RES_LEDGER", "shadow").strip().lower()
if MODE not in ("off", "shadow", "on"):
    MODE = "shadow"

LEDGER_K = float(os.getenv("LEDGER_K", "3"))          # запас на везение для редкого дропа (материалы, ядра)
LOOT_MAX = 2.0         # потолок бонуса к лому от снаряжения: «Мародёр», руны удачи, дроны, артефакты (st.loot в игре)
DROP_UP_MAX = 0.3      # улучшение «Бонус к дропу»: 300 ур. × 0,1%
SLACK = {"scrap": 3000, "cores": 3, "wire": 3, "plate": 3, "chip": 3}   # постоянный запас на неточности за сохранение
MATS = ("wire", "plate", "chip")

# ---- зеркало game.html (совпадение проверяет test_ledger.py) ----
SCRAP_CHANCE = 0.30
BOSS_LOOT = 150
NPCS = {   # id: (lvl, [лом мин, макс], {материал: вес})
    "scrap_crawler": (1, (2, 5), {"wire": .4}),
    "rogue_drone": (2, (3, 7), {"wire": .3, "chip": .15}),
    "sentry_bot": (4, (5, 12), {"chip": .3, "plate": .3}),
    "war_walker": (7, (10, 20), {"plate": .5, "chip": .3}),
}
DG_BASE = ("scrap_crawler", "rogue_drone", "sentry_bot", "war_walker")
LOC_MIN = {"scrapfields": 1, "reactor_ruins": 3, "iron_canyon": 6}
LOC_CORES = {"reactor_ruins", "iron_canyon"}   # локации с флагом cores в LOCS
TIER_LVL = {1: 1, 2: 5, 3: 10, 4: 15, 5: 20, 6: 25, 7: 30, 8: 35, 9: 40, 10: 45}
GRADE_SELL = (1, 2, 4, 10)


def mob_info(mob):
    """(уровень, (лом мин, макс), веса материалов, главарь) как NPCS в игре, или None."""
    if mob in NPCS:
        lvl, scrap, loot = NPCS[mob]
        return lvl, scrap, loot, False
    for pre in ("dg", "db"):
        if mob.startswith(pre) and mob[2:].isdigit():
            L = int(mob[2:])
            if not 1 <= L <= 40:
                return None
            base = DG_BASE[(L - 1) % 4]
            loot = {k: round(v * 0.6, 3) for k, v in NPCS[base][2].items()}
            scrap = (L + 1, 2 * L + 4)
            if pre == "db":
                scrap = (scrap[0] * 10, scrap[1] * 10)
            return L, scrap, loot, pre == "db"
    return None


def kill_income(mob, loc, vip_scrap=0.0, drop_bonus=0.0):
    """Наибольший разумный приход за одно подтверждённое убийство: {"scrap", "cores", материалы} (дробные).

    vip_scrap — бонус VIP к лому (сервер знает уровень VIP); drop_bonus — бонусы к дропу, которые знает сервер
    (VIP, билет сезона, сезонная зона). Бонусы снаряжения сервер не видит — берётся их потолок."""
    info = mob_info(mob)
    if not info:
        return {}
    lv, scrap, loot, boss = info
    if mob in NPCS:
        lv = lv or LOC_MIN.get(loc, 1)
    s = lv - 1
    out = {}
    chance = 1.0 if boss else SCRAP_CHANCE
    out["scrap"] = chance * scrap[1] * (1 + LOOT_MAX + vip_scrap)
    dm = 1 + drop_bonus + DROP_UP_MAX
    base = (0.13 + 0.0079 * s) / 100
    for mid, c in loot.items():
        ch = base * max(0.3, min(1.0, c / 0.4))
        if boss:
            ch = min(0.9, ch * BOSS_LOOT)
        out[mid] = out.get(mid, 0) + min(1.0, ch * dm) * LEDGER_K
    if lv >= 3 or loc in LOC_CORES:
        ch = (0.02 + 0.001 * s) / 100
        if boss:
            ch = min(0.9, ch * BOSS_LOOT)
        out["cores"] = min(1.0, ch * dm) * LEDGER_K
    return out


def tier_k(lvl):
    return 1 + 0.12 * lvl + 0.0022 * lvl * lvl


def gear_sell(item_id, g=0, e=0):
    """Цена продажи учтённой вещи торговцу (sellPrice в игре) или 0, если это не снаряжение."""
    parts = str(item_id).split("_")
    if len(parts) != 3 or parts[0] != "g" or not parts[2].isdigit():
        return 0
    t = int(parts[2])
    if t not in TIER_LVL:
        return 0
    sell = round(6 * tier_k(TIER_LVL[t]) ** 2.2)
    g = max(0, min(3, int(g or 0)))
    return round(sell * GRADE_SELL[g] * (1 + 0.5 * max(0, int(e or 0))))


# ---- приход по игрокам (в памяти; после перезапуска первое сохранение сверяется по-старому) ----
_acc = {}                    # uid -> {"t": начало, ресурс: приход}
recent = deque(maxlen=200)   # что новая проверка срезала бы (видно в «Статистике»)


def add(uid, income):
    if MODE == "off" or not income:
        return
    a = _acc.setdefault(uid, {"t": time.time()})
    for k, v in income.items():
        if v:
            a[k] = a.get(k, 0.0) + v


def peek(uid):
    return dict(_acc.get(uid) or {})


def reset(uid):
    """Сохранение принято — приход, учтённый при сверке (check), списывается. Пришедшее после сверки
    (убийства идут в фоне, пока сохранение пишется в базу) остаётся до следующего сохранения."""
    a = _acc.get(uid)
    snap = a.pop("_snap", None) if a else None
    if not a or snap is None:
        _acc[uid] = {"t": time.time()}
        return
    for k, v in snap.items():
        a[k] = max(0.0, a.get(k, 0.0) - v)
    a["t"] = time.time()


def _mats(S, mid):
    n = 0
    for bag in ("inv", "store"):
        for it in S.get(bag) or []:
            if isinstance(it, dict) and it.get("id") == mid:
                try:
                    n += int(it.get("n", 1) or 0)
                except (TypeError, ValueError):
                    pass
    return n


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check(uid, nick, oldS, newS, granted):
    """Сравнить прирост с приходом. Возвращает список замечаний [(ресурс, прирост, допустимо)].
    В shadow ничего не меняет — только запоминает (для «Статистики») и считает метрики."""
    if MODE == "off" or not isinstance(oldS, dict) or not isinstance(newS, dict):
        return []
    a = _acc.get(uid)
    if a is None:
        return []                                   # после перезапуска прихода нет — сверяет старая проверка
    a["_snap"] = {k: v for k, v in a.items() if k not in ("t", "_snap")}
    out = []
    for k in ("scrap", "cores") + MATS:
        if k in MATS:
            gain = _mats(newS, k) - _mats(oldS, k)
        else:
            o, n = oldS.get(k, 0), newS.get(k, 0)
            if not (_num(o) and _num(n)):
                continue
            gain = n - o
        allowed = a.get(k, 0.0) + granted.get(k, 0) + SLACK[k]
        if gain > allowed:
            out.append((k, int(gain), int(allowed)))
    metrics.inc("ledger.checked")
    if out:
        metrics.inc("ledger.over")
        recent.append({"ts": int(time.time()), "uid": uid, "nick": nick, "mode": MODE,
                       "notes": [f"{k} +{g} при приходе {al}" for k, g, al in out],
                       "secs": int(time.time() - a.get("t", time.time()))})
    return out


def cleanup(online_ids):
    """Раз в минуту: приход ушедших игроков старше часа не нужен (их следующее сохранение сверит старая проверка)."""
    now = time.time()
    for uid in [u for u, a in _acc.items() if u not in online_ids and now - a.get("t", now) > 3600]:
        _acc.pop(uid, None)
