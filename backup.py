"""Ежедневные резервные копии: полный файл SQLite и отдельная выгрузка денежных таблиц (для любой базы)."""
import asyncio
import datetime
import gzip
import json
import logging
import os
import sqlite3

from sqlalchemy import select

from config import BACKUP_DIR
from db import engine, IS_PG, SQLITE_URL
from models import Base

log = logging.getLogger("backup")
MONEY_TABLES = ("gram_wallets", "gram_tx", "gram_withdrawals", "star_payments", "item_inst", "sphere_bal", "market_lots", "referrals", "ref_earn")
KEEP_DAYS = 14


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    made = []
    if not IS_PG:
        src_path = SQLITE_URL.replace("sqlite:///", "", 1)
        if os.path.exists(src_path):
            dst_path = os.path.join(BACKUP_DIR, f"db-{stamp}.sqlite")
            src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
            with dst:
                src.backup(dst)                          # целостная копия даже во время работы
            src.close(); dst.close()
            made.append(dst_path)
    dump = {}
    with engine.connect() as conn:
        for t in Base.metadata.sorted_tables:
            if t.name in MONEY_TABLES:
                dump[t.name] = [dict(r._mapping) for r in conn.execute(select(t))]
    money_path = os.path.join(BACKUP_DIR, f"money-{stamp}.json.gz")
    with gzip.open(money_path, "wt", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, default=str)
    made.append(money_path)
    # старые копии удаляем
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=KEEP_DAYS)
    for name in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, name)
        if os.path.isfile(p) and datetime.datetime.utcfromtimestamp(os.path.getmtime(p)) < cutoff:
            os.remove(p)
    return made


async def send_to_admins(made):
    """Денежная выгрузка уходит администраторам в Telegram: копия переживёт даже потерю диска Railway."""
    from aiogram.types import FSInputFile
    from sqlalchemy import func as sfunc
    import gram
    from config import ADMIN_USERNAMES
    from db import SessionLocal
    from models import GameSave
    bot = gram.BOT.get("bot")
    money = [p for p in made if os.path.basename(p).startswith("money-")]
    if not bot or not money or not ADMIN_USERNAMES:
        return
    async with SessionLocal() as s:
        ids = [r[0] for r in (await s.execute(select(GameSave.tg_id).where(sfunc.lower(GameSave.username).in_([u.lower() for u in ADMIN_USERNAMES])))).all()]
    for chat in ids:
        try:
            await bot.send_document(chat, FSInputFile(money[0]), caption="🗄 Ежедневная резервная копия денежных таблиц MetalWar (GRAM, выводы, звёзды, реестр вещей, маркет, рефералы). Храни этот файл.")
        except Exception as e:
            log.warning("Не удалось отправить копию админу %s: %s", chat, e)


async def backup_loop():
    await asyncio.sleep(60)
    while True:
        try:
            made = await asyncio.get_event_loop().run_in_executor(None, make_backup)
            log.info("Резервная копия: %s", ", ".join(os.path.basename(x) for x in made))
            if os.getenv("BACKUP_TO_TELEGRAM", "1") != "0":
                await send_to_admins(made)
        except Exception as e:
            log.error("Резервная копия не удалась: %s", e)
        await asyncio.sleep(24 * 3600)
