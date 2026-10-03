import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import Boolean, create_engine, event, inspect, literal, select, text
from sqlalchemy.orm import sessionmaker

import metrics
from config import DB_URL

from models import Base
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

log = logging.getLogger("db")

# Если в Railway подключена PostgreSQL, она сама передаёт DATABASE_URL — тогда работаем с ней.
# Иначе — SQLite-файл на томе /data, как раньше.
PG_URL = os.getenv("DATABASE_URL", "").strip()
if not PG_URL and os.getenv("PGHOST") and os.getenv("PGPASSWORD"):
    # новые шаблоны PostgreSQL в Railway не создают DATABASE_URL — собираем адрес из PGHOST/PGUSER/…
    from urllib.parse import quote
    PG_URL = "postgresql://{}:{}@{}:{}/{}".format(quote(os.getenv("PGUSER", "postgres"), safe=""), quote(os.getenv("PGPASSWORD"), safe=""),
                                                   os.getenv("PGHOST"), os.getenv("PGPORT", "5432"), os.getenv("PGDATABASE", "railway"))
SQLITE_URL = DB_URL.replace("+aiosqlite", "")
IS_PG = PG_URL.startswith(("postgres://", "postgresql://"))

# Пул соединений. Значения стартовые — подбираются по метрикам /metrics (sql.*), а не «на всякий случай больше».
POOL_SIZE = env_int("DB_POOL_SIZE", 10)
MAX_OVERFLOW = env_int("DB_MAX_OVERFLOW", 10)
POOL_TIMEOUT = env_float("DB_POOL_TIMEOUT", 3)
STATEMENT_TIMEOUT_MS = env_int("DB_STATEMENT_TIMEOUT_MS", 5000)

if IS_PG:
    URL = PG_URL.replace("postgres://", "postgresql+psycopg2://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(
        URL, pool_pre_ping=True, pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT, pool_recycle=1800,
        # зависший запрос не держит соединение вечно
        connect_args={"connect_timeout": 5, "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout=3000"},
    )
else:
    engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False, "timeout": 5},
                           pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW, pool_timeout=POOL_TIMEOUT)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WAL: чтение не ждёт запись. Для разработки; под нагрузкой — только PostgreSQL.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

_Session = sessionmaker(engine, expire_on_commit=False)

# Отдельные потоки для базы: запрос ждёт ответа PostgreSQL в потоке, а event loop
# в это время продолжает обслуживать чат, удары и heartbeat всех игроков.
_db_pool = ThreadPoolExecutor(max_workers=POOL_SIZE + MAX_OVERFLOW, thread_name_prefix="db")
_inflight = 0


async def run_db(fn, *args, label="sql"):
    """Выполнить синхронную функцию базы в потоке, с метриками."""
    global _inflight
    _inflight += 1
    metrics.gauge("sql.inflight", _inflight)
    t0 = asyncio.get_running_loop().time()
    try:
        return await asyncio.get_running_loop().run_in_executor(_db_pool, lambda: fn(*args))
    except Exception:
        metrics.inc("sql.errors")
        raise
    finally:
        _inflight -= 1
        metrics.observe(label, (asyncio.get_running_loop().time() - t0) * 1000)


def _buffered(result):
    """Забираем строки прямо в потоке базы, чтобы чтение результата не ходило в сеть из event loop."""
    try:
        if result.returns_rows:
            return result.freeze()()
    except Exception:
        pass
    return result


