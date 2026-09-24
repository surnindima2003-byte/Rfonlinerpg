from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DB_URL
from models import Base

# Синхронный режим SQLAlchemy: ему не нужна библиотека greenlet
SYNC_URL = DB_URL.replace("+aiosqlite", "")
engine = create_engine(SYNC_URL, connect_args={"check_same_thread": False})
_Session = sessionmaker(engine, expire_on_commit=False)


class AsyncLikeSession:
    """Обёртка, чтобы остальной код (async with / await) работал без изменений."""

    def __init__(self):
        self._s = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._s.close()

    async def execute(self, stmt):
        return self._s.execute(stmt)

    def add(self, obj):
        self._s.add(obj)

    async def commit(self):
        self._s.commit()


def SessionLocal():
    return AsyncLikeSession()


async def init_db():
    Base.metadata.create_all(engine)
