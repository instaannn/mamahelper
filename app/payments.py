# app/payments.py
"""
Модуль для работы с платежами через API ЮKassa.
Поддерживает СБП и другие способы оплаты.
"""
import logging
import os
import aiosqlite
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
    bot_username: Optional[str] = None,
    customer_phone: Optional[str] = None,
    customer_email: Optional[str] = None
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
        # Проверяем режим работы (тестовый/боевой)
        is_test_mode = "test" in YOOKASSA_SECRET_KEY.lower() or "test" in YOOKASSA_SHOP_ID.lower()
        if is_test_mode:
            logging.warning(f"⚠️ Создание платежа в ТЕСТОВОМ режиме. СБП недоступен в тестовом режиме!")
        else:
            logging.info(f"✅ Создание платежа в БОЕВОМ режиме. СБП должен быть доступен.")
        
        # Создаем уникальный idempotence_key для предотвращения дублирования платежей
        idempotence_key = str(uuid.uuid4())
        
        # Формируем payload для идентификации платежа
        payload = f"premium_{subscription_type}_{user_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # Формируем return_url
        final_return_url = return_url
        if not final_return_url and bot_username:
            final_return_url = f"https://t.me/{bot_username}?start=payment_success"
        elif not final_return_url:
            final_return_url = "https://t.me"
        
        logging.info(f"🔗 Return URL для платежа: {final_return_url}")
        
        # Создаем платеж
        # Если не указывать payment_method_data, на странице оплаты будут доступны
        # все способы оплаты, активированные в личном кабинете ЮKassa (карты, СБП и др.)
        payment_data = {
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": final_return_url
            },
            "capture": True,  # Автоматическое списание средств
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "subscription_type": subscription_type,
                "subscription_days": str(subscription_days),
                "payload": payload
            }
        }
        
        # Если есть номер телефона или email пользователя, добавляем receipt для формирования чека
        # Это необходимо, если библиотека yookassa автоматически добавляет receipt при включенной онлайн-кассе
        # Если receipt указан, customer должен содержать валидный email или phone
        if customer_phone or customer_email:
            receipt_customer = {}
            if customer_phone:
                # Форматируем номер телефона (должен начинаться с +7 для России)
                phone = customer_phone.strip()
                if not phone.startswith("+"):
                    if phone.startswith("8"):
                        phone = "+7" + phone[1:]
                    elif phone.startswith("7"):
                        phone = "+" + phone
                    else:
                        phone = "+7" + phone
                receipt_customer["phone"] = phone
            if customer_email:
                receipt_customer["email"] = customer_email.strip()
            
            if receipt_customer:
                payment_data["receipt"] = {
                    "customer": receipt_customer,
                    "items": [
                        {
                            "description": description[:128],  # Максимум 128 символов
                            "quantity": "1.00",
                            "amount": {
                                "value": f"{amount:.2f}",
                                "currency": "RUB"
                            },
                            "vat_code": 1  # НДС 20%
                        }
                    ],
                    "tax_system_code": 1  # Общая система налогообложения
                }
                logging.info(f"📝 Receipt добавлен с customer: {receipt_customer}")
        else:
            # Если нет phone/email, не добавляем receipt
            # ЮKassa автоматически запросит email на странице оплаты, если онлайн-касса настроена
            logging.info(f"💡 Receipt не добавлен (нет phone/email). ЮKassa запросит email автоматически.")
        
        logging.info(f"📋 Данные платежа: amount={amount} RUB, description={description}")
        logging.info(f"💡 Примечание: payment_method_data не указан - будут доступны все способы оплаты из личного кабинета ЮKassa")
        
        # Логируем payment_data для отладки (без секретных данных)
        import json
        payment_data_str = json.dumps(payment_data, ensure_ascii=False, indent=2)
        logging.info(f"🔍 Payment data перед отправкой:\n{payment_data_str}")
        
        # Убеждаемся, что receipt НЕ указан в payment_data
        if "receipt" in payment_data:
            logging.warning(f"⚠️ ВНИМАНИЕ: receipt найден в payment_data! Удаляем его.")
            del payment_data["receipt"]
            logging.info(f"🔍 Payment data после удаления receipt:\n{json.dumps(payment_data, ensure_ascii=False, indent=2)}")
        
        try:
            payment = Payment.create(payment_data, idempotence_key)
        except ValueError as ve:
            # Если ошибка связана с receipt, логируем подробности
            if "receipt" in str(ve).lower() or "customer" in str(ve).lower():
                logging.error(f"❌ Ошибка валидации receipt/customer. Payment data: {payment_data_str}")
                logging.error(f"❌ Возможно, библиотека yookassa автоматически добавляет receipt. Проверьте настройки ЮKassa.")
            raise
        
        payment_id = payment.id
        confirmation_url = payment.confirmation.confirmation_url
        status = payment.status
        
        # Логируем информацию о доступных способах оплаты (если доступно)
        try:
            if hasattr(payment, 'payment_method') and payment.payment_method:
                logging.info(f"💳 Способ оплаты платежа: {payment.payment_method}")
            if hasattr(payment, 'available_payment_methods') and payment.available_payment_methods:
                logging.info(f"💳 Доступные способы оплаты: {payment.available_payment_methods}")
        except Exception as log_error:
            logging.debug(f"Не удалось получить информацию о способах оплаты: {log_error}")
        
        logging.info(
            f"✅ Платеж создан: payment_id={payment_id}, "
            f"user_id={user_id}, amount={amount}, status={status}"
        )
        logging.info(f"🔗 URL для оплаты: {confirmation_url}")
        
        # Дополнительное предупреждение, если используется тестовый режим
        if is_test_mode:
            logging.warning(
                f"⚠️ ВНИМАНИЕ: Платеж создан в ТЕСТОВОМ режиме! "
                f"СБП недоступен в тестовом режиме. "
                f"Для работы СБП используйте боевые ключи ЮKassa."
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
        
        # Получаем информацию о чеке, если она доступна
        receipt_info = None
        if hasattr(payment, 'receipt') and payment.receipt:
            receipt_info = {
                "receipt_registration": getattr(payment.receipt, 'receipt_registration', None),
                "fiscal_storage_number": getattr(payment.receipt, 'fiscal_storage_number', None),
                "fiscal_document_number": getattr(payment.receipt, 'fiscal_document_number', None),
                "fiscal_attribute": getattr(payment.receipt, 'fiscal_attribute', None),
                "fiscal_provider_id": getattr(payment.receipt, 'fiscal_provider_id', None),
            }
        
        return {
            "payment_id": payment.id,
            "status": payment.status,
            "amount": float(payment.amount.value),
            "currency": payment.amount.currency,
            "metadata": payment.metadata if hasattr(payment, 'metadata') else {},
            "paid": payment.paid if hasattr(payment, 'paid') else False,
            "created_at": payment.created_at if hasattr(payment, 'created_at') else None,
            "receipt": receipt_info  # Информация о чеке
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
    from app.storage import _get_db
    
    pending_payments = []
    
    try:
        async with _get_db() as db:
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

