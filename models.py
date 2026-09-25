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
    # поля для рейтинга (берутся из сохранения)
    nick: Mapped[str] = mapped_column(String(32), default="")
    lvl: Mapped[int] = mapped_column(Integer, default=1)
    cls: Mapped[str] = mapped_column(String(16), default="")
    bm: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    guild_id: Mapped[str] = mapped_column(String(64), default="")


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


class Doc(Base):
    """Документы гильдий: guilds/{id}, guilds/{id}/members/{uid}, .../requests/{uid}, .../log/{id}."""
    __tablename__ = "docs"

    path: Mapped[str] = mapped_column(String(256), primary_key=True)
    col: Mapped[str] = mapped_column(String(200), index=True)
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated: Mapped[int] = mapped_column(BigInteger, default=0)


class Meta(Base):
    """Служебные значения, например номер «эпохи» базы после сброса."""
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class MarketLot(Base):
    """Лот маркета: предмет продавца, выставленный за лом."""
    __tablename__ = "market_lots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    seller_id: Mapped[int] = mapped_column(BigInteger, index=True)
    seller_nick: Mapped[str] = mapped_column(String(32), default="")
    item: Mapped[str] = mapped_column(Text, default="{}")
    price: Mapped[int] = mapped_column(BigInteger, default=0)
    created: Mapped[int] = mapped_column(BigInteger, default=0)


class MarketHist(Base):
    """История сделок: покупки и продажи игрока."""
    __tablename__ = "market_hist"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(8))
    item: Mapped[str] = mapped_column(Text, default="{}")
    price: Mapped[int] = mapped_column(BigInteger, default=0)
    other: Mapped[str] = mapped_column(String(40), default="")
    ts: Mapped[int] = mapped_column(BigInteger, default=0)


class GramWallet(Base):
    """Серверный баланс GRAM игрока в нано-GRAM (1 GRAM = 1 000 000 000)."""
    __tablename__ = "gram_wallets"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memo: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    spent: Mapped[int] = mapped_column(BigInteger, default=0)       # потрачено в магазине — для VIP
    created: Mapped[int] = mapped_column(BigInteger, default=0)


class GramTx(Base):
    """Журнал всех движений GRAM. ref уникален — один перевод не зачисляется дважды."""
    __tablename__ = "gram_tx"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(BigInteger, default=0)      # со знаком: + зачисление, − списание
    ref: Mapped[str] = mapped_column(String(160), unique=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    ts: Mapped[int] = mapped_column(BigInteger, default=0)


class GramWithdrawal(Base):
    """Заявка на вывод. Сумма списывается сразу, выплату подтверждает администратор."""
    __tablename__ = "gram_withdrawals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    nick: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(80))
    amount: Mapped[int] = mapped_column(BigInteger)                 # списано с баланса
    payout: Mapped[int] = mapped_column(BigInteger)                 # к выплате после комиссии
    status: Mapped[str] = mapped_column(String(12), default="pending")
    tx_hash: Mapped[str] = mapped_column(String(120), default="")
    created: Mapped[int] = mapped_column(BigInteger, default=0)
    updated: Mapped[int] = mapped_column(BigInteger, default=0)
