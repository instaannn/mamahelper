# app/webhook_handler.py
"""
Обработчик HTTP webhook уведомлений от ЮKassa.
Можно запустить как отдельный сервер или интегрировать в основной процесс.
"""
import logging
import json
import hmac
import hashlib
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from app.payments import is_yookassa_configured
from app.storage import complete_yookassa_payment, mark_payment_notification_sent, is_user_premium
import os
from dotenv import load_dotenv

load_dotenv()

# Секретный ключ для проверки подписи webhook (опционально, но рекомендуется)
YOOKASSA_WEBHOOK_SECRET = os.getenv('YOOKASSA_WEBHOOK_SECRET')


def verify_webhook_signature(request_body: bytes, signature: str) -> bool:
    """
    Проверить подпись webhook от ЮKassa (HMAC-SHA256).
    
    Args:
        request_body: Тело запроса в байтах
        signature: Подпись из заголовка X-YooMoney-Signature
    
    Returns:
        True если подпись валидна, False иначе
    """
    if not YOOKASSA_WEBHOOK_SECRET:
        # Если секретный ключ не настроен, пропускаем проверку
        logging.warning("⚠️ [WEBHOOK] YOOKASSA_WEBHOOK_SECRET не настроен, пропускаем проверку подписи")
        return True
    
    try:
        # Вычисляем HMAC-SHA256 подпись
        expected_signature = hmac.new(
            YOOKASSA_WEBHOOK_SECRET.encode('utf-8'),
            request_body,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_signature, signature)
    except Exception as e:
        logging.error(f"❌ [WEBHOOK] Ошибка при проверке подписи webhook: {e}", exc_info=True)
        return False


async def process_yookassa_webhook(request_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Обработать webhook уведомление от ЮKassa.
    
    Args:
        request_data: JSON данные из webhook запроса
    
    Returns:
        Словарь с результатом обработки
    """
    try:
        # Проверяем тип события
        event = request_data.get('event')
        if not event:
            logging.warning("⚠️ [WEBHOOK] Получен webhook без поля 'event'")
            return {"status": "error", "message": "Missing event field"}
        
        logging.info(f"📥 [WEBHOOK] Получен webhook от ЮKassa: event={event}")
        
        # Обрабатываем только событие payment.succeeded
        if event != 'payment.succeeded':
            logging.debug(f"ℹ️ [WEBHOOK] Событие {event} не требует обработки, пропускаем")
            return {"status": "ok", "message": f"Event {event} ignored"}
        
        # Получаем объект платежа
        payment_object = request_data.get('object', {})
        if not payment_object:
            logging.error("❌ [WEBHOOK] Получен webhook без поля 'object'")
            return {"status": "error", "message": "Missing object field"}
        
        payment_id = payment_object.get('id')
        if not payment_id:
            logging.error("❌ [WEBHOOK] Получен webhook без payment_id в объекте")
            return {"status": "error", "message": "Missing payment id"}
        
        # Проверяем статус платежа
        payment_status = payment_object.get('status')
        if payment_status != 'succeeded':
            logging.warning(f"⚠️ [WEBHOOK] Платеж {payment_id} имеет статус {payment_status}, ожидался 'succeeded'")
            return {"status": "ok", "message": f"Payment status is {payment_status}, not succeeded"}
        
        # Проверяем metadata для сверки user_id
        metadata = payment_object.get('metadata', {})
        user_id_str = metadata.get('user_id')
        
        logging.info(
            f"📥 [WEBHOOK] Обработка payment.succeeded: payment_id={payment_id}, "
            f"user_id={user_id_str if user_id_str else 'unknown'}"
        )
        
        # Завершаем платеж и активируем премиум
        result = await complete_yookassa_payment(payment_id)
        
        if result:
            user_id = result.get('user_id')
            subscription_days = result.get('subscription_days')
            premium_until = result.get('premium_until')
            
            logging.info(
                f"✅ [WEBHOOK] Премиум активирован через webhook для user_id={user_id} "
                f"(платеж: {payment_id}, подписка: {subscription_days} дней)"
            )
            
            # Проверяем, что user_id из metadata совпадает с user_id из платежа
            if user_id_str:
                try:
                    metadata_user_id = int(user_id_str)
                    if metadata_user_id != user_id:
                        logging.warning(
                            f"⚠️ [WEBHOOK] Несоответствие user_id: metadata={metadata_user_id}, "
                            f"платеж={user_id} для payment_id={payment_id}"
                        )
                except ValueError:
                    logging.warning(f"⚠️ [WEBHOOK] Некорректный user_id в metadata: {user_id_str}")
            
            # Проверяем, что премиум действительно активирован
            has_premium = await is_user_premium(user_id)
            if not has_premium:
                logging.error(f"❌ [WEBHOOK] КРИТИЧЕСКАЯ ОШИБКА: Премиум НЕ активирован для user_id={user_id} после complete_yookassa_payment!")
                return {
                    "status": "error",
                    "message": "Premium activation failed",
                    "user_id": user_id,
                    "payment_id": payment_id
                }
            
            return {
                "status": "ok",
                "message": "Payment processed successfully",
                "user_id": user_id,
                "payment_id": payment_id,
                "subscription_days": subscription_days
            }
        else:
            logging.warning(f"⚠️ [WEBHOOK] complete_yookassa_payment вернул None для платежа {payment_id}")
            # Проверяем, может быть премиум уже активирован
            if user_id_str:
                try:
                    user_id = int(user_id_str)
                    has_premium = await is_user_premium(user_id)
                    if has_premium:
                        logging.info(f"ℹ️ [WEBHOOK] Премиум уже активирован для user_id={user_id}")
                        return {
                            "status": "ok",
                            "message": "Premium already activated",
                            "user_id": user_id,
                            "payment_id": payment_id
                        }
                except ValueError:
                    pass
            
            return {
                "status": "ok",
                "message": "Payment already processed or not found",
                "payment_id": payment_id
            }
    
    except Exception as e:
        logging.error(f"❌ [WEBHOOK] Ошибка при обработке webhook от ЮKassa: {e}", exc_info=True)
        return {
            "status": "error",
            "message": "Internal server error"
        }


# Для использования с aiohttp или другим HTTP фреймворком
async def handle_yookassa_webhook_request(request_body: bytes, signature: Optional[str] = None) -> Dict[str, Any]:
    """
    Обработать HTTP запрос webhook от ЮKassa.
    
    Args:
        request_body: Тело запроса в байтах
        signature: Подпись из заголовка X-YooMoney-Signature (опционально)
    
    Returns:
        Словарь с результатом обработки для отправки в HTTP ответе
    """
    try:
        # Проверяем подпись (если предоставлена)
        if signature and not verify_webhook_signature(request_body, signature):
            logging.warning("⚠️ [WEBHOOK] Неверная подпись webhook от ЮKassa")
            return {"status": "error", "message": "Invalid signature"}
        
        # Парсим JSON
        try:
            request_data = json.loads(request_body.decode('utf-8'))
        except json.JSONDecodeError as e:
            logging.error(f"❌ [WEBHOOK] Ошибка парсинга JSON в webhook: {e}")
            return {"status": "error", "message": "Invalid JSON"}
        
        # Обрабатываем webhook
        return await process_yookassa_webhook(request_data)
    
    except Exception as e:
        logging.error(f"❌ [WEBHOOK] Ошибка при обработке webhook запроса: {e}", exc_info=True)
        return {"status": "error", "message": "Internal server error"}
