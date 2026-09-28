"""Специальные квесты: вступить в чат игры и подписаться на канал.

Проверку делает сам бот через getChatMember, награда приходит обычной выдачей (grant):
онлайн — сразу, офлайн — при следующем входе. Отметка «уже получено» — это сама выдача
с by_admin = "special:<id>", поэтому отдельная таблица не нужна.

Требование Telegram: для проверки подписчиков КАНАЛА бот должен быть его администратором;
в ЧАТЕ (супергруппе) бот должен состоять.
"""
import asyncio
import json
import logging
import time

from aiohttp import web
from sqlalchemy import select

import gram
import metrics
from db import SessionLocal
from models import Grant

log = logging.getLogger("special")

QUESTS = {
    "chat": {"chat": "@RFONLINERPG_CHAT", "url": "https://t.me/RFONLINERPG_CHAT", "kind": "cores", "amount": 10},
    "channel": {"chat": "@RFONLINERPG", "url": "https://t.me/RFONLINERPG", "kind": "cores", "amount": 20},
}
MEMBER = {"member", "administrator", "creator"}
_locks = {}
_last = {}


async def _claimed(s, uid):
    rows = (await s.execute(select(Grant.by_admin).where(Grant.tg_id == uid, Grant.by_admin.like("special:%")))).scalars().all()
    return {r.split(":", 1)[1] for r in rows}


async def is_member(chat, uid):
    """True/False — состоит ли игрок; None — Telegram не дал проверить (бот не админ, чат не найден)."""
    bot = gram.BOT.get("bot")
    if not bot:
        return None
    try:
        m = await asyncio.wait_for(bot.get_chat_member(chat_id=chat, user_id=uid), 8)
    except Exception as e:
        log.warning("Проверка подписки %s для %s не удалась: %s", chat, uid, e)
        metrics.inc("special.check_error")
        return None
    status = getattr(m, "status", "")
    status = getattr(status, "value", status)
    if status in MEMBER:
        return True
    return bool(status == "restricted" and getattr(m, "is_member", False))


def setup(app, read_auth, push_to_player, grant_dict):
    async def api_status(request):
        body, user = await read_auth(request)
        async with SessionLocal() as s:
            done = await _claimed(s, user["id"])
        return web.json_response({"ok": True, "quests": {k: {"done": k in done, "url": q["url"], "reward": q["amount"]}
                                                          for k, q in QUESTS.items()}})

    async def api_claim(request):
        body, user = await read_auth(request)
        uid, qid = user["id"], str(body.get("id", ""))
        q = QUESTS.get(qid)
        if not q:
            return web.json_response({"ok": False, "error": "Такого задания нет"})
        if time.time() - _last.get(uid, 0) < 3:
            return web.json_response({"ok": False, "error": "Подожди пару секунд"})
        _last[uid] = time.time()
        lock = _locks.setdefault(uid, asyncio.Lock())
        async with lock:                                           # двойное нажатие не даст две награды
            async with SessionLocal() as s:
                if qid in await _claimed(s, uid):
                    return web.json_response({"ok": True, "already": True})
            ok = await is_member(q["chat"], uid)
            if ok is None:
                return web.json_response({"ok": False, "error": "Проверка временно недоступна, попробуй позже"})
            if not ok:
                return web.json_response({"ok": False, "need": "join", "url": q["url"],
                                          "error": "Сначала вступи, потом нажми «Проверить»"})
            async with SessionLocal() as s:
                g = Grant(tg_id=uid, kind=q["kind"], payload=json.dumps({"amount": q["amount"], "quest": qid}),
                          by_admin="special:" + qid, created=int(time.time()))
                s.add(g)
                await s.commit()
                gd = grant_dict(g)
        await push_to_player(uid, {"t": "grant", "grant": gd})
        metrics.inc("special.claim." + qid)
        log.info("Спецквест %s выполнен: uid=%s", qid, uid)
        return web.json_response({"ok": True, "reward": q["amount"], "kind": q["kind"], "grant": gd})

    app.router.add_post("/api/special/status", api_status)
    app.router.add_post("/api/special/claim", api_claim)
