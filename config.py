import os

# Токен бота задаётся в Railway → Variables → BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///robot_mmo.db")

# Адрес игры, который выдаст Railway (Settings → Networking → Generate Domain)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Порт веб-сервера: Railway передаёт его сам
PORT = int(os.getenv("PORT", "8080"))
