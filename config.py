import os

# Вставь сюда токен, полученный от @BotFather, либо задай переменную окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///robot_mmo.db")
