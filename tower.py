"""Кровавая башня (как в Liberty, в нашем стиле).

Каждый день в 20:30 МСК открывается запись на 5 минут, в 20:35 — общий старт для всех записавшихся.
У каждого своя дорога: 60 монстров 5 уровня, барьер, 60 монстров 10 уровня, барьер — и общий босс
(без дропа, прочность общая и считается на сервере). Побеждает тот, кто нанёс боссу больше всего урона.
Смерть в коридоре — выбывание. Только с 10 уровня, одна попытка в день (тратится при старте).
Награды: опыт с монстров в башне ×4 (в игре), +10 ядер всем, кто ударил босса, +30 ядер победителю.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

import metrics
from db import SessionLocal
from models import Grant

MSK = timezone(timedelta(hours=3))
OPEN_H, OPEN_M, REG_SEC, RUN_SEC = 20, 30, 5 * 60, 15 * 60
NAME = "Багровый Страж"
st = {"phase": "idle", "reg": set(), "until": 0, "hp": 0, "max": 0, "dmg": {}, "nick": {}, "dead": set(), "day": "", "used": set(), "sent": 0, "dirty": False}
_last = {}


def _today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _open_ts(day_offset=0):
    d = (datetime.now(MSK) + timedelta(days=day_offset)).replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    return int(d.timestamp())


def next_open():
    t = _open_ts()
    return t if time.time() < t + REG_SEC else _open_ts(1)


def view(uid=None):
    v = {"t": "tw", "phase": st["phase"], "reg": len(st["reg"]), "until": int(st["until"] * 1000), "next": next_open() * 1000,
         "hp": max(0, int(st["hp"])), "max": st["max"], "name": NAME}
    if uid is not None:
        v["me"] = uid in st["reg"]
        v["used"] = uid in st["used"]
        v["dead"] = uid in st["dead"]
    return v


def _day_reset():
    if st["day"] != _today():
        st["day"], st["used"] = _today(), set()


def open_reg(sec=REG_SEC):
    st.update(phase="reg", reg=set(), until=time.time() + sec, dmg={}, dead=set(), dirty=True, nick={})   # ники прошлой башни не копятся


async def tick(hub, seasonpts, push):
    _day_reset()
    now = time.time()
    if st["phase"] in ("idle", "done") and _open_ts() <= now < _open_ts() + REG_SEC:
        open_reg(_open_ts() + REG_SEC - now)
        hub.to_all({"t": "pinfo", "text": "🩸 Кровавая башня открыта: запись 5 минут (Меню → События)"})
    if st["phase"] == "reg" and now >= st["until"]:
        if not st["reg"]:
            st["phase"] = "done"; st["dirty"] = True
        else:
            n = len(st["reg"])
            st.update(phase="run", until=now + RUN_SEC, max=250_000 + 120_000 * n, dirty=True)
            st["hp"] = st["max"]
            for uid in st["reg"]:
                st["used"].add(uid)                                    # попытка тратится при старте
                await seasonpts.add(uid, "tower", 1)                   # задание сезона «Кровавая башня»
                hub.to_uid(uid, {"t": "twstart", "max": st["max"]})
            metrics.inc("tower.start")
    if st["phase"] == "run" and (now >= st["until"] or (st["reg"] and st["reg"] <= st["dead"])):
        await _finish(hub, push, None)
    if st["dirty"] and now - st["sent"] > 0.5:
        st["dirty"] = False; st["sent"] = now
        for uid in st["reg"]:
            hub.to_uid(uid, view(uid))


async def _finish(hub, push, winner):
    st["phase"] = "done"; st["dirty"] = True
    top = sorted(st["dmg"].items(), key=lambda kv: -kv[1])
    if winner is None and top and st["hp"] <= 0:
        winner = top[0][0]
    res = [{"nick": st["nick"].get(u, "Пилот"), "dmg": d} for u, d in top[:10]]
    now = int(time.time())
    grants = []
    async with SessionLocal() as s:
        for uid, d in top:
            amt = 10 + (30 if uid == winner else 0)
            g = Grant(tg_id=uid, kind="cores", payload=json.dumps({"amount": amt}), by_admin="tower", created=now)
            s.add(g); grants.append((uid, g, amt))
        await s.commit()
    for uid, g, amt in grants:                                         # награда приходит сразу (офлайн — при входе)
        hub.to_uid(uid, {"t": "grant", "grant": {"id": getattr(g, "id", None), "kind": "cores", "payload": {"amount": amt}, "by": "tower"}})
    for uid in st["reg"]:
        hub.to_uid(uid, {"t": "twend", "win": uid == winner, "killed": st["hp"] <= 0, "top": res,
                         "winner": st["nick"].get(winner, "") if winner else ""})
    metrics.inc("tower.end")


async def on_hit(info, d, hub, push):
    uid, now = info["id"], time.time()
    if st["phase"] != "run" or uid not in st["reg"] or uid in st["dead"] or info.get("dead") or info.get("loc") != "tower" or now - _last.get(uid, 0) < 0.25:
        return
    _last[uid] = now
    try:
        dmg = max(1, min(int(d.get("dmg", 0)), (40 + int(info.get("lvl", 1)) * 8) * 6))
    except (TypeError, ValueError):
        return
    st["hp"] -= dmg; st["dirty"] = True
    st["dmg"][uid] = st["dmg"].get(uid, 0) + dmg
    st["nick"][uid] = info.get("nick", "Пилот")
    if st["hp"] <= 0:
        st["hp"] = 0
        await _finish(hub, push, max(st["dmg"].items(), key=lambda kv: kv[1])[0])


def on_dead(info):
    if st["phase"] == "run" and info["id"] in st["reg"]:
        st["dead"].add(info["id"]); st["dirty"] = True


def setup(app, read_auth, online):
    async def api_state(request):
        body, user = await read_auth(request)
        _day_reset()
        return web.json_response({"ok": True, **view(user["id"])})

    async def api_join(request):
        body, user = await read_auth(request)
        uid = user["id"]
        _day_reset()
        if body.get("admin_open") and user["admin"] and st["phase"] in ("idle", "done"):
            open_reg(40)                                               # админ: открыть запись на 40 сек для проверки
            return web.json_response({"ok": True, **view(uid)})
        if body.get("leave"):
            st["reg"].discard(uid); st["dirty"] = True
            return web.json_response({"ok": True, **view(uid)})
        info = online(uid) or {}
        if st["phase"] != "reg":
            return web.json_response({"ok": False, "error": "Запись сейчас закрыта"})
        if int(info.get("lvl", 1)) < 10 and not user["admin"]:
            return web.json_response({"ok": False, "error": "Кровавая башня — с 10 уровня"})
        if uid in st["used"] and not user["admin"]:                    # админу можно повторять для проверки
            return web.json_response({"ok": False, "error": "Попытка на сегодня уже использована"})
        st["reg"].add(uid); st["nick"][uid] = info.get("nick", user["name"][:16]); st["dirty"] = True
        return web.json_response({"ok": True, **view(uid)})

    app.router.add_post("/api/tower/state", api_state)
    app.router.add_post("/api/tower/join", api_join)


def cleanup():
    """Раз в минуту: отметки частоты ударов старше минуты не нужны."""
    for uid in [u for u, t in _last.items() if time.time() - t > 60]:
        _last.pop(uid, None)
