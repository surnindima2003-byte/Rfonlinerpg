"""Оплата звёздами Telegram: подтверждение счёта и зачисление GRAM после успешной оплаты."""
import logging
from aiogram import Router, F
from aiogram.types import PreCheckoutQuery, Message

import gram
from config import STAR_PACKS

router = Router()
log = logging.getLogger("stars")


def parse_payload(payload):
    """payload вида stars:<tg_id>:<количество звёзд>"""
    try:
        kind, uid, stars = payload.split(":")
        return (int(uid), int(stars)) if kind == "stars" else (None, None)
    except (ValueError, AttributeError):
        return None, None


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    uid, stars = parse_payload(q.invoice_payload)
    ok = uid == q.from_user.id and stars in STAR_PACKS and q.currency == "XTR" and q.total_amount == stars
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел, открой пополнение заново")


@router.message(F.successful_payment)
async def paid(message: Message):
    p = message.successful_payment
    uid, stars = parse_payload(p.invoice_payload)
    if p.currency != "XTR" or uid != message.from_user.id or stars != p.total_amount:
        log.warning("Странный платёж %s от %s", p.telegram_payment_charge_id, message.from_user.id)
        return
    nano = await gram.credit_stars(uid, stars, p.telegram_payment_charge_id)
    if nano:
        await message.answer(f"⭐ Оплата получена: +{gram.g(nano)} GRAM на игровой баланс MetalWar.")
