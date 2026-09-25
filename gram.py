"""GRAM (бывший Toncoin) в игре: серверный баланс, пополнение из блокчейна TON, покупки и вывод.

Как работает пополнение: у игры один кошелёк (GAME_WALLET), у каждого игрока свой код-комментарий.
Сервер раз в 15 секунд читает входящие переводы через toncenter и зачисляет их по комментарию.
Сид-фраза кошелька на сервере не хранится: выплаты администратор делает сам и отмечает их в админке.
"""
import asyncio
import json
import logging
import re
import secrets
import time

import aiohttp
from aiohttp import web
from sqlalchemy import select, func

from config import TON_NETWORK, GAME_WALLET, TONCENTER_KEY, GRAM_WITHDRAW_MIN, GRAM_WITHDRAW_FEE
from db import SessionLocal
from models import GramWallet, GramTx, GramWithdrawal, GameSave, Meta, Referral, RefEarn

log = logging.getLogger("gram")
NANO = 1_000_000_000
API = "https://testnet.toncenter.com/api/v2" if TON_NETWORK != "mainnet" else "https://toncenter.com/api/v2"
ADDR_RE = re.compile(r"^(EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$|^-?\d:[0-9a-fA-F]{64}$")
# цены магазина задаёт только сервер
PACK_PRICES = {"season": 10.5, "books": 5, "spheres": 1, "cores": 2, "legend": 20}


def g(nano):
    return round(nano / NANO, 4)


async def wallet_of(s, uid):
    w = (await s.execute(select(GramWallet).where(GramWallet.tg_id == uid))).scalar_one_or_none()
    if not w:
        w = GramWallet(tg_id=uid, memo="MW" + secrets.token_hex(4).upper(), balance=0, spent=0, created=int(time.time()))
        s.add(w)
        await s.commit()
    return w


async def move(s, uid, amount, kind, ref, note=""):
    """Изменить баланс и записать операцию. Возвращает False, если не хватает средств."""
    w = await wallet_of(s, uid)
    if amount < 0 and w.balance + amount < 0:
        return False
    w.balance += amount
    s.add(GramTx(tg_id=uid, kind=kind, amount=amount, ref=ref, note=note[:200], ts=int(time.time())))
    return True


# ---------- реферальная система ----------
REF_RATES = {1: 0.05, 2: 0.02}          # 5% с покупок друга, 2% с покупок друзей друга
BOT_USERNAME = {"name": ""}             # заполняет main.py при запуске


async def bind_referral(uid, inviter_id):
    """Привязать нового игрока к пригласившему. Только один раз, не себя, без замкнутого круга."""
    if not inviter_id or inviter_id == uid:
        return False
    async with SessionLocal() as s:
        if (await s.execute(select(Referral).where(Referral.tg_id == uid))).scalar_one_or_none():
            return False
        save = (await s.execute(select(GameSave).where(GameSave.tg_id == uid))).scalar_one_or_none()
        if save and save.data and save.updated and time.time() - save.updated > 3600 and (save.lvl or 1) > 3:
            return False                                   # старых игроков задним числом не привязываем
        up = (await s.execute(select(Referral).where(Referral.tg_id == inviter_id))).scalar_one_or_none()
        if up and up.inviter_id == uid:
            return False                                   # A пригласил B, B не может пригласить A
        if not (await s.execute(select(GameSave).where(GameSave.tg_id == inviter_id))).scalar_one_or_none():
            return False                                   # пригласивший должен быть игроком
        s.add(Referral(tg_id=uid, inviter_id=inviter_id, created=int(time.time())))
        await s.commit()
    log.info("Реферал: %s приглашён игроком %s", uid, inviter_id)
    return True


