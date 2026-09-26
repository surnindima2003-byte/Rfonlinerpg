import logging
import os

from sqlalchemy import Boolean, create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from config import DB_URL
from models import Base

log = logging.getLogger("db")

# Если в Railway подключена PostgreSQL, она сама передаёт DATABASE_URL — тогда работаем с ней.
# Иначе — SQLite-файл на томе /data, как раньше.
PG_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_URL = DB_URL.replace("+aiosqlite", "")
IS_PG = PG_URL.startswith(("postgres://", "postgresql://"))

if IS_PG:
    URL = PG_URL.replace("postgres://", "postgresql+psycopg2://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
else:
    engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
_Session = sessionmaker(engine, expire_on_commit=False)


class AsyncLikeSession:
    """Обёртка, чтобы остальной код (async with / await) работал без изменений."""

    def __init__(self):
        self._s = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *exc):
        if exc_type:
            self._s.rollback()          # при ошибке ничего не записываем наполовину
        self._s.close()

    async def execute(self, stmt):
        return self._s.execute(stmt)

    def add(self, obj):
        self._s.add(obj)

    async def commit(self):
        self._s.commit()


def SessionLocal():
    return AsyncLikeSession()


def migrate_sqlite_to_pg():
    """Один раз переносит все таблицы из старого SQLite-файла в PostgreSQL."""
    path = SQLITE_URL.replace("sqlite:///", "", 1)
    if not IS_PG or not os.path.exists(path):
        return
    with engine.begin() as pg:
        done = pg.execute(text("SELECT value FROM meta WHERE key = 'migrated_from_sqlite'")).first()
        if done:
            return
    old = create_engine(SQLITE_URL)
    old_tables = set(inspect(old).get_table_names())
    moved = 0
    with old.connect() as src, engine.begin() as dst:
        for table in Base.metadata.sorted_tables:
            if table.name not in old_tables:
                continue
            if dst.execute(select(table).limit(1)).first():
                continue                                   # в новой базе уже есть данные — не трогаем
            cols = {c["name"] for c in inspect(old).get_columns(table.name)}
            rows = [dict(r._mapping) for r in src.execute(text(f'SELECT * FROM "{table.name}"'))]
            rows = [{k: v for k, v in r.items() if k in cols and k in table.c} for r in rows]
            bools = [c.name for c in table.c if isinstance(c.type, Boolean)]
            for r in rows:                                 # SQLite хранит 0/1, PostgreSQL ждёт true/false
                for b in bools:
                    if b in r and r[b] is not None:
                        r[b] = bool(r[b])
            if rows:
                dst.execute(table.insert(), rows)
                moved += len(rows)
            # счётчики автонумерации продолжают с последнего номера
            if "id" in table.c and table.c.id.autoincrement and rows:
                dst.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), (SELECT COALESCE(MAX(id), 1) FROM \"{table.name}\"))"))
        dst.execute(text("INSERT INTO meta (key, value) VALUES ('migrated_from_sqlite', '1')"))
    log.warning("Перенос SQLite → PostgreSQL завершён: %s строк", moved)


async def init_db():
    Base.metadata.create_all(engine)
    migrate_sqlite_to_pg()
    log.info("База данных: %s", "PostgreSQL" if IS_PG else "SQLite")