class AsyncLikeSession:
    """Прежний интерфейс (async with / await s.execute / s.add / await s.commit), но запросы
    выполняются в потоках базы и больше не блокируют event loop."""

    def __init__(self):
        self._s = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *exc):
        s = self._s

        def finish():
            if exc_type:
                s.rollback()          # при ошибке ничего не записываем наполовину
            s.close()
        await run_db(finish, label="sql.close")

    async def execute(self, stmt, *args, **kw):
        s = self._s
        return await run_db(lambda: _buffered(s.execute(stmt, *args, **kw)), label="sql")

    async def scalar(self, stmt, *args, **kw):
        s = self._s
        return await run_db(lambda: s.scalar(stmt, *args, **kw), label="sql")

    async def get(self, entity, ident):
        s = self._s
        return await run_db(lambda: s.get(entity, ident), label="sql")

    def add(self, obj):
        self._s.add(obj)

    def add_all(self, objs):
        self._s.add_all(objs)

    async def delete(self, obj):
        s = self._s
        await run_db(lambda: s.delete(obj), label="sql")

    async def flush(self):
        await run_db(self._s.flush, label="sql")

    async def commit(self):
        await run_db(self._s.commit, label="sql.commit")

    async def rollback(self):
        await run_db(self._s.rollback, label="sql")


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


def _sql_literal(value):
    return str(literal(value).compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True}))


def add_missing_columns():
    """Добавляет в существующие таблицы колонки, которые появились в models.py после их создания.

    create_all создаёт только новые таблицы, а в старые колонки не дописывает — отсюда
    ошибки вида «no such column: game_saves.epoch». Данные не удаляются.
    """
    from config import DATA_EPOCH
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = 0
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if col.primary_key:
                    # колонку первичного ключа так не добавить — нужна пересборка таблицы
                    log.error("Миграция: %s.%s входит в первичный ключ, автоматически не добавлена", table.name, col.name)
                    continue
                if col.name == "epoch":
                    # старые сохранения продолжают жить в текущей эпохе; для вайпа задай LEGACY_EPOCH=<старая эпоха>
                    value = os.getenv("LEGACY_EPOCH", DATA_EPOCH)
                elif col.default is not None and getattr(col.default, "is_scalar", False):
                    value = col.default.arg
                else:
                    value = None
                ddl = col.type.compile(dialect=engine.dialect)
                default = f" DEFAULT {_sql_literal(value)}" if value is not None else ""
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{default}'))
                if value is not None:
                    conn.execute(text(f'UPDATE "{table.name}" SET "{col.name}" = :v WHERE "{col.name}" IS NULL'), {"v": value})
                log.warning("Миграция: добавлена колонка %s.%s (значение для старых строк: %r)", table.name, col.name, value)
                added += 1
        for table in Base.metadata.sorted_tables:
            if table.name in tables:
                for idx in table.indexes:
                    idx.create(conn, checkfirst=True)
    return added


async def init_db():
    await run_db(Base.metadata.create_all, engine, label="sql.migrate")
    await run_db(add_missing_columns, label="sql.migrate")
    await run_db(migrate_sqlite_to_pg, label="sql.migrate")
    log.warning("База данных: %s (пул %s+%s)", "PostgreSQL" if IS_PG else "SQLite", POOL_SIZE, MAX_OVERFLOW)
    # подсказка, почему не подключилась PostgreSQL (пароль из адреса в лог не пишем)
    if IS_PG:
        log.warning("PostgreSQL: хост %s", PG_URL.split("@")[-1].split("/")[0])
    elif PG_URL:
        log.warning("DATABASE_URL задан, но это не адрес PostgreSQL (начинается с «%s…»). Нужно ${{Postgres.DATABASE_URL}}", PG_URL[:10])
    else:
        log.warning("DATABASE_URL пустой — сервер на SQLite. В Railway: Variables → DATABASE_URL = ${{Postgres.DATABASE_URL}} → Deploy")
    if not IS_PG and os.getenv("ENV_NAME", "production") == "production":
        log.warning("Боевой сервер на SQLite: при 100 игроках нужна PostgreSQL (подключи её в Railway)")


async def db_ping(timeout=2.0):
    """Для /ready: база отвечает."""
    def ping():
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    try:
        await asyncio.wait_for(run_db(ping, label="sql.ping"), timeout)
        return True
    except Exception:
        return False


def dispose():
    engine.dispose()
    _db_pool.shutdown(wait=False, cancel_futures=True)
