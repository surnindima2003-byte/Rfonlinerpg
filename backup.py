"""Ежедневные резервные копии: полный файл SQLite (на PostgreSQL — полная выгрузка всех таблиц) и отдельная
выгрузка денежных таблиц (для любой базы). Перед вайпом webserver.ensure_epoch делает полную выгрузку (dump_all)."""
import asyncio
import datetime
import gzip
import json
import logging
import os
import sqlite3

from sqlalchemy import select, inspect as sa_inspect

from config import BACKUP_DIR
from db import engine, IS_PG, SQLITE_URL
from models import Base

log = logging.getLogger("backup")
# season_prizes — призы сезона в USDT (ждёт / подтверждён / выдан): таблица переживает вайп, раньше в копию не попадала
MONEY_TABLES = ("gram_wallets", "gram_tx", "gram_withdrawals", "star_payments", "item_inst", "sphere_bal", "market_lots", "referrals", "ref_earn",
                "season_prizes")
KEEP_DAYS = 14
KEEP_FULL_DAYS = 3           # полные выгрузки большие (все сохранения) — храним меньше


def dump_all(prefix="full", tables=None):
    """Полная выгрузка таблиц (по умолчанию — всех) в BACKUP_DIR/<prefix>-<время>.jsonl.gz, построчно.

    Раньше на PostgreSQL в копию попадали только денежные таблицы: после ошибочного вайпа сохранения,
    гильдии и вещи было не восстановить. Формат: первая строка {"table": имя}, дальше строки таблицы.
    Пишется потоково, чтобы не держать все сохранения в памяти. Возвращает путь к файлу."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}.jsonl.gz")
    tmp = path + ".part"
    names = set(tables) if tables else None
    with engine.connect() as conn, gzip.open(tmp, "wt", encoding="utf-8") as f:
        have = set(sa_inspect(conn).get_table_names())
        for t in Base.metadata.sorted_tables:
            if t.name not in have or (names is not None and t.name not in names):
                continue
            f.write(json.dumps({"table": t.name}, ensure_ascii=False) + "\n")
            for r in conn.execution_options(stream_results=True).execute(select(t)):
                f.write(json.dumps(dict(r._mapping), ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)                            # недописанный файл не выглядит готовой копией
    return path


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
    if IS_PG:
        # у SQLite выше уже есть полная копия файла; на PostgreSQL — выгрузка всех таблиц (сохранения, гильдии, вещи)
        made.append(dump_all("full"))
    # старые копии удаляем (копии перед вайпом — тоже по сроку, как полные)
    now = datetime.datetime.utcnow()
    for name in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, name)
        days = KEEP_FULL_DAYS if name.startswith("full-") else KEEP_DAYS
        if os.path.isfile(p) and datetime.datetime.utcfromtimestamp(os.path.getmtime(p)) < now - datetime.timedelta(days=days):
            os.remove(p)
    return made


async def send_to_admins(made):
    """Денежная выгрузка уходит администраторам в Telegram: копия переживёт даже потерю диска Railway.

    Получатели — по Telegram id (ADMIN_IDS и username, закреплённые за своим id при первом входе).
    Раньше искали по username в сохранениях: им мог оказаться другой человек, занявший старый ник админа,
    и тогда ему уходили балансы и адреса всех игроков."""
    from aiogram.types import FSInputFile
    import gram
    from webserver import admin_ids
    bot = gram.BOT.get("bot")
    money = [p for p in made if os.path.basename(p).startswith("money-")]
    ids = sorted(admin_ids())
    if not bot or not money:
        return
    if not ids:
        log.warning("Резервная копия не отправлена: нет id администраторов (задай ADMIN_IDS или зайди в игру админом)")
        return
    for chat in ids:
        try:
            await bot.send_document(chat, FSInputFile(money[0]), caption="🗄 Ежедневная резервная копия денежных таблиц MetalWar (GRAM, выводы, звёзды, реестр вещей, маркет, рефералы, призы сезона). Храни этот файл.")
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
