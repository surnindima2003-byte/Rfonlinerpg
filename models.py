from sqlalchemy import BigInteger, String, Integer, Text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    faction: Mapped[str] = mapped_column(String(32), default="")

    level: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)

    hp: Mapped[int] = mapped_column(Integer, default=50)
    max_hp: Mapped[int] = mapped_column(Integer, default=50)
    attack: Mapped[int] = mapped_column(Integer, default=6)
    defense: Mapped[int] = mapped_column(Integer, default=2)

    scrap: Mapped[int] = mapped_column(Integer, default=0)
    energy_core: Mapped[int] = mapped_column(Integer, default=0)

    current_zone: Mapped[str] = mapped_column(String(32), default="scrapfields")


class GameSave(Base):
    """Прогресс игрока из мини-приложения (JSON), привязан к Telegram ID."""
    __tablename__ = "game_saves"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[str] = mapped_column(Text, default="")
    updated: Mapped[int] = mapped_column(BigInteger, default=0)


class Grant(Base):
    """Выдача от администратора: применяется в игре при следующем входе или сразу, если игрок онлайн."""
    __tablename__ = "grants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    by_admin: Mapped[str] = mapped_column(String(64), default="")
    created: Mapped[int] = mapped_column(BigInteger, default=0)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
