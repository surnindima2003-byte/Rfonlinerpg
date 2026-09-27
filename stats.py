"""Статистика для админа и сбор ошибок из игры."""
import hashlib
import time

from aiohttp import web
from sqlalchemy import select, func

from db import SessionLocal
from models import GameSave, FirstSeen, ClientError, GramWallet, GramTx, GramWithdrawal, StarPayment, MarketHist, PvpStat

_err_rate = {}          # tg_id -> [время, количество] — не больше 20 ошибок в минуту от игрока
_err_users = {}         # ключ ошибки -> набор игроков (в памяти, для оценки охвата)
NANO = 10 ** 9


async def mark_seen(uid):
    """Запомнить первый вход игрока (вызывается при загрузке сохранения)."""
    async with SessionLocal() as s:
        if not (await s.execute(select(FirstSeen).where(FirstSeen.tg_id == uid))).scalar_one_or_none():
            s.add(FirstSeen(tg_id=uid, ts=int(time.time())))
            await s.commit()


async def api_err(request):
    from webserver import read_auth
    try:
        body, user = await read_auth(request)
    except web.HTTPException:
        return web.json_response({"ok": False})
    uid, now = user["id"], time.time()
    t0, n = _err_rate.get(uid, (now, 0))
    if now - t0 > 60:
        t0, n = now, 0
    if n >= 20:
        return web.json_response({"ok": True})
    _err_rate[uid] = (t0, n + 1)
    msg = str(body.get("msg", ""))[:500]
    if not msg:
        return web.json_response({"ok": True})
    stack = str(body.get("stack", ""))[:2000]
    key = hashlib.sha1((msg + stack.split("\n")[1:2].__str__()).encode()).hexdigest()[:40]
    users = _err_users.setdefault(key, set())
    users.add(uid)
    async with SessionLocal() as s:
        row = (await s.execute(select(ClientError).where(ClientError.key == key))).scalar_one_or_none()
        if not row:
            row = ClientError(key=key, msg=msg, stack=stack, ua=str(body.get("ua", ""))[:200], count=0, users=0, first=int(now))
            s.add(row)
        row.count = (row.count or 0) + 1
        row.users = max(row.users or 0, len(users))
        row.last = int(now)
        await s.commit()
    return web.json_response({"ok": True})


async def api_stats(request):
    from webserver import read_auth, clients
    body, user = await read_auth(request)
    if not user["admin"]:
        raise web.HTTPForbidden()
    now = int(time.time())
    day, week = now - 86400, now - 7 * 86400
    async with SessionLocal() as s:
        cnt = lambda q: s.execute(q)
        total = (await cnt(select(func.count()).select_from(GameSave))).scalar() or 0
        dau = (await cnt(select(func.count()).select_from(GameSave).where(GameSave.updated >= day))).scalar() or 0
        wau = (await cnt(select(func.count()).select_from(GameSave).where(GameSave.updated >= week))).scalar() or 0
        new_day = (await cnt(select(func.count()).select_from(FirstSeen).where(FirstSeen.ts >= day))).scalar() or 0
        new_week = (await cnt(select(func.count()).select_from(FirstSeen).where(FirstSeen.ts >= week))).scalar() or 0
        # удержание: пришли 1–2 дня назад и зашли снова за последние сутки
        cohort = select(FirstSeen.tg_id).where(FirstSeen.ts >= now - 2 * 86400, FirstSeen.ts < day)
        c_all = (await cnt(select(func.count()).select_from(cohort.subquery()))).scalar() or 0
        c_back = (await cnt(select(func.count()).select_from(GameSave).where(GameSave.tg_id.in_(cohort), GameSave.updated >= day))).scalar() or 0
        lv_rows = (await cnt(select(GameSave.lvl, func.count()).group_by(GameSave.lvl))).all()
        buckets = {"1–9": 0, "10–19": 0, "20–29": 0, "30–39": 0, "40–50": 0, "50+": 0}
        for lv, n in lv_rows:
            lv = lv or 1
            b = "1–9" if lv < 10 else "10–19" if lv < 20 else "20–29" if lv < 30 else "30–39" if lv < 40 else "40–50" if lv <= 50 else "50+"
            buckets[b] += n
        gram_total = (await cnt(select(func.coalesce(func.sum(GramWallet.balance), 0)))).scalar() or 0
        gram_locked = (await cnt(select(func.coalesce(func.sum(GramWallet.locked), 0)))).scalar() or 0
        dep_day = (await cnt(select(func.coalesce(func.sum(GramTx.amount), 0)).where(GramTx.kind == "deposit", GramTx.ts >= day))).scalar() or 0
        dep_all = (await cnt(select(func.coalesce(func.sum(GramTx.amount), 0)).where(GramTx.kind == "deposit"))).scalar() or 0
        shop_day = (await cnt(select(func.coalesce(func.sum(GramTx.amount), 0)).where(GramTx.kind == "shop", GramTx.ts >= day))).scalar() or 0
        wd_pending = (await cnt(select(func.count(), func.coalesce(func.sum(GramWithdrawal.amount), 0)).where(GramWithdrawal.status == "pending"))).first()
        stars_all = (await cnt(select(func.coalesce(func.sum(StarPayment.stars), 0)))).scalar() or 0
        stars_day = (await cnt(select(func.coalesce(func.sum(StarPayment.stars), 0)).where(StarPayment.ts >= day))).scalar() or 0
        mk_day = (await cnt(select(func.count(), func.coalesce(func.sum(MarketHist.price), 0)).where(MarketHist.kind == "buy", MarketHist.ts >= day))).first()
        pvp_fights = (await cnt(select(func.coalesce(func.sum(PvpStat.kills), 0)))).scalar() or 0
        errs = (await cnt(select(ClientError).order_by(ClientError.last.desc()).limit(30))).scalars().all()
    g = lambda v: round((v or 0) / NANO, 4)
    return web.json_response({"ok": True,
        "players": {"online": len(clients), "dau": dau, "wau": wau, "total": total, "new_day": new_day, "new_week": new_week,
                    "retention": round(c_back * 100 / c_all) if c_all else None, "cohort": c_all, "levels": buckets},
        "money": {"gram_total": g(gram_total), "gram_locked": g(gram_locked), "dep_day": g(dep_day), "dep_all": g(dep_all),
                  "shop_day": g(-shop_day), "wd_pending": wd_pending[0] or 0, "wd_pending_sum": g(wd_pending[1]),
                  "stars_day": stars_day, "stars_all": stars_all, "market_day": mk_day[0] or 0, "market_day_sum": g(mk_day[1]), "pvp_kills": pvp_fights},
        "errors": [{"msg": e.msg, "stack": e.stack[:600], "count": e.count, "users": e.users, "last": e.last * 1000, "ua": e.ua} for e in errs]})


def setup(app):
    app.router.add_post("/api/err", api_err)
    app.router.add_post("/api/admin/stats", api_stats)
