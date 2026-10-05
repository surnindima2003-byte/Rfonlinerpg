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
import asyncio
import math
import os
import time
import weakref
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

BASE_REFILL_SEC = 300        # разовый запас (SCRAP_BASE и т.п.) восстанавливается полностью за 5 минут

# Что даёт пак магазина (как PACKS в game.html; совпадение проверяет test_saveguard_packs.py).
# После покупки этот прирост в ближайшем сохранении законный — но только он, а не «что угодно».
# ench — в паке вещи с заточкой: проверку скачка заточки для этого сохранения не делаем.
PACK_GRANTS = {
    "p_start": {"scrap": 10_000},
    "p_base": {"scrap": 25_000, "ench": True},
    "p_std": {"scrap": 70_000, "cores": 50, "ench": True},
    "p_elite": {"scrap": 500_000, "cores": 400, "sph": 5, "ench": True},
    "p_legend": {"scrap": 1_000_000, "cores": 1000, "sph": 15, "ench": True},
    "p_epic": {"scrap": 3_000_000, "cores": 2000, "sph": 130, "ench": True},
    "p_cores": {"cores": 700},
    "d_start": {"ench": True}, "d_base": {"ench": True}, "d_adv": {"ench": True},
    "d_sup": {"ench": True}, "d_top": {"ench": True}, "d_admin": {"ench": True},
    "x_books": {"ench": True},
    "x_pots": {"scrap": 20_000},
    "u1": {"sph": 60}, "u2": {"sph": 120}, "u3": {"sph": 245},
}
PURCHASE_TTL = 600           # покупка учитывается в сохранениях следующие 10 минут

_purchases = {}              # tg_id -> {"scrap", "cores", "sph", "ench", "t"} — ещё не учтённые покупки
_slack = {}                  # tg_id -> {"t", ресурс: остаток разового запаса}
recent = deque(maxlen=200)   # последние подозрительные сохранения (видны в /metrics)

# Сохранение и серверный крафт одного игрока идут строго по очереди (сервер — один процесс):
# иначе два крафта в одну секунду дают две вещи за одни ресурсы, а сохранение, начатое до крафта,
# перезаписывает его списание.
_locks = weakref.WeakValueDictionary()


def player_lock(uid):
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


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


def _base_caps():
    caps = {"scrap": SCRAP_BASE, "cores": CORES_BASE, "sph": SPH_BASE}
    caps.update({m: MAT_BASE for m in MATS})
    return caps


def slack_of(uid, now=None):
    """Сколько разового запаса осталось у игрока (копия; записать — commit_slack).

    Раньше SCRAP_BASE/MAT_BASE/SPH_BASE давались на КАЖДОЕ сохранение, а частота сохранений не ограничена:
    сохраняясь раз в секунду, можно было «напечатать» по 20 000 лома, 60 материалов и 40 сфер в секунду.
    Теперь это общий запас, который тратится на необъяснимый прирост и восстанавливается за BASE_REFILL_SEC."""
    now = now or time.time()
    caps = _base_caps()
    st = _slack.get(uid)
    if not st:
        return dict(caps, t=now)
    k = min(1.0, max(0.0, now - st["t"]) / BASE_REFILL_SEC)
    out = {r: min(cap, st.get(r, cap) + cap * k) for r, cap in caps.items()}
    out["t"] = now
    return out


def commit_slack(uid, slack):
    _slack[uid] = slack


