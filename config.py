import os
import sys


def _env_num(name, default, cast):
    """Число из переменной окружения. Пустое значение (переменная заведена в Railway, но не заполнена)
    или опечатка не роняют сервер: берётся значение по умолчанию и пишется предупреждение в лог."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return cast(default)
    try:
        return cast(float(raw.strip().replace(",", "."))) if cast is int else cast(raw.strip().replace(",", "."))
    except ValueError:
        print(f"[config] {name}={raw!r} — не число, использую {default}", file=sys.stderr)
        return cast(default)


def env_int(name, default):
    return _env_num(name, default, int)


def env_float(name, default):
    return _env_num(name, default, float)

# Токен бота задаётся в Railway → Variables → BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///robot_mmo.db")

# Адрес игры, который выдаст Railway (Settings → Networking → Generate Domain)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Порт веб-сервера: Railway передаёт его сам
PORT = env_int("PORT", 8080)

# Администраторы игры: Telegram-ники через запятую, без @
ADMIN_USERNAMES = {u.strip().lstrip("@").lower() for u in os.getenv("ADMIN_USERNAMES", "D0gEx0").split(",") if u.strip()}
# Модераторы: могут выдавать мут в чате, АВТО-бой доступен без VIP. Остальных прав админа у них нет.
MOD_USERNAMES = {u.strip().lstrip("@").lower() for u in os.getenv("MOD_USERNAMES", "yamakaschi,Yana_Ivanova66").split(",") if u.strip()}
# Надёжнее всего — по Telegram id (числа через запятую): username можно сменить, и его займёт другой человек.
# Если id не заданы, username из списков выше при первом входе «закрепляется» за своим id (см. webserver.role_ok).
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
MOD_IDS = {int(x) for x in os.getenv("MOD_IDS", "").replace(" ", "").split(",") if x.isdigit()}
# Сколько секунд действует initData (подпись Telegram при открытии игры). Украденная подпись — вход за игрока,
# поэтому не неделя, как было, а сутки. Дольше сессия не длится: игрок просто открывает игру заново.
INITDATA_MAX_AGE = max(3600, env_int("INITDATA_MAX_AGE", 86400))
# Сброс базы: сервер сносит все таблицы, когда эта метка меняется.
# Чтобы снова обнулить игру, поменяй WIPE_TOKEN в Railway → Variables (например на wipe-
# Эпоха 5 принудительно обнуляет прогресс всех игроков при следующем запуске,
# даже если в Railway уже задан WIPE_TOKEN.
SCHEMA_VERSION = "5"
WIPE_TOKEN = os.getenv("WIPE_TOKEN", "wipe-1")
DATA_EPOCH = f"{SCHEMA_VERSION}:{WIPE_TOKEN}"

# ---- GRAM (бывший Toncoin), сеть TON ----
# TON_NETWORK: testnet — тестовая сеть без реальных денег, mainnet — основная.
TON_NETWORK = os.getenv("TON_NETWORK", "testnet").strip().lower()
# Адрес кошелька игры, на который игроки переводят GRAM (сид-фраза на сервере НЕ нужна).
GAME_WALLET = os.getenv("GAME_WALLET", "").strip()
# Ключ API toncenter.com (бесплатный, из @tonapibot) — без него лимит 1 запрос в секунду.
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "").strip()
GRAM_WITHDRAW_MIN = env_float("GRAM_WITHDRAW_MIN", 1)
GRAM_WITHDRAW_FEE = env_float("GRAM_WITHDRAW_FEE", 0.10)

# Пополнение звёздами: 1 ⭐ = STAR_USD долларов (выплата Telegram разработчику), 1 GRAM = GRAM_USD долларов
STAR_USD = env_float("STAR_USD", 0.013)
GRAM_USD = env_float("GRAM_USD", 1.55)
STAR_PACKS = [50, 100, 250, 500, 1000, 2500]

# Имя окружения: production (боевой сервер) или staging (тестовый). На тестовом в игре видна метка.
ENV_NAME = os.getenv("ENV_NAME", "production")
# Папка для ежедневных резервных копий (на томе Railway)
BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")

# ---- Живой мир и нагрузка (см. docs/server-load-plan.md) ----
# Частота рассылки позиций. 10 — как было; 5 вдвое снижает трафик, если клиент плавно интерполирует.
WORLD_HZ = max(1, min(20, env_int("WORLD_HZ", 10)))
# Радиус видимости в пикселях мира: игрокам шлём только тех, кто ближе. 0 — всю локацию (как было).
VIEW_RADIUS = env_float("VIEW_RADIUS", 0)
# соседи на экране пропадают на этом множителе радиуса (появляются — на самом радиусе): без мигания на границе
VIEW_HYST = max(1.0, env_float("VIEW_HYST", 1.2))
# Ближняя зона: соседи ближе — 10 раз в секунду, дальние (их видно на миникарте) — раз в секунду.
# 1300 px больше экрана телефона и обычного окна на компьютере (1 px мира = 1 px экрана). 0 — все каждый шаг.
NEAR_RADIUS = env_float("NEAR_RADIUS", 1300)
# Сколько вкладок одного игрока держим одновременно
WS_MAX_PER_UID = env_int("WS_MAX_PER_UID", 3)
# Входящие сообщения с одного сокета: в среднем в секунду и допустимый всплеск
WS_IN_RATE = env_float("WS_IN_RATE", 60)
WS_IN_BURST = env_float("WS_IN_BURST", 120)
# Сколько секунд ждём авторизацию после открытия сокета
WS_AUTH_TIMEOUT = env_float("WS_AUTH_TIMEOUT", 5)
# Токен для /metrics. Пустой — эндпоинт выключен.
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "").strip()
# Нагрузочный тест: работает ТОЛЬКО на staging при LOADTEST=1. Боты используют выдуманные id от LOADTEST_UID_BASE
# и получают снятый предел уровня, чтобы проверять PvP. На production этот режим не включается никогда.
LOADTEST = ENV_NAME == "staging" and os.getenv("LOADTEST", "") == "1"
LOADTEST_UID_BASE = 9_100_000_000_000
