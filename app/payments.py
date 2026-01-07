# app/payments.py
"""
Модуль для работы с платежами через API ЮKassa.
Поддерживает СБП и другие способы оплаты.
"""
import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import uuid
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

try:
    from yookassa import Configuration, Payment
    YOOKASSA_AVAILABLE = True
except ImportError:
    YOOKASSA_AVAILABLE = False
    logging.warning("⚠️ Библиотека yookassa не установлена. Установите: pip install yookassa")

# Загружаем переменные окружения
YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID')
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY')

# Настраиваем ЮKassa при импорте модуля
if YOOKASSA_AVAILABLE and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY
    
    # Проверяем, тестовый или боевой режим
    # Тестовые ключи обычно начинаются с "test_" или содержат "test"
    is_test_mode = "test" in YOOKASSA_SECRET_KEY.lower() or "test" in YOOKASSA_SHOP_ID.lower()
    
    if is_test_mode:
        logging.info("✅ ЮKassa настроен (ТЕСТОВЫЙ режим)")
        logging.warning("⚠️ В тестовом режиме СБП может быть недоступен. Для СБП используйте боевые ключи.")
    else:
        logging.info("✅ ЮKassa настроен (БОЕВОЙ режим)")
elif YOOKASSA_AVAILABLE:
    logging.warning("⚠️ ЮKassa не настроен: отсутствуют YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY")
else:
    logging.warning("⚠️ ЮKassa недоступен: библиотека не установлена")


async def create_payment(
    user_id: int,
    amount: float,
    description: str,
    subscription_type: str,
    subscription_days: int,
    return_url: Optional[str] = None,
    bot_username: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Создать платеж через API ЮKassa.
    
    Args:
        user_id: ID пользователя Telegram
        amount: Сумма платежа в рублях (например, 99.0)
        description: Описание платежа
        subscription_type: Тип подписки ("1month" или "3months")
        subscription_days: Количество дней подписки (30 или 90)
        return_url: URL для возврата после оплаты (опционально)
    
    Returns:
        Словарь с информацией о платеже (id, confirmation_url) или None при ошибке
    """
    if not YOOKASSA_AVAILABLE:
        logging.error("❌ Библиотека yookassa не установлена")
        return None
    
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logging.error("❌ ЮKassa не настроен: отсутствуют учетные данные")
        return None
    
    try:
        # Создаем уникальный idempotence_key для предотвращения дублирования платежей
        idempotence_key = str(uuid.uuid4())
        
        # Формируем payload для идентификации платежа
        payload = f"premium_{subscription_type}_{user_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # Создаем платеж
        # Если не указывать payment_method_data, на странице оплаты будут доступны
        # все способы оплаты, активированные в личном кабинете ЮKassa (карты, СБП и др.)
        payment = Payment.create({
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": return_url or (f"https://t.me/{bot_username}?start=payment_success" if bot_username else "https://t.me")
            },
            "capture": True,  # Автоматическое списание средств
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "subscription_type": subscription_type,
                "subscription_days": str(subscription_days),
                "payload": payload
            }
        }, idempotence_key)
        
        payment_id = payment.id
        confirmation_url = payment.confirmation.confirmation_url
        status = payment.status
        
        logging.info(
            f"✅ Платеж создан: payment_id={payment_id}, "
            f"user_id={user_id}, amount={amount}, status={status}"
        )
        
        return {
            "payment_id": payment_id,
            "confirmation_url": confirmation_url,
            "status": status,
            "payload": payload,
            "amount": amount,
            "subscription_type": subscription_type,
            "subscription_days": subscription_days
        }
        
    except Exception as e:
        logging.error(f"❌ Ошибка при создании платежа через ЮKassa: {e}", exc_info=True)
        return None


async def get_payment_status(payment_id: str) -> Optional[Dict[str, Any]]:
    """
    Получить статус платежа по ID.
    
    Args:
        payment_id: ID платежа в ЮKassa
    
    Returns:
        Словарь с информацией о платеже или None при ошибке
    """
    if not YOOKASSA_AVAILABLE:
        return None
    
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return None
    
    try:
        payment = Payment.find_one(payment_id)
        
        return {
            "payment_id": payment.id,
            "status": payment.status,
            "amount": float(payment.amount.value),
            "currency": payment.amount.currency,
            "metadata": payment.metadata if hasattr(payment, 'metadata') else {},
            "paid": payment.paid if hasattr(payment, 'paid') else False,
            "created_at": payment.created_at if hasattr(payment, 'created_at') else None
        }
    except Exception as e:
        logging.error(f"❌ Ошибка при получении статуса платежа {payment_id}: {e}", exc_info=True)
        return None


def is_yookassa_configured() -> bool:
    """Проверить, настроен ли ЮKassa."""
    return (
        YOOKASSA_AVAILABLE and 
        YOOKASSA_SHOP_ID is not None and 
        YOOKASSA_SECRET_KEY is not None
    )


async def check_pending_payments() -> list:
    """
    Получить список pending платежей из БД для проверки их статуса.
    
    Returns:
        Список словарей с информацией о pending платежах (yookassa_payment_id, user_id)
    """
    import aiosqlite
    from app.storage import DB_PATH
    
    pending_payments = []
    
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT yookassa_payment_id, user_id
                FROM payments
                WHERE status = 'pending' AND yookassa_payment_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 50
            """) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    pending_payments.append({
                        "payment_id": row["yookassa_payment_id"],
                        "user_id": row["user_id"]
                    })
    except Exception as e:
        logging.error(f"❌ Ошибка при получении pending платежей: {e}", exc_info=True)
    
    return pending_payments

