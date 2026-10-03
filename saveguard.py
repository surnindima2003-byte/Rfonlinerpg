"""Проверка сохранений игрока (инвентарь, лом, ядра, заточка).

Сохранение по-прежнему приходит с телефона. Этот модуль — первый шаг к защите от подделки:

1. Структура (жёсткие правила, которые честная игра не нарушает никогда): числа конечные и
   неотрицательные, заточка 0..15, грейд 0..3, сумка не больше 60 ячеек, склад не больше 200.
2. Скачки между двумя сохранениями (мягкие правила): лом и ядра выросли больше, чем можно
   добыть за прошедшее время с учётом выдач сервера; заточка прыгнула на несколько уровней сразу.

Режимы (переменная SAVE_GUARD):
  shadow (по умолчанию) — только записываем подозрения: лог, /metrics, список последних случаев.
  on     — нарушения структуры исправляются перед записью (вещь с заточкой +99 станет +15 и т.п.),
           а сохранение со скачком не принимается: в базе остаётся прошлое.
  off    — проверка выключена.
Рекомендуется несколько дней посмотреть на shadow, прежде чем включать on.
"""
import math
import os
import time
from collections import deque

import metrics

MODE = os.getenv("SAVE_GUARD", "shadow").strip().lower()

ENCH_MAX = 15
GRADE_MAX = 3
INV_MAX, STORE_MAX = 60, 200
NUM_MAX = 2_000_000_000
STACK_MAX = 1_000_000

# мягкие пределы скачков (с запасом: лучше пропустить, чем обидеть честного игрока)
SCRAP_BASE = 20_000          # разовые поступления: продажа вещей торговцу, награды квестов
SCRAP_PER_SEC = 1200         # фарм лома в секунду даже с бонусами VIP (до +230%)
CORES_BASE = 60
CORES_PER_SEC = 2
ENCH_JUMP = 3                # за одно сохранение (раз в ~10 с) больше +3 к заточке не набрать
SPHERES = ("sph_cu", "sph_ti")
SPH_PRICE_MIN = 150          # самая дешёвая сфера у торговца (медная)
SPH_BASE = 40                # сферы «из ниоткуда» за одно сохранение: награда VIP (до 30), задания дня, выпадение
ENCH_SLACK = 3
MATS = ("wire", "plate", "chip")    # материалы крафта: из них сервер собирает руны и дронов, которые продаются за GRAM
MAT_BASE = 60                # разовые поступления материалов за одно сохранение
MAT_PER_SEC = 1.0            # выпадение материалов в секунду с большим запасом

_purchases = {}              # tg_id -> время последней покупки в магазине GRAM
recent = deque(maxlen=200)   # последние подозрительные сохранения (видны в /metrics)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _items(S):
    out = []
    for key in ("inv", "store"):
        lst = S.get(key)
        if isinstance(lst, list):
            out += [x for x in lst if isinstance(x, dict)]
    eq = S.get("eq")
    if isinstance(eq, dict):
        out += [x for x in eq.values() if isinstance(x, dict)]
    return out


def spheres(S):
    """Сколько сфер заточки лежит в сумке и на складе."""
    n = 0
    for key in ("inv", "store"):
        for x in S.get(key) or []:
            if isinstance(x, dict) and x.get("id") in SPHERES and _num(x.get("n", 0)):
                n += max(0, int(x.get("n", 0)))
    return n


def mats(S, mid):
    """Сколько материала лежит в сумке и на складе."""
    n = 0
    for key in ("inv", "store"):
        for x in S.get(key) or []:
            if isinstance(x, dict) and x.get("id") == mid and _num(x.get("n", 0)):
                n += max(0, int(x.get("n", 0)))
    return n


def ench_total(S):
    """Сумма заточки всех вещей: каждая успешная заточка съедает хотя бы одну сферу."""
    return sum(int(x.get("e") or 0) for x in _items(S) if _num(x.get("e") or 0) and 0 <= (x.get("e") or 0) <= 1000)


def max_ench(S):
    return max([int(x.get("e") or 0) for x in _items(S) if _num(x.get("e") or 0)] or [0])


def structure(S):
    """Список нарушений структуры (пустой — всё в порядке)."""
    bad = []
    for k in ("scrap", "cores", "exp"):
        v = S.get(k, 0)
        if v is not None and (not _num(v) or v < 0 or v > NUM_MAX):
            bad.append(f"{k}={v!r}")
    if isinstance(S.get("inv"), list) and len(S["inv"]) > INV_MAX + 5:
        bad.append(f"inv_len={len(S['inv'])}")
    if isinstance(S.get("store"), list) and len(S["store"]) > STORE_MAX + 5:
        bad.append(f"store_len={len(S['store'])}")
    for x in _items(S):
        e, g, n = x.get("e", 0) or 0, x.get("g", 0) or 0, x.get("n", 1)
        if not _num(e) or e < 0 or e > ENCH_MAX:
            bad.append(f"ench {x.get('id')}={e!r}")
        if not _num(g) or g < 0 or g > GRADE_MAX:
            bad.append(f"grade {x.get('id')}={g!r}")
        if n is not None and (not _num(n) or n < 0 or n > STACK_MAX):
            bad.append(f"n {x.get('id')}={n!r}")
    return bad