def jumps(old, new, dt, granted, slack=None, skip_ench=False, caps_out=None):
    """Подозрительные скачки между прошлым и новым сохранением. granted — выдачи сервера за это время.

    slack — остаток разового запаса (slack_of); тратится на месте. None — полный запас (как одно сохранение).
    caps_out — словарь: для каждого превышения ключ ресурса → наибольшее допустимое значение (для лома и ядер
    по нему сервер может «срезать» лишнее, а не отклонять всё сохранение)."""
    out = []
    caps_out = {} if caps_out is None else caps_out
    dt = max(1.0, min(dt, 3600.0))
    if slack is None:
        slack = _base_caps()

    def over(key, gain, allowed):
        """Прирост сверх объяснимого берётся из запаса. True — запаса не хватило."""
        extra = gain - allowed
        if extra <= 0:
            return False
        if extra > slack.get(key, 0):
            return True
        slack[key] = slack.get(key, 0) - extra
        return False

    scrap_slack0 = slack.get("scrap", 0)
    for k, rate in (("scrap", SCRAP_PER_SEC), ("cores", CORES_PER_SEC)):
        a, b = old.get(k, 0), new.get(k, 0)
        if _num(a) and _num(b):
            allowed = rate * dt + granted.get(k, 0)
            if over(k, b - a, allowed):
                caps_out[k] = int(a + allowed + slack.get(k, 0))
                out.append(f"{k} +{int(b - a)} за {int(dt)} с (предел {int(allowed + slack.get(k, 0))})")
    for mid in MATS:
        allowed = MAT_PER_SEC * dt
        d = mats(new, mid) - mats(old, mid)
        if over(mid, d, allowed):
            caps_out[mid] = None
            out.append(f"{mid} +{d} за {int(dt)} с (предел {int(allowed + slack.get(mid, 0))})")
    if not skip_ench:
        d_e = max_ench(new) - max_ench(old)
        if d_e > ENCH_JUMP:
            caps_out["ench"] = None
            out.append(f"заточка +{d_e} за {int(dt)} с")
    # сферы: больше, чем можно купить на весь доступный лом, получить наградами и выбить
    a, b = old.get("scrap", 0), new.get("scrap", 0)
    scrap_room = 0
    if _num(a) and _num(b):
        scrap_room = max(0, a - b + scrap_slack0 + SCRAP_PER_SEC * dt + granted.get("scrap", 0))
    sph_allowed = granted.get("sph", 0) + int(scrap_room // SPH_PRICE_MIN)
    s_old, s_new = spheres(old), spheres(new)
    sph_slack0 = slack.get("sph", 0)
    if over("sph", s_new - s_old, sph_allowed):
        caps_out["sph"] = None
        out.append(f"сферы +{s_new - s_old} за {int(dt)} с (предел {int(sph_allowed + slack.get('sph', 0))})")
    # заточка без сфер: успешных заточек не может быть больше, чем потрачено сфер
    if not skip_ench:
        spent_max = max(0, s_old + sph_allowed + sph_slack0 - s_new)
        e_gain = ench_total(new) - ench_total(old)
        if e_gain > spent_max + ENCH_SLACK:
            caps_out["ench"] = None
            out.append(f"заточка +{e_gain} при потраченных сферах не больше {spent_max}")
    return out


def note_purchase(uid, pack=None):
    """Покупка пака: в ближайших сохранениях законен прирост ровно того, что в паке."""
    g = PACK_GRANTS.get(pack, {})
    p = _purchases.get(uid)
    if not p or time.time() - p["t"] > PURCHASE_TTL:
        p = _purchases[uid] = {"scrap": 0, "cores": 0, "sph": 0, "ench": False, "t": 0}
    for k in ("scrap", "cores", "sph"):
        p[k] += g.get(k, 0)
    p["ench"] = p["ench"] or bool(g.get("ench"))
    p["t"] = time.time()


CLAMPABLE = ("scrap", "cores")       # эти скачки сервер исправляет (срезает лишнее), остальные — отклоняет


def check(uid, nick, old_data, new_data, dt, granted, fix_out=None):
    """Возвращает (данные для записи или None — не записывать, список замечаний).

    fix_out — словарь: сюда попадают значения, которые сервер исправил в сохранении (например {"scrap": 120000}),
    чтобы телефон применил их у себя. Раньше любой скачок отклонял сохранение целиком: честный игрок с лишним
    ломом (продажа вещей, награда) терял на сервере и весь остальной прогресс."""
    if MODE == "off":
        return new_data, []
    S = new_data.get("S") if isinstance(new_data.get("S"), dict) else {}
    oldS = old_data.get("S") if isinstance(old_data, dict) and isinstance(old_data.get("S"), dict) else None
    notes = [("структура", b) for b in structure(S)]
    bought = _purchases.get(uid)
    if bought and time.time() - bought["t"] > PURCHASE_TTL:
        _purchases.pop(uid, None)
        bought = None
    slack = slack_of(uid)
    if oldS is not None:
        g = dict(granted)
        if bought:
            for k in ("scrap", "cores", "sph"):
                g[k] = g.get(k, 0) + bought[k]
        caps = {}
        notes += [("скачок", j) for j in jumps(oldS, S, dt, g, slack, skip_ench=bool(bought and bought["ench"]), caps_out=caps)]
        if MODE == "on" and caps and all(k in CLAMPABLE and v is not None for k, v in caps.items()):
            # только лом и ядра: срезаем лишнее до допустимого, остальное сохранение принимаем
            for k, v in caps.items():
                S[k] = max(0, min(int(S.get(k, 0)), v))
                slack[k] = 0
                if fix_out is not None:
                    fix_out[k] = S[k]
            notes = [("исправлено" if a == "скачок" else a, b) for a, b in notes]
    rejected = MODE == "on" and any(a == "скачок" for a, _ in notes)
    if not rejected:
        commit_slack(uid, slack)                     # запас тратится, только если сохранение принято
        if bought and oldS is not None:
            _purchases.pop(uid, None)                # покупка учтена в принятом сохранении
    if not notes:
        return new_data, []
    metrics.inc("saveguard.flagged")
    recent.append({"ts": int(time.time()), "uid": uid, "nick": nick, "mode": MODE, "notes": [f"{a}: {b}" for a, b in notes][:6]})
    metrics.gauge("saveguard.recent", list(recent)[-20:])
    if MODE != "on":
        return new_data, notes
    if rejected:
        metrics.inc("saveguard.rejected")
        return None, notes
    fix_structure(S)
    metrics.inc("saveguard.fixed")
    if fix_out:
        metrics.inc("saveguard.clamped")
    return new_data, notes


def cleanup(online_ids):
    """Раз в минуту: забываем запас и покупки ушедших игроков (запас у них всё равно восстановился бы полностью)."""
    now = time.time()
    for uid in [u for u, st in _slack.items() if u not in online_ids and now - st["t"] > BASE_REFILL_SEC]:
        _slack.pop(uid, None)
    for uid in [u for u, p in _purchases.items() if now - p["t"] > PURCHASE_TTL]:
        _purchases.pop(uid, None)
