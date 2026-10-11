"""Эффекты умений для соседей (сообщение sfx).

Раньше другие пилоты видели только удары в PvP (pfx). Всё остальное, что рисует умение, — кольца АоЕ,
разрезы, следы рывков, лучи очереди и цепи, цифры урона и лечения, ауры баффов, поле нанитов, руна,
глиф притяжения, эхо-двойник — существовало только на экране того, кто его применил.

Теперь телефон заклинателя присылает нарисованное одним коротким пакетом, сервер проверяет его здесь
и рассылает игрокам рядом (webserver.broadcast_skill_fx). Эффект — только картинка: на бой, урон и
награды он не влияет, поэтому сервер не пересчитывает умение, а лишь не даёт этим каналом злоупотреблять:
  * ограничены размеры списков и чисел, цвета — только #RRGGBB;
  * все точки эффекта — не дальше REACH от заклинателя (нельзя рисовать по всей карте);
  * надписи — только цифры урона/лечения и несколько слов из самих умений (не чат в обход мута);
  * не чаще RATE пакетов в секунду с небольшим запасом BURST.

Формат пакета (координаты — пиксели мира, life и dur — секунды):
  f: кольца ["r", x, y, радиус, life, цвет], разрезы ["s", x, y, угол, life, цвет],
     следы рывков ["t", x1, y1, x2, y2, life, цвет]
  b: лучи [x1, y1, x2, y2, life, плазма 0/1, цвет]
  x: надписи [x, y, текст, цвет]
  g: долгие эффекты на земле ["echo", x, y, dur, aim], ["rune", x, y, dur], ["grav", x, y, радиус, dur],
     ["field", x, y, радиус, dur] и аура баффа на пилоте ["buff", id, вид, dur]
  s: id умения — соседи пишут его название над заклинателем и показывают замах
"""
import math
import re

RADIUS2 = 1100 ** 2      # кому рассылать — как удары PvP (PVP_FX_RADIUS2): дальше эффект всё равно за экраном
REACH = 1400             # точки эффекта не дальше этого от последней позиции заклинателя (рывки, цепь, поле)
RATE, BURST = 12.0, 6.0  # пакетов в секунду и запас (очередь «Снайпера» — 3 пакета за 0,36 с)
MAX_F, MAX_B, MAX_X, MAX_G = 16, 16, 10, 4
MAX_LIFE, MAX_DUR, MAX_R = 2.0, 15.0, 600.0
WORDS = {"ИЗ ТЕНИ", "Щит-контур", "оглушён", "−20% защиты"}
NUM = re.compile(r"(КРИТ )?[-+]?\d{1,7}")
COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
SKILL = re.compile(r"[a-z][a-z0-9_]{0,23}")
BUFF = re.compile(r"[a-z]{2,10}")
DEFAULT_COLOR = "#5FD4E8"


