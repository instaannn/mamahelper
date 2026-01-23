# app/webhook_server.py
"""
Простой HTTP сервер для обработки webhook уведомлений от ЮKassa.
Можно запустить отдельно или интегрировать в основной процесс бота.
"""
import logging
import asyncio
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import concurrent.futures

from app.webhook_handler import handle_yookassa_webhook_request, is_yookassa_configured


class YooKassaWebhookHandler(BaseHTTPRequestHandler):
    """HTTP обработчик для webhook от ЮKassa."""
    
    def do_POST(self):
        """Обработать POST запрос от ЮKassa."""
        try:
            # Проверяем путь
            if self.path != '/webhooks/yookassa':
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Not found"}).encode())
                return
            
            # Читаем тело запроса
            content_length = int(self.headers.get('Content-Length', 0))
            request_body = self.rfile.read(content_length)
            
            # Получаем подпись из заголовка
            signature = self.headers.get('X-YooMoney-Signature', '')
            
            # Обрабатываем webhook в синхронном контексте
            # Используем asyncio.run для запуска async функции
            # Если event loop уже запущен, используем другой подход
            try:
                try:
                    # Пытаемся получить текущий event loop
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Если loop уже запущен, создаем новый в отдельном потоке
                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(
                                asyncio.run,
                                handle_yookassa_webhook_request(request_body, signature)
                            )
                            result = future.result(timeout=30)
                    else:
                        result = loop.run_until_complete(
                            handle_yookassa_webhook_request(request_body, signature)
                        )
                except RuntimeError:
                    # Нет event loop, создаем новый
                    result = asyncio.run(
                        handle_yookassa_webhook_request(request_body, signature)
                    )
            except Exception as e:
                logging.error(f"❌ [WEBHOOK] Ошибка при обработке webhook: {e}", exc_info=True)
                result = {"status": "error", "message": "Internal server error"}
            
            # Отправляем ответ
            status_code = 200 if result.get("status") == "ok" else 500
            self.send_response(status_code)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
            logging.info(f"📤 [WEBHOOK] Отправлен ответ: status={status_code}, result={result}")
            
        except Exception as e:
            logging.error(f"❌ [WEBHOOK] Ошибка при обработке HTTP запроса: {e}", exc_info=True)
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Internal server error"}).encode())
    
    def do_GET(self):
        """Обработать GET запрос (health check)."""
        if self.path == '/health' or self.path == '/webhooks/yookassa/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "yookassa-webhook"}).encode())
        else:
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
    
    def log_message(self, format, *args):
        """Переопределяем логирование для использования нашего logger."""
        logging.debug(f"[HTTP] {format % args}")


def run_webhook_server(host: str = '0.0.0.0', port: int = 8080):
    """
    Запустить HTTP сервер для обработки webhook от ЮKassa.
    
    Args:
        host: Хост для прослушивания (по умолчанию 0.0.0.0)
        port: Порт для прослушивания (по умолчанию 8080)
    """
    if not is_yookassa_configured():
        logging.warning("⚠️ [WEBHOOK] ЮKassa не настроен, webhook сервер не запускается")
        return
    
    server = HTTPServer((host, port), YooKassaWebhookHandler)
    logging.info(f"✅ [WEBHOOK] Webhook сервер запущен на {host}:{port}")
    logging.info(f"📡 [WEBHOOK] Endpoint для ЮKassa: http://{host}:{port}/webhooks/yookassa")
    logging.info(f"💡 [WEBHOOK] Health check: http://{host}:{port}/health")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("🛑 [WEBHOOK] Остановка webhook сервера...")
        server.shutdown()


def start_webhook_server_thread(host: str = '0.0.0.0', port: int = 8080) -> threading.Thread:
    """
    Запустить webhook сервер в отдельном потоке.
    
    Args:
        host: Хост для прослушивания
        port: Порт для прослушивания
    
    Returns:
        Thread объект запущенного сервера
    """
    thread = threading.Thread(
        target=run_webhook_server,
        args=(host, port),
        daemon=True,
        name="YooKassaWebhookServer"
    )
    thread.start()
    return thread


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    webhook_host = os.getenv('WEBHOOK_HOST', '0.0.0.0')
    webhook_port = int(os.getenv('WEBHOOK_PORT', '8080'))
    
    run_webhook_server(host=webhook_host, port=webhook_port)
