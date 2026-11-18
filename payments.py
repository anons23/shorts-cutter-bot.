"""Оплата Премиума — пока заглушка.

Чтобы подключить оплату, нужно:
  1. выбрать способ (например, звёзды Telegram или ЮKassa) и вписать его в PAYMENT_PROVIDER;
  2. реализовать для него start_payment() и обработку успешного платежа в bot.py.
Пока PAYMENT_PROVIDER пустой, кнопки «Купить» показывают тарифы и сообщают, что оплата скоро будет.
Каждое нажатие записывается в статистику (/stats) — так видно, сколько людей готовы платить.
"""
import os

PROVIDER = os.getenv("PAYMENT_PROVIDER", "").strip().lower()
CURRENCY = os.getenv("PRICE_CURRENCY", "₽")
IMPLEMENTED: set[str] = set()  # сюда добавим "stars", "yookassa" и т.п., когда реализуем


def _plans() -> list[tuple[int, int]]:
    """PREMIUM_PLANS=7:149,30:399 -> [(7, 149), (30, 399)] — дней и цена."""
    plans = []
    for part in os.getenv("PREMIUM_PLANS", "7:149,30:399").split(","):
        try:
            days, price = part.split(":")
            plans.append((int(days), int(price)))
        except ValueError:
            continue
    return plans or [(30, 399)]


PLANS = _plans()


def ready() -> bool:
    return PROVIDER in IMPLEMENTED


def plan_title(days: int, price: int) -> str:
    period = {7: "неделя", 30: "месяц", 90: "3 месяца", 365: "год"}.get(days, f"{days} дн.")
    return f"{period} — {price} {CURRENCY}"


async def start_payment(bot, chat_id: int, uid: int, days: int, price: int) -> None:
    """Выставить счёт. Реализуем, когда выберем способ оплаты."""
    raise NotImplementedError(f"Способ оплаты «{PROVIDER}» ещё не реализован")
