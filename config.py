import os

# Токен бота задаётся в Railway → Variables → BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///robot_mmo.db")

# Адрес игры, который выдаст Railway (Settings → Networking → Generate Domain)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Порт веб-сервера: Railway передаёт его сам
PORT = int(os.getenv("PORT", "8080"))

# Администраторы игры: Telegram-ники через запятую, без @
ADMIN_USERNAMES = {u.strip().lstrip("@").lower() for u in os.getenv("ADMIN_USERNAMES", "D0gEx0").split(",") if u.strip()}

# Сброс базы: сервер сносит все таблицы, когда эта метка меняется.
# Чтобы снова обнулить игру, поменяй WIPE_TOKEN в Railway → Variables (например на wipe-2).
SCHEMA_VERSION = "3"
WIPE_TOKEN = os.getenv("WIPE_TOKEN", "wipe-1")
DATA_EPOCH = f"{SCHEMA_VERSION}:{WIPE_TOKEN}"
