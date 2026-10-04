"""Общие помощники для обработчиков бота."""
import logging
import time

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

log = logging.getLogger("bot")


async def edit_or_send(callback: CallbackQuery, text, reply_markup=None):
    """Заменить текст сообщения с кнопками, а если нельзя — отправить новое.

    * Telegram отвечает ошибкой «message is not modified», если текст тот же (например, тот же результат рейда);
    * сообщения старше 48 часов бот редактировать не может (callback.message — InaccessibleMessage).
    Раньше в обоих случаях обработчик падал, а игрок не видел ответа."""
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.info("edit_text не удался (%s), отправляю новое сообщение", e)
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=reply_markup)


_last = {}


def too_fast(key, uid, gap):
    """True, если игрок нажимает чаще, чем раз в gap секунд (кнопки рейда можно было жать без остановки)."""
    now = time.monotonic()
    k = (key, uid)
    if now - _last.get(k, 0) < gap:
        return True
    _last[k] = now
    if len(_last) > 50_000:
        _last.clear()
    return False
