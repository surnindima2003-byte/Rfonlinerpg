"""PvP-правила в духе Lineage: защита новичков, флаг агрессора, карма, рейтинг."""
import logging
import time

from aiohttp import web
from sqlalchemy import select

from db import SessionLocal
from models import PvpStat, GameSave

log = logging.getLogger("pvp")
NEWBIE_LVL = 10          # до этого уровня PvP недоступно в обе стороны
FLAG_SEC = 30            # напавший на «чистого» пилота 30 с ходит с фиолетовым флагом
KARMA_DECAY_KILLS = 20   # каждые 20 убитых мобов снимают 1 карму
_pair_t = {}             # (убийца, жертва) -> время: рейтинг за одного и того же не чаще раза в 10 мин
_mob_kills = {}


async def stat_of(s, uid):
    row = (await s.execute(select(PvpStat).where(PvpStat.tg_id == uid))).scalar_one_or_none()
    if not row:
        row = PvpStat(tg_id=uid, kills=0, deaths=0, pk=0, karma=0, rating=1000)
        s.add(row)
    return row


async def load_karma(info):
    async with SessionLocal() as s:
        row = (await s.execute(select(PvpStat).where(PvpStat.tg_id == info["id"]))).scalar_one_or_none()
    info["kr"] = row.karma if row else 0


def flagged(info):
    return info.get("flag_t", 0) > time.time()


def can_fight(a, b):
    return a.get("lvl", 1) >= NEWBIE_LVL and b.get("lvl", 1) >= NEWBIE_LVL


def on_hit(attacker, target):
    """Нападение на «чистого» пилота (без кармы и флага) делает нападающего фиолетовым."""
    if not flagged(target) and not target.get("kr"):
        attacker["flag_t"] = time.time() + FLAG_SEC


async def on_death(victim, killer):
    """Итог боя: справедливое убийство — рейтинг, убийство невиновного — карма. Возвращает сообщения обоим."""
    guilty = flagged(victim) or victim.get("kr", 0) > 0
    now = time.time()
    fresh = now - _pair_t.get((killer["id"], victim["id"]), 0) > 600
    _pair_t[(killer["id"], victim["id"])] = now
    gain = 0
    async with SessionLocal() as s:
        k, v = await stat_of(s, killer["id"]), await stat_of(s, victim["id"])
        k.kills += 1
        v.deaths += 1
        if guilty:
            if fresh:
                gain = max(5, min(25, 12 + (victim.get("lvl", 1) - killer.get("lvl", 1))))
                k.rating += gain
                v.rating = max(0, v.rating - gain // 2)
        else:
            k.pk += 1
            k.karma += 2 if victim.get("lvl", 1) <= killer.get("lvl", 1) - 10 else 1
        await s.commit()
        killer["kr"] = k.karma
    return ({"t": "pvp_kill", "nick": victim["nick"], "pk": not guilty, "karma": killer["kr"], "rating": gain},
            {"t": "pvp_died", "red": victim.get("kr", 0) > 0})


async def mob_killed(uid, info):
    """Убийство мобов постепенно смывает карму."""
    if not info or not info.get("kr"):
        return
    _mob_kills[uid] = _mob_kills.get(uid, 0) + 1
    if _mob_kills[uid] >= KARMA_DECAY_KILLS:
        _mob_kills[uid] = 0
        async with SessionLocal() as s:
            row = await stat_of(s, uid)
            row.karma = max(0, row.karma - 1)
            await s.commit()
            info["kr"] = row.karma


async def api_pvp(request):
    from webserver import read_auth
    body, user = await read_auth(request)
    async with SessionLocal() as s:
        me = await stat_of(s, user["id"])
        await s.commit()
        rows = (await s.execute(select(PvpStat, GameSave.nick, GameSave.lvl, GameSave.cls).join(GameSave, GameSave.tg_id == PvpStat.tg_id)
                                .where(PvpStat.kills + PvpStat.deaths > 0).order_by(PvpStat.rating.desc()).limit(100))).all()
        top = [{"uid": str(r[0].tg_id), "nick": r[1] or "Пилот", "lvl": r[2] or 1, "cls": r[3] or "", "rating": r[0].rating,
                "kills": r[0].kills, "deaths": r[0].deaths, "karma": r[0].karma} for r in rows]
    return web.json_response({"ok": True, "top": top, "me": {"rating": me.rating, "kills": me.kills, "deaths": me.deaths, "pk": me.pk, "karma": me.karma}})


def setup(app):
    app.router.add_post("/api/pvp/top", api_pvp)
