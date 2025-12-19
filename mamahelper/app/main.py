# app/main.py
import logging
import os
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.handlers.dose import build_calculate_conversation
from app.handlers.feedback import build_feedback_conversation
from app.handlers.redflags import build_redflags_handlers  # ← добавили правильный импорт

# Загружаем переменные окружения из .env файла
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Берём токен из переменных окружения
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"""Привет, {user_name}! Я помогу быстро и бережно посчитать разовую дозу сиропа для малыша 👶💊

Полезно прямо сейчас:
• Рассчитать дозу: /calculate
• Красные флаги при ОРВИ: /redflags
• Красные флаги при поносе/рвоте и обезвоживании: /redflags_gi
• Идеи и обратная связь: /feedback
"""
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Привет! Я помогу быстро и бережно посчитать разовую дозу сиропа для малыша 👶💊\n\n"
        "Команды:\n"
        "/calculate — расчёт дозы (шаг за шагом)\n"
        "/redflags — красные флаги при ОРВИ 🚩\n"
        "/redflags_gi — красные флаги при поносе/рвоте и обезвоживании 🚩\n"
        "/feedback — предложения и обратная связь 💬\n"
        "/help — помощь и подсказки ℹ️\n\n"
        "Подсказка: команды появляются в меню «/».\n\n"
        "Важно: я ИИ-помощник, не врач. При тревожных симптомах обращайтесь к педиатру. "
        "В экстренной ситуации звоните 103/112."
    )
    await update.message.reply_text(help_text)

def main():
    if not API_TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в переменных окружения. Создайте файл .env с токеном.")

    application = Application.builder().token(API_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Диалоги/обработчики
    application.add_handler(build_calculate_conversation())
    application.add_handler(build_feedback_conversation())

    # Красные флаги (ОРВИ + ЖКТ)
    for h in build_redflags_handlers():
        application.add_handler(h)

    print("Бот запущен... (polling)")
    application.run_polling()

if __name__ == "__main__":
    main()