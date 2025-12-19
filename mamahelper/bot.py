import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Загружаем переменные окружения из .env файла
load_dotenv()

# Включаем логирование, чтобы видеть что происходит
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Токен из переменных окружения
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Определяем функцию-обработчик для команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(f"Привет, {user_name}! Я твой помощник. Я помогу рассчитать нужную дозу лекарства для малыша. Просто напиши мне вес ребенка и название лекарства.")

# Определяем функцию-обработчик для команды /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    Вот что я пока умею:
    /start - начать работу
    /help - показать эту справку
    Напиши мне что-то вроде: 'парацетамол для 11 кг'
    """
    await update.message.reply_text(help_text)

# Главная функция, которая запускает бота
def main():
    if not API_TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в переменных окружения. Создайте файл .env с токеном.")
    
    # Создаем Application объект
    application = Application.builder().token(API_TOKEN).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Запускаем бота. Он будет постоянно опрашивать сервера Telegram на предмет новых сообщений.
    print("Бот запущен...")
    application.run_polling()

# Запускаем функцию main, если файл запущен напрямую
if __name__ == '__main__':
    main()