async def pay_referrals(s, buyer, spent_nano):
    """Бонус из выручки игры: 5% пригласившему, 2% пригласившему пригласившего."""
    from webserver import push_to_player
    notes = []
    cur, level = buyer, 1
    while level <= 2:
        row = (await s.execute(select(Referral).where(Referral.tg_id == cur))).scalar_one_or_none()
        if not row:
            break
        bonus = int(spent_nano * REF_RATES[level])
        if bonus > 0:
            await move(s, row.inviter_id, bonus, "ref", f"ref{level}:{buyer}:{time.time_ns()}", f"Реферальный бонус {int(REF_RATES[level]*100)}%")
            s.add(RefEarn(inviter_id=row.inviter_id, friend_id=buyer, level=level, amount=bonus, ts=int(time.time())))
            notes.append((row.inviter_id, bonus))
        cur, level = row.inviter_id, level + 1
    return notes


# ---------- наблюдатель входящих переводов ----------
async def deposit_watcher():
    if not GAME_WALLET:
        log.warning("GAME_WALLET не задан: приём GRAM выключен")
        return
    log.info("Приём GRAM: сеть %s, кошелёк игры %s", TON_NETWORK, GAME_WALLET)
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    async with aiohttp.ClientSession(headers=headers) as http:
        while True:
            try:
                async with http.get(API + "/getTransactions", params={"address": GAME_WALLET, "limit": 40, "archival": "true"}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
                if data.get("ok"):
                    await process_transactions(data.get("result", []))
            except Exception as e:
                log.warning("toncenter: %s", e)
            await asyncio.sleep(15)


async def process_transactions(txs):
    from webserver import push_to_player        # поздний импорт, чтобы избежать цикла
    for tx in reversed(txs):
        msg = tx.get("in_msg") or {}
        try:
            value = int(msg.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0 or not msg.get("source"):
            continue
        h = (tx.get("transaction_id") or {}).get("hash", "")
        if not h:
            continue
        ref = "dep:" + h
        comment = str(msg.get("message") or "").strip().upper()
        async with SessionLocal() as s:
            if (await s.execute(select(GramTx).where(GramTx.ref == ref))).scalar_one_or_none():
                continue
            w = (await s.execute(select(GramWallet).where(GramWallet.memo == comment))).scalar_one_or_none() if comment else None
            if w:
                await move(s, w.tg_id, value, "deposit", ref, "Пополнение " + str(msg.get("source"))[:48])
                await s.commit()
                log.info("Зачислено %s GRAM игроку %s", g(value), w.tg_id)
                await push_to_player(w.tg_id, {"t": "gram", "text": f"Пополнение: +{g(value)} GRAM"})
            else:
                # перевод без кода или с чужим кодом — показываем администратору
                s.add(GramTx(tg_id=0, kind="unmatched", amount=value, ref=ref, note=(comment or "без комментария")[:40] + " от " + str(msg.get("source"))[:48], ts=int(time.time())))
                await s.commit()


# ---------- API игрока ----------
async def api_gram(request):
    from webserver import read_auth, push_to_player
    body, user = await read_auth(request)
    op, uid = request.match_info["op"], user["id"]
    async with SessionLocal() as s:
        w = await wallet_of(s, uid)
        if op == "state":
            hist = (await s.execute(select(GramTx).where(GramTx.tg_id == uid).order_by(GramTx.ts.desc()).limit(40))).scalars().all()
            wds = (await s.execute(select(GramWithdrawal).where(GramWithdrawal.tg_id == uid).order_by(GramWithdrawal.created.desc()).limit(10))).scalars().all()
            return web.json_response({"ok": True, "balance": g(w.balance), "spent": g(w.spent), "memo": w.memo, "address": GAME_WALLET, "network": TON_NETWORK,
                                      "min": GRAM_WITHDRAW_MIN, "fee": GRAM_WITHDRAW_FEE,
                                      "hist": [{"kind": x.kind, "amount": g(x.amount), "note": x.note, "ts": x.ts * 1000} for x in hist],
                                      "wds": [{"id": x.id, "amount": g(x.amount), "payout": g(x.payout), "address": x.address, "status": x.status, "ts": x.created * 1000} for x in wds]})
        if op == "spend":
            pack = str(body.get("pack", ""))
            price = PACK_PRICES.get(pack)
            if price is None:
                return web.json_response({"ok": False, "error": "Нет такого пака"})
            nano = int(round(price * NANO))
            if not await move(s, uid, -nano, "shop", f"shop:{uid}:{pack}:{time.time_ns()}", "Магазин: " + pack):
                return web.json_response({"ok": False, "error": "Не хватает GRAM"})
            w.spent += nano
            notes = await pay_referrals(s, uid, nano)
            await s.commit()
            for who, bonus in notes:
                await push_to_player(who, {"t": "gram", "text": f"Реферальный бонус: +{g(bonus)} GRAM"})
            return web.json_response({"ok": True, "balance": g(w.balance), "spent": g(w.spent)})
        if op == "withdraw":
            address = str(body.get("address", "")).strip()
            try:
                amount = float(body.get("amount", 0))
            except (TypeError, ValueError):
                amount = 0
            if not ADDR_RE.match(address):
                return web.json_response({"ok": False, "error": "Проверь TON-адрес"})
            if amount < GRAM_WITHDRAW_MIN or amount > 1_000_000:
                return web.json_response({"ok": False, "error": f"Минимальный вывод {GRAM_WITHDRAW_MIN} GRAM"})
            pending = (await s.execute(select(func.count()).select_from(GramWithdrawal).where(GramWithdrawal.tg_id == uid, GramWithdrawal.status == "pending"))).scalar()
            if pending >= 3:
                return web.json_response({"ok": False, "error": "Не больше трёх заявок одновременно"})
            nano = int(round(amount * NANO))
            if not await move(s, uid, -nano, "withdraw", f"wd:{uid}:{time.time_ns()}", "Заявка на вывод"):
                return web.json_response({"ok": False, "error": "Не хватает GRAM"})
            save = (await s.execute(select(GameSave).where(GameSave.tg_id == uid))).scalar_one_or_none()
            s.add(GramWithdrawal(tg_id=uid, nick=(save.nick if save and save.nick else user["name"])[:16], address=address, amount=nano,
                                 payout=int(nano * (1 - GRAM_WITHDRAW_FEE)), created=int(time.time()), updated=int(time.time())))
            await s.commit()
            return web.json_response({"ok": True, "balance": g(w.balance)})
        if op == "ref":
            direct = (await s.execute(select(Referral).where(Referral.inviter_id == uid).order_by(Referral.created.desc()))).scalars().all()
            ids = [r.tg_id for r in direct]
            second = (await s.execute(select(func.count()).select_from(Referral).where(Referral.inviter_id.in_(ids)))).scalar() if ids else 0
            earned = (await s.execute(select(func.coalesce(func.sum(RefEarn.amount), 0)).where(RefEarn.inviter_id == uid))).scalar()
            per = {}
            for fid, lvl_, amt in (await s.execute(select(RefEarn.friend_id, RefEarn.level, func.sum(RefEarn.amount)).where(RefEarn.inviter_id == uid).group_by(RefEarn.friend_id, RefEarn.level))).all():
                per[fid] = per.get(fid, 0) + (amt or 0)
            saves = {r.tg_id: r for r in (await s.execute(select(GameSave).where(GameSave.tg_id.in_(ids)))).scalars().all()} if ids else {}
            friends = [{"nick": (saves[f].nick if f in saves and saves[f].nick else "Пилот"), "lvl": (saves[f].lvl if f in saves else 1),
                        "earned": g(per.get(f, 0)), "ts": r.created * 1000} for f, r in zip(ids, direct)]
            name = BOT_USERNAME["name"]
            link = f"https://t.me/{name}?start=ref_{uid}" if name else ""
            return web.json_response({"ok": True, "link": link, "count": len(ids), "second": second, "earned": g(earned), "friends": friends[:100]})
    raise web.HTTPBadRequest(text="bad op")


# ---------- API администратора ----------
async def api_gram_admin(request):
    from webserver import read_auth, push_to_player
    body, user = await read_auth(request)
    if not user["admin"]:
        raise web.HTTPForbidden(text="not admin")
    op = request.match_info["op"]
    async with SessionLocal() as s:
        if op == "list":
            wds = (await s.execute(select(GramWithdrawal).where(GramWithdrawal.status == "pending").order_by(GramWithdrawal.created))).scalars().all()
            um = (await s.execute(select(GramTx).where(GramTx.kind == "unmatched").order_by(GramTx.ts.desc()).limit(20))).scalars().all()
            total = (await s.execute(select(func.coalesce(func.sum(GramWallet.balance), 0)))).scalar()
            return web.json_response({"ok": True, "network": TON_NETWORK, "address": GAME_WALLET, "total": g(total),
                                      "wds": [{"id": x.id, "nick": x.nick, "address": x.address, "amount": g(x.amount), "payout": g(x.payout), "ts": x.created * 1000} for x in wds],
                                      "unmatched": [{"amount": g(x.amount), "note": x.note, "ts": x.ts * 1000} for x in um]})
        if op == "decide":
            wd = (await s.execute(select(GramWithdrawal).where(GramWithdrawal.id == int(body.get("id", 0))))).scalar_one_or_none()
            if not wd or wd.status != "pending":
                return web.json_response({"ok": False, "error": "Заявка уже обработана"})
            action = body.get("action")
            if action == "paid":
                wd.status, wd.tx_hash, wd.updated = "paid", str(body.get("tx", ""))[:120], int(time.time())
                text = f"Вывод {g(wd.payout)} GRAM выполнен"
            elif action == "reject":
                wd.status, wd.updated = "rejected", int(time.time())
                await move(s, wd.tg_id, wd.amount, "refund", f"refund:{wd.id}", "Возврат: заявка отклонена")
                text = f"Заявка на вывод отклонена, {g(wd.amount)} GRAM вернулись на баланс"
            else:
                raise web.HTTPBadRequest(text="bad action")
            await s.commit()
            await push_to_player(wd.tg_id, {"t": "gram", "text": text})
            return web.json_response({"ok": True})
        if op == "credit":
            # тестовые начисления разрешены только в тестовой сети
            if TON_NETWORK == "mainnet":
                return web.json_response({"ok": False, "error": "В основной сети начислять GRAM вручную нельзя"})
            target = str(body.get("target", "me")).strip().lstrip("@").lower()
            if target in ("", "me"):
                uid = user["id"]
            else:
                row = (await s.execute(select(GameSave).where(func.lower(GameSave.username) == target))).scalar_one_or_none()
                if not row:
                    return web.json_response({"ok": False, "error": "Игрок не найден"})
                uid = row.tg_id
            amount = max(0.01, min(10000.0, float(body.get("amount", 10))))
            await move(s, uid, int(amount * NANO), "test", f"test:{uid}:{time.time_ns()}", "Тестовое начисление")
            await s.commit()
            await push_to_player(uid, {"t": "gram", "text": f"Тестовое начисление: +{amount} GRAM"})
            return web.json_response({"ok": True})
    raise web.HTTPBadRequest(text="bad op")


async def migrate_market_to_gram():
    """Лоты маркета раньше стоили лом. При переходе на GRAM старые лоты снимаются, предметы возвращаются продавцам."""
    from models import MarketLot, Grant
    async with SessionLocal() as s:
        done = (await s.execute(select(Meta).where(Meta.key == "market_gram"))).scalar_one_or_none()
        if done:
            return
        lots = (await s.execute(select(MarketLot))).scalars().all()
        for lot in lots:
            it = json.loads(lot.item)
            s.add(Grant(tg_id=lot.seller_id, kind="item", payload=json.dumps({"amount": it.get("n", 1), "item": it["id"], "grade": it.get("g", 0)}),
                        by_admin="market", created=int(time.time())))
        await s.execute(MarketLot.__table__.delete())
        s.add(Meta(key="market_gram", value="1"))
        await s.commit()
        if lots:
            log.info("Маркет переведён на GRAM: %s старых лотов возвращены продавцам", len(lots))


def setup(app):
    app.router.add_post("/api/gram/admin/{op}", api_gram_admin)
    app.router.add_post("/api/gram/{op}", api_gram)