def fix_structure(S):
    """Привести значения в допустимые рамки (режим on)."""
    for k in ("scrap", "cores", "exp"):
        v = S.get(k, 0)
        if v is not None and (not _num(v) or v < 0 or v > NUM_MAX):
            S[k] = 0 if not _num(v) or v < 0 else NUM_MAX
    for key, lim in (("inv", INV_MAX), ("store", STORE_MAX)):
        if isinstance(S.get(key), list) and len(S[key]) > lim:
            S[key] = S[key][:lim]
    for x in _items(S):
        for k, hi in (("e", ENCH_MAX), ("g", GRADE_MAX), ("n", STACK_MAX)):
            if k in x:
                v = x[k]
                x[k] = 0 if not _num(v) or v < 0 else min(int(v), hi)


def jumps(old, new, dt, granted):
    """Подозрительные скачки между прошлым и новым сохранением. granted — выдачи сервера за это время."""
    out = []
    dt = max(1.0, min(dt, 3600.0))
    for k, base, rate in (("scrap", SCRAP_BASE, SCRAP_PER_SEC), ("cores", CORES_BASE, CORES_PER_SEC)):
        a, b = old.get(k, 0), new.get(k, 0)
        if _num(a) and _num(b):
            limit = base + rate * dt + granted.get(k, 0)
            if b - a > limit:
                out.append(f"{k} +{int(b - a)} за {int(dt)} с (предел {int(limit)})")
    for mid in MATS:
        limit = MAT_BASE + MAT_PER_SEC * dt
        d = mats(new, mid) - mats(old, mid)
        if d > limit:
            out.append(f"{mid} +{d} за {int(dt)} с (предел {int(limit)})")
    d_e = max_ench(new) - max_ench(old)
    if d_e > ENCH_JUMP:
        out.append(f"заточка +{d_e} за {int(dt)} с")
    # сферы: больше, чем можно купить на весь доступный лом, получить наградами и выбить
    a, b = old.get("scrap", 0), new.get("scrap", 0)
    scrap_room = 0
    if _num(a) and _num(b):
        scrap_room = max(0, a - b + SCRAP_BASE + SCRAP_PER_SEC * dt + granted.get("scrap", 0))
    sph_gain = SPH_BASE + granted.get("sph", 0) + int(scrap_room // SPH_PRICE_MIN)
    s_old, s_new = spheres(old), spheres(new)
    if s_new - s_old > sph_gain:
        out.append(f"сферы +{s_new - s_old} за {int(dt)} с (предел {sph_gain})")
    # заточка без сфер: успешных заточек не может быть больше, чем потрачено сфер
    spent_max = max(0, s_old + sph_gain - s_new)
    e_gain = ench_total(new) - ench_total(old)
    if e_gain > spent_max + ENCH_SLACK:
        out.append(f"заточка +{e_gain} при потраченных сферах не больше {spent_max}")
    return out


def note_purchase(uid):
    """Покупка пака: в ближайшем сохранении прирост лома, вещей и заточки законный."""
    _purchases[uid] = time.time()


def check(uid, nick, old_data, new_data, dt, granted):
    """Возвращает (данные для записи или None — не записывать, список замечаний)."""
    if MODE == "off":
        return new_data, []
    S = new_data.get("S") if isinstance(new_data.get("S"), dict) else {}
    oldS = old_data.get("S") if isinstance(old_data, dict) and isinstance(old_data.get("S"), dict) else None
    notes = [("структура", b) for b in structure(S)]
    bought = _purchases.get(uid, 0) >= time.time() - dt - 5
    if oldS is not None and not bought:
        notes += [("скачок", j) for j in jumps(oldS, S, dt, granted)]
    if bought and oldS is not None:
        _purchases.pop(uid, None)
    if not notes:
        return new_data, []
    metrics.inc("saveguard.flagged")
    recent.append({"ts": int(time.time()), "uid": uid, "nick": nick, "mode": MODE, "notes": [f"{a}: {b}" for a, b in notes][:6]})
    metrics.gauge("saveguard.recent", list(recent)[-20:])
    if MODE != "on":
        return new_data, notes
    if any(a == "скачок" for a, _ in notes):
        metrics.inc("saveguard.rejected")
        return None, notes
    fix_structure(S)
    metrics.inc("saveguard.fixed")
    return new_data, notes
