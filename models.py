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
        # Сохранение принимается только в той эпохе, в которой оно было создано.
    # Это не даёт старой вкладке вернуть инвентарь после полного сброса базы.
    epoch: Mapped[str] = mapped_column(String(96), default="", index=True)
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
    locked: Mapped[int] = mapped_column(BigInteger, default=0)      # GRAM из звёзд: тратить можно, выводить нельзя
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


class Referral(Base):
    """Кто пригласил игрока (один пригласивший на игрока, навсегда)."""
    __tablename__ = "referrals"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created: Mapped[int] = mapped_column(BigInteger, default=0)


class RefEarn(Base):
    """Реферальные начисления: 5% с покупок друга (уровень 1), 2% с покупок друзей друзей (уровень 2)."""
    __tablename__ = "ref_earn"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    friend_id: Mapped[int] = mapped_column(BigInteger, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    ts: Mapped[int] = mapped_column(BigInteger, default=0)


class StarPayment(Base):
    """Оплата звёздами. charge_id уникален: один платёж зачисляется один раз, по нему же делается возврат."""
    __tablename__ = "star_payments"

    charge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    gram: Mapped[int] = mapped_column(BigInteger, default=0)
    ts: Mapped[int] = mapped_column(BigInteger, default=0)


class ItemInst(Base):
    """Зарегистрированная сервером вещь: выпала по решению сервера или куплена за GRAM. Только такие продаются за GRAM."""
    __tablename__ = "item_inst"

    uid: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner: Mapped[int] = mapped_column(BigInteger, index=True)
    item: Mapped[str] = mapped_column(String(24))
    g: Mapped[int] = mapped_column(Integer, default=0)
    e: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(8), default="inv", index=True)   # inv | market | gone
    source: Mapped[str] = mapped_column(String(12), default="drop")
    created: Mapped[int] = mapped_column(BigInteger, default=0)


class SphereBal(Base):
    """Сколько сфер заточки у игрока выпало по решению сервера (их можно продавать за GRAM)."""
    __tablename__ = "sphere_bal"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner: Mapped[int] = mapped_column(BigInteger, index=True)
    item: Mapped[str] = mapped_column(String(12))
    n: Mapped[int] = mapped_column(Integer, default=0)


class PvpStat(Base):
    """PvP: победы, поражения, карма (убийства невиновных) и рейтинг."""
    __tablename__ = "pvp_stats"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    deaths: Mapped[int] = mapped_column(Integer, default=0)
    pk: Mapped[int] = mapped_column(Integer, default=0)            # убито невиновных
    karma: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[int] = mapped_column(Integer, default=1000)


class FirstSeen(Base):
    """Когда игрок впервые зашёл — для статистики новых игроков и удержания."""
    __tablename__ = "first_seen"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, default=0, index=True)


class ClientError(Base):
    """Ошибки, случившиеся у игроков в игре: одинаковые складываются в одну запись со счётчиком."""
    __tablename__ = "client_errors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    msg: Mapped[str] = mapped_column(Text, default="")
    stack: Mapped[str] = mapped_column(Text, default="")
    ua: Mapped[str] = mapped_column(String(200), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)
    users: Mapped[int] = mapped_column(Integer, default=0)
    first: Mapped[int] = mapped_column(BigInteger, default=0)
    last: Mapped[int] = mapped_column(BigInteger, default=0)


class ServerProg(Base):
    """Опыт, посчитанный сервером по подтверждённым убийствам. Уровень для PvP и рейтинга — отсюда."""
    __tablename__ = "server_prog"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exp: Mapped[int] = mapped_column(BigInteger, default=0)
    base_lvl: Mapped[int] = mapped_column(Integer, default=1)     # уровень, набранный до начала учёта
    bonus: Mapped[int] = mapped_column(Integer, default=0)        # уровни, выданные админом


class LootDay(Base):
    """Сколько убийств за сегодняшние сутки (UTC) засчитано игроку: после порога ценный лут выпадает реже."""
    __tablename__ = "loot_day"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, default=0)          # номер суток от 1970-01-01 (UTC)
    kills: Mapped[int] = mapped_column(Integer, default=0)        # взвешенно: главарь считается за 10


class FunnelEvent(Base):
    """Первый раз, когда игрок дошёл до шага воронки новичка (один раз на шаг)."""
    __tablename__ = "funnel_events"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    step: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, default=0, index=True)


class FactionLock(Base):
    """Фракция игрока, закреплённая сервером при первом входе в живой мир. Сменить её, подменив сообщение, нельзя."""
    __tablename__ = "faction_lock"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fac: Mapped[str] = mapped_column(String(8))
    ts: Mapped[int] = mapped_column(BigInteger, default=0)


class SeasonPts(Base):
    """Очки сезона игрока: начисляет только сервер (от них зависят призы рейтинга)."""
    __tablename__ = "season_pts"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    season: Mapped[str] = mapped_column(String(16), primary_key=True)    # «2026-10» — сезон длится месяц
    pts: Mapped[int] = mapped_column(Integer, default=0, index=True)
    data: Mapped[str] = mapped_column(Text, default="{}")              # счётчики заданий дня, недели и постоянных
    updated: Mapped[int] = mapped_column(Integer, default=0)


class SeasonPrize(Base):
    """Призы рейтинга сезона: фиксируются после конца сезона, выплачиваются в GRAM после подтверждения админа."""
    __tablename__ = "season_prizes"

    season: Mapped[str] = mapped_column(String(16), primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    place: Mapped[int] = mapped_column(Integer, default=0)
    pts: Mapped[int] = mapped_column(Integer, default=0)
    nick: Mapped[str] = mapped_column(String(40), default="")
    usdt: Mapped[int] = mapped_column(Integer, default=0)
    nano: Mapped[int] = mapped_column(BigInteger, default=0)          # сумма в GRAM (нано) по курсу на момент итогов
    status: Mapped[str] = mapped_column(String(12), default="wait")   # wait → approved (админ) → claimed (игрок)
    created: Mapped[int] = mapped_column(Integer, default=0)
