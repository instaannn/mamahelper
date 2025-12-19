# app/handlers/feedback.py
from datetime import datetime, timezone
from typing import Tuple

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from app.storage import save_feedback  # добавим функцию ниже
from app.i18n_ru import DISCLAIMER

# Состояния диалога обратной связи
ASK_TEXT, ASK_CONTACT = range(2)

# /feedback — входная точка
async def start_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([["Отмена"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "💬 Напишите ваши предложения, идеи или что стоит улучшить.\n\n"
        "Можно прикрепить скриншот в следующем сообщении или просто описать текстом.\n\n"
        "Чтобы отменить — нажмите «Отмена».",
        reply_markup=kb,
    )
    return ASK_TEXT

# Получаем текст
async def got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "отмена":
        await update.message.reply_text("Отменено. Спасибо! 🙌", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # Сохраняем черновик в user_data
    context.user_data["feedback_text"] = text

    # Предложим оставить контакт (кнопка «Отправить телефон»)
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить телефон", request_contact=True)], ["Продолжить без контакта", "Отмена"]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "📞 По желанию: оставьте контакт, чтобы мы могли ответить.\n"
        "Это не обязательно — можно продолжить без контакта.",
        reply_markup=kb,
    )
    return ASK_CONTACT

# Контакт или отказ
async def got_contact_or_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обработка кнопок
    if update.message.text:
        if update.message.text.lower() == "отмена":
            await update.message.reply_text("Отменено. Спасибо! 🙌", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        # продолжить без контакта
        contact_value = None
    elif update.message.contact:
        c = update.message.contact
        contact_value = f"{c.first_name or ''} {c.last_name or ''} | {c.phone_number or ''}".strip()
    else:
        # неожиданный ввод
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Отправить телефон", request_contact=True)], ["Продолжить без контакта", "Отмена"]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        await update.message.reply_text("Пожалуйста, выберите кнопку или напишите «Отмена».", reply_markup=kb)
        return ASK_CONTACT

    # Собираем информацию и сохраняем
    user = update.effective_user
    text = context.user_data.get("feedback_text", "(пусто)")
    meta = {
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "chat_id": update.effective_chat.id,
        "contact": contact_value,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # (Необязательно) добавим текущий «контекст» из калькулятора, если он есть
    for k in ("drug", "conc_label", "weight"):
        if k in context.user_data:
            meta[k] = context.user_data[k]

    save_feedback(text=text, meta=meta)

    await update.message.reply_text(
        "Спасибо за обратную связь! 💌 Мы обязательно посмотрим.\n"
        f"{DISCLAIMER}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

def build_feedback_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("feedback", start_feedback)],
        states={
            ASK_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_text)],
            ASK_CONTACT: [
                MessageHandler(filters.CONTACT, got_contact_or_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_contact_or_skip),
            ],
        },
        fallbacks=[CommandHandler("feedback", start_feedback)],
        allow_reentry=True,
    )