def _num(v, lo, hi):
    """Конечное число в пределах [lo, hi] или None (строки, bool, NaN не принимаем)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if not math.isfinite(v):
        return None
    return max(lo, min(hi, v))


def _color(v):
    return v if isinstance(v, str) and COLOR.fullmatch(v) else DEFAULT_COLOR


class _Area:
    """Проверка «точка рядом с заклинателем» и округление координат."""

    def __init__(self, info):
        self.x = float(info.get("x", 0) or 0)
        self.y = float(info.get("y", 0) or 0)

    def pt(self, xv, yv):
        x, y = _num(xv, -500, 10000), _num(yv, -500, 10000)
        if x is None or y is None or (x - self.x) ** 2 + (y - self.y) ** 2 > REACH ** 2:
            return None
        return round(x), round(y)


def _fx(e, area):
    if not isinstance(e, list) or not e:
        return None
    kind = e[0]
    if kind == "r" and len(e) == 6:
        pt, r, life = area.pt(e[1], e[2]), _num(e[3], 1, MAX_R), _num(e[4], 0.05, MAX_LIFE)
        if pt and r is not None and life is not None:
            return ["r", *pt, round(r), round(life, 2), _color(e[5])]
    elif kind == "s" and len(e) == 6:
        pt, a, life = area.pt(e[1], e[2]), _num(e[3], -10, 10), _num(e[4], 0.05, MAX_LIFE)
        if pt and a is not None and life is not None:
            return ["s", *pt, round(a, 2), round(life, 2), _color(e[5])]
    elif kind == "t" and len(e) == 7:
        a, b, life = area.pt(e[1], e[2]), area.pt(e[3], e[4]), _num(e[5], 0.05, MAX_LIFE)
        if a and b and life is not None:
            return ["t", *a, *b, round(life, 2), _color(e[6])]
    return None


def _beam(e, area):
    if not isinstance(e, list) or len(e) != 7:
        return None
    a, b, life = area.pt(e[0], e[1]), area.pt(e[2], e[3]), _num(e[4], 0.05, MAX_LIFE)
    if not (a and b) or life is None:
        return None
    return [*a, *b, round(life, 2), 1 if e[5] in (1, True) else 0, _color(e[6])]


def _text(e, area):
    if not isinstance(e, list) or len(e) != 4 or not isinstance(e[2], str):
        return None
    pt, s = area.pt(e[0], e[1]), e[2][:16]
    if not pt or not (NUM.fullmatch(s) or s in WORDS):
        return None
    return [*pt, s, _color(e[3])]


def _ground(e, area):
    if not isinstance(e, list) or not e:
        return None
    kind = e[0]
    if kind == "buff" and len(e) == 4:
        uid, dur = e[1], _num(e[3], 0.1, MAX_DUR)
        if isinstance(uid, int) and not isinstance(uid, bool) and 0 < uid < 2 ** 53 and isinstance(e[2], str) \
                and BUFF.fullmatch(e[2]) and dur is not None:
            return ["buff", uid, e[2], round(dur, 1)]
    elif kind == "echo" and len(e) == 5:
        pt, dur, aim = area.pt(e[1], e[2]), _num(e[3], 0.1, MAX_DUR), _num(e[4], -10, 10)
        if pt and dur is not None and aim is not None:
            return ["echo", *pt, round(dur, 1), round(aim, 2)]
    elif kind == "rune" and len(e) == 4:
        pt, dur = area.pt(e[1], e[2]), _num(e[3], 0.1, MAX_DUR)
        if pt and dur is not None:
            return ["rune", *pt, round(dur, 1)]
    elif kind in ("grav", "field") and len(e) == 5:
        pt, r, dur = area.pt(e[1], e[2]), _num(e[3], 1, MAX_R), _num(e[4], 0.1, MAX_DUR)
        if pt and r is not None and dur is not None:
            return [kind, *pt, round(r), round(dur, 1)]
    return None


def _rate_ok(info, now):
    tokens = min(BURST, info.get("sfx_tok", BURST) + (now - info.get("sfx_t", now)) * RATE)
    info["sfx_t"] = now
    if tokens < 1:
        info["sfx_tok"] = tokens
        return False
    info["sfx_tok"] = tokens - 1
    return True


def clean(d, info, now):
    """Проверенный пакет для рассылки соседям или None (мусор, пусто, слишком часто)."""
    if info.get("dead"):
        return None
    if not _rate_ok(info, now):
        return None
    area = _Area(info)
    out = {"t": "sfx", "from": info["id"], "loc": info.get("loc", "")}
    for key, fn, cap in (("f", _fx, MAX_F), ("b", _beam, MAX_B), ("x", _text, MAX_X), ("g", _ground, MAX_G)):
        src = d.get(key)
        if isinstance(src, list):
            got = [v for v in (fn(e, area) for e in src[:cap]) if v]
            if got:
                out[key] = got
    sid = d.get("s")
    if isinstance(sid, str) and SKILL.fullmatch(sid):
        out["s"] = sid
    return out if len(out) > 3 else None
