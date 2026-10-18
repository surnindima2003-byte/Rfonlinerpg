"""Босс трона в Мире — «Железный Наместник» (как Владыка Мира в LIBERTY, патч 17 октября).

Стоит на каменной площади за кольцом колонн в восточном конце Мира, 60 уровень. Это общий моб (mobworld):
прочность считает сервер, ведёт босса тот, за кем он гонится. Отличия от обычных монстров — правило типа wt:

* после гибели возвращается через 1–2 часа (случайно); время возвращения хранится в базе (Meta), поэтому
  перезапуск сервера босса раньше срока не возвращает;
* добычу и опыт получает не только нанёсший больше всех урона, а каждый, кто снёс хотя бы SHARE_MIN его
  прочности (до SHARE_MAX пилотов): каждому сервер засчитывает бой, и обычная заявка на убийство проходит;
* восстанавливается не через 12 секунд без ударов, а через REGEN_AFTER — успеть вернуться после гибели.

Сколько осталось до возвращения, видят все: {"t":"wt","until":мс или 0} — при входе в игру, при гибели босса
и когда он вернулся. Номер босса в раскладке Мира — IDX (последний после монстров спотов, как в игре).
"""
import asyncio
import json
import random
import time

import mobguard
import mobworld

LOC = mobguard.WORLD_LOC
TYPE, LVL, HP = mobguard.THRONE_BOSS, mobguard.THRONE_LVL, mobguard.THRONE_HP
IDX = sum(n for _, n, _, _ in mobguard.WORLD_SPOTS)   # номер в раскладке Мира: 168 монстров спотов, босс — следующий (test_world_boss сверяет с игрой)
NAME = "Железный Наместник"
RESPAWN = (3600, 7200)           # 1–2 часа
REGEN_AFTER = 90
SHARE_MIN = 0.01                 # снёс 1% прочности (5 000) — получаешь свою добычу и опыт
SHARE_MAX = 30
META_KEY = "throne_boss"

st = {"until": 0.0, "told": True}
persist = None                   # async def (value) — запись в базу; подставляет webserver


def alive(now=None):
    return (now or time.time()) >= st["until"]


def view():
    return {"t": "wt", "until": 0 if alive() else int(st["until"] * 1000)}


class Rule:
    """Правило mobworld для типа wt (см. mobworld.set_type_rule)."""
    regen_after = REGEN_AFTER

    def alive(self, loc, i):
        return loc == LOC and alive()

    def killed(self, loc, i, m, winner, now, hub):
        until = now + random.uniform(*RESPAWN)
        st.update(until=until, told=False)
        need = m["max"] * SHARE_MIN
        share = sorted((u for u, d in m["dmg"].items() if d >= need), key=lambda u: -m["dmg"][u])[:SHARE_MAX]
        if winner not in share:
            share.insert(0, winner)
        _save(until, i)
        hub.to_all(view())                                        # таймер возвращения — всем (карта, портал)
        hub.to_loc(LOC, {"t": "pinfo", "text": killed_text(len(share), until, now)})
        return until, {"share": share, "back": round(until - now)}, share


def _save(until, i):
    if persist is None:
        return
    try:
        asyncio.get_running_loop().create_task(persist(json.dumps({"until": round(until, 1), "i": i})))
    except RuntimeError:
        pass                     # вне цикла событий (проверки) — сохранять некуда


def restore(value, now=None):
    """После перезапуска: босс, убитый до него, остаётся мёртвым до своего срока."""
    now = now or time.time()
    try:
        d = json.loads(value or "{}")
        until, i = float(d.get("until", 0)), int(d.get("i", IDX))
    except (TypeError, ValueError):
        return
    if until <= now or i != IDX:
        return
    st.update(until=until, told=False)
    mobworld._mobs.setdefault(LOC, {})[IDX] = {"type": TYPE, "hp": HP, "max": HP, "dmg": {}, "dead_until": until, "last": 0}


def killed_text(n, until, now=None):
    left = max(0, int(until - (now or time.time())))
    return f"🏆 {NAME} повержен! Добычу получили пилотов: {n}. Вернётся через {left // 3600} ч {left % 3600 // 60} мин"


def tick(hub):
    """Раз в тик мира: босс вернулся — сказать всем (таймер) и тем, кто в Мире."""
    if not st["told"] and alive():
        st["told"] = True
        hub.to_all(view())
        hub.to_loc(LOC, {"t": "pinfo", "text": f"⚔ {NAME} вернулся на Трон Владыки"})


def respawn_now(hub):
    """Админ: вернуть босса сразу (проверка на сервере)."""
    st.update(until=0.0, told=True)
    m = mobworld._mobs.get(LOC, {}).pop(IDX, None)
    if m and m.get("dead_until"):
        hub.to_loc(LOC, {"t": "mres", "loc": LOC, "i": IDX})
    hub.to_all(view())
    _save(0, IDX)


mobworld.set_type_rule(TYPE[:2], Rule())
