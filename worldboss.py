"""Мировой босс «Владыка Ржавчины» (как мировой босс в Liberty, в нашем стиле).

Расписание: понедельник, среда, пятница, воскресенье — в 20:00 по Москве, держится до 30 минут.
Появляется в Центральном ангаре (безопасная зона) — драться может каждый. Прочность общая для всех
и считается только на сервере. Босс бьёт по площади: место удара заранее подсвечивается красным кругом.
Когда босс повержен, добыча падает на пол для всех: кто первый подобрал, того и предмет.

  телефон → сервер: {"t":"wbhit","dmg"}, {"t":"wbpick","id"}
  сервер → все:     {"t":"wb", ...состояние}, {"t":"wbaoe","x","y","r","in"}, {"t":"wbloot","items"}, {"t":"wbrm","id"}
  сервер → игроку:  {"t":"wbgot","id","kind","n","g"}
"""
import random
import time
from datetime import datetime, timedelta, timezone

import metrics

MSK = timezone(timedelta(hours=3))
DAYS = (0, 2, 4, 6)                # пн, ср, пт, вс
HOUR = 20
DURATION = 30 * 60
MAX_HP = 5_000_000
NAME = "Владыка Ржавчины"
X, Y = 640, 960                    # юго-запад площади: не закрывает портал, лавки и точку появления
LOC = "lobby"
# полный дроп с одного убийства (кучки по одной штуке — «кто успел, тот забрал»)
DROP = [("cores", 1, 0, 10), ("pot_hp", 1, 0, 5), ("pot_xp", 1, 0, 5), ("pot_atk", 1, 0, 5), ("kit_l", 1, 0, 5),
        ("garmor", 1, 1, 1), ("gweapon", 1, 1, 1), ("gany", 1, 0, 5), ("sph_ti", 1, 0, 2), ("sph_cu", 1, 0, 5)]

st = {"active": False, "hp": 0, "until": 0, "hits": {}, "aoe_t": 0, "sent": 0, "dirty": False, "loot": {}, "loot_until": 0, "manual": False}
_last_hit = {}


def next_start(now=None):
    dt = datetime.fromtimestamp(now or time.time(), MSK)
    for add in range(0, 8):
        d = (dt + timedelta(days=add)).replace(hour=HOUR, minute=0, second=0, microsecond=0)
        if d.weekday() in DAYS and d.timestamp() + DURATION > dt.timestamp():
            return int(d.timestamp())
    return int(dt.timestamp()) + 86400


def view():
    return {"t": "wb", "active": st["active"], "hp": max(0, int(st["hp"])), "max": MAX_HP, "until": int(st["until"] * 1000),
            "next": next_start() * 1000, "name": NAME, "x": X, "y": Y, "loc": LOC}


def start(manual=False, slot=None):
    st.update(active=True, hp=MAX_HP, until=time.time() + DURATION, hits={}, aoe_t=time.time() + 8, dirty=True, manual=manual, slot=slot)
    metrics.inc("wboss.start")


def _ended(now):
    """Босс ушёл или повержен. Плановое окно отмечаем «отыгранным» — повторно в нём босс не появится.
    Раньше блокировка шла по времени конца (30 минут после любого босса): если админ вызвал и убил босса
    перед 20:00, плановый босс в этот вечер не появлялся."""
    st["active"] = False
    st["ended"] = now
    st["dirty"] = True
    if st.get("slot"):
        st["done_slot"] = st["slot"]


def tick(hub):
    now = time.time()
    if not st["active"]:
        s = next_start(now)
        if s <= now < s + DURATION and st.get("done_slot") != s:
            start(slot=s)
            hub.to_all({"t": "pinfo", "text": f"⚠ Мировой босс «{NAME}» появился в Центральном ангаре!"})
    if st["active"]:
        if now > st["until"]:
            _ended(now)
            hub.to_all({"t": "pinfo", "text": f"Мировой босс «{NAME}» ушёл непобеждённым"})
        elif now >= st["aoe_t"]:
            st["aoe_t"] = now + random.uniform(4, 7)
            cands = [c.info for c in hub.by_loc.get(LOC, ()) if (c.info.get("x", 0) - X) ** 2 + (c.info.get("y", 0) - Y) ** 2 < 520 ** 2]
            for i in random.sample(cands, min(3, len(cands))):        # по 3 игрокам — круг удара появляется под ногами
                hub.to_loc(LOC, {"t": "wbaoe", "x": round(i["x"]), "y": round(i["y"]), "r": 110, "in": 1.6})
    if st["dirty"] and now - st["sent"] > 0.5:
        st["dirty"] = False; st["sent"] = now
        hub.to_all(view())
    if st["loot"] and now > st["loot_until"]:
        st["loot"] = {}
        hub.to_loc(LOC, {"t": "wbloot", "items": []})              # кучки исчезают и у игроков (раньше висели до перезахода)


async def on_hit(info, d, hub, seasonpts):
    if not st["active"] or info.get("loc") != LOC:
        return
    uid, now = info["id"], time.time()
    if now - _last_hit.get(uid, 0) < 0.25:
        return
    _last_hit[uid] = now
    if (info.get("x", 0) - X) ** 2 + (info.get("y", 0) - Y) ** 2 > 420 ** 2:
        return                                                     # слишком далеко от босса
    try:
        dmg = int(d.get("dmg", 0))
    except (TypeError, ValueError):
        return
    dmg = max(1, min(dmg, (40 + int(info.get("lvl", 1)) * 8) * 6))
    st["hp"] -= dmg; st["dirty"] = True
    st["hits"][uid] = st["hits"].get(uid, 0) + 1
    await seasonpts.add(uid, "wboss", 1)                           # задание сезона: «Ударить мирового босса 3 раза»
    if st["hp"] <= 0:
        _ended(now)
        st["hp"] = 0
        items, n = {}, 0
        for kind, cnt, g, piles in DROP:
            for _ in range(piles):
                n += 1
                a, r = random.uniform(0, 6.283), random.uniform(30, 190)
                import math
                items[str(n)] = {"id": str(n), "kind": kind, "n": cnt, "g": g, "x": round(X + math.cos(a) * r), "y": round(Y + math.sin(a) * r * 0.7)}
        st["loot"], st["loot_until"] = items, now + 600
        hub.to_all({"t": "pinfo", "text": f"🏆 {NAME} повержен! Собирайте добычу в ангаре"})
        hub.to_all(view())
        hub.to_loc(LOC, {"t": "wbloot", "items": list(items.values())})
        metrics.inc("wboss.kill")


def on_pick(info, d, hub):
    it = st["loot"].get(str(d.get("id", "")))
    if not it or info.get("loc") != LOC or (info.get("x", 0) - it["x"]) ** 2 + (info.get("y", 0) - it["y"]) ** 2 > 90 ** 2:
        return
    st["loot"].pop(it["id"], None)                                 # кто первый — того и предмет
    if it.get("kind") == "cores":
        import ledger
        ledger.add(info["id"], {"cores": it.get("n", 1)})          # учёт ресурсов: ядра с мирового босса
    hub.to_uid(info["id"], {"t": "wbgot", **it})
    hub.to_loc(LOC, {"t": "wbrm", "id": it["id"]})


def cleanup():
    for uid in [u for u, t in _last_hit.items() if time.time() - t > 60]:
        _last_hit.pop(uid, None)
