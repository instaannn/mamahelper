# app/main.py
import logging
import os
import sys
import subprocess
import time
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta, time as dt_time

from telegram import Update, LabeledPrice
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, PreCheckoutQueryHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.handlers.dose import build_calculate_conversation
from app.handlers.feedback import build_feedback_conversation
from app.handlers.redflags import build_redflags_handlers
from app.handlers.profile import build_profile_handlers
from app.storage import (
    init_db, get_child_profile, set_user_premium, is_user_premium,
    get_users_with_expiring_premium, get_users_with_expired_premium,
    has_notification_been_sent, mark_notification_sent,
    save_payment, complete_payment,
    track_user_interaction, get_bot_statistics,
    disable_expired_premium_subscriptions, DB_PATH, mark_payment_notification_sent
)
from app.utils import is_premium_user
from app.payments import create_payment, is_yookassa_configured, get_payment_status, check_pending_payments
from app.storage import complete_yookassa_payment, mark_payment_notification_sent, mark_payment_notification_sent

# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройка логирования с записью в файл
LOG_DIR = Path(__file__).resolve().parent / "data"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

# Создаем форматтер
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Настройка root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Очищаем существующие обработчики
root_logger.handlers.clear()

# Обработчик для файла
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

# Обработчик для консоли
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
root_logger.addHandler(console_handler)

logging.info(f"📝 Логи записываются в файл: {LOG_FILE}")

# Берём токен из переменных окружения
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not API_TOKEN:
    raise SystemExit(
        "❌ Ошибка: TELEGRAM_BOT_TOKEN не установлен!\n\n"
        "Установите токен одним из способов:\n"
        "1. Создайте файл .env с содержимым: TELEGRAM_BOT_TOKEN=ваш_токен\n"
        "2. Или установите переменную окружения: export TELEGRAM_BOT_TOKEN=ваш_токен\n\n"
        "Получите токен от @BotFather в Telegram."
    )

# Токен провайдера платежей (опционально, для Telegram Payments)
PROVIDER_TOKEN = os.getenv('PROVIDER_TOKEN')

# ID администратора бота (для доступа к статистике)
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
if ADMIN_USER_ID:
    try:
        ADMIN_USER_ID = int(ADMIN_USER_ID)
    except ValueError:
        ADMIN_USER_ID = None
        logging.warning("⚠️ ADMIN_USER_ID должен быть числом")
else:
    ADMIN_USER_ID = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start с новым приветственным сценарием."""
    try:
        if not update.message:
            logging.warning("Received /start command but update.message is None")
            return
        
        user_id = update.effective_user.id if update.effective_user else "unknown"
        
        # Проверяем, есть ли параметр в команде /start (например, /start payment_success)
        command_args = update.message.text.split() if update.message.text else []
        if len(command_args) > 1 and command_args[1] == "payment_success":
            # Пользователь вернулся после оплаты
            logging.info(f"💰 Пользователь {user_id} вернулся после оплаты")
            # Проверяем статус платежей немедленно
            if is_yookassa_configured():
                try:
                    await check_yookassa_payments_status(context)
                except Exception as e:
                    logging.error(f"❌ Ошибка при проверке платежей: {e}", exc_info=True)
            # Продолжаем выполнение обычного /start
        
        logging.info(f"🚀 [START] Начало обработки команды /start для user {user_id}")
        
        # Показываем индикатор печати (не блокируем при ошибке)
        try:
            logging.debug(f"📝 [START] Показываем индикатор печати для user {user_id}")
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing"
            )
            logging.debug(f"✅ [START] Индикатор печати показан для user {user_id}")
        except Exception as action_error:
            logging.warning(f"⚠️ [START] Не удалось показать индикатор печати для user {user_id}: {action_error}")
            # Не критично, продолжаем работу
        
        user = update.effective_user
        # Используем имя профиля (first_name), если нет - username, если нет - "друг"
        user_name = user.first_name or user.username or "друг"
        
        # Оптимизация: выполняем критичные проверки параллельно, track_user_interaction - асинхронно
        logging.debug(f"📝 [START] Запускаем параллельные проверки для user {user_id}")
        from app.storage import has_dose_events
        
        # Запускаем критичные проверки параллельно (без track_user_interaction для ускорения)
        profile_task = asyncio.create_task(get_child_profile(user.id))
        events_task = asyncio.create_task(has_dose_events(user.id))
        premium_task = asyncio.create_task(is_premium_user(user.id))
        
        # Ждем результаты критичных проверок
        logging.debug(f"⏳ [START] Ожидаем результаты проверок для user {user_id}")
        profile, has_events, is_premium = await asyncio.gather(
            profile_task,
            events_task,
            premium_task,
            return_exceptions=True
        )
        logging.debug(f"✅ [START] Получены результаты проверок для user {user_id}")
        
        # Обрабатываем исключения
        if isinstance(profile, Exception):
            logging.warning(f"⚠️ Ошибка при получении профиля для user {user.id}: {profile}")
            profile = None
        if isinstance(has_events, Exception):
            logging.warning(f"⚠️ Ошибка при проверке записей для user {user.id}: {has_events}")
            has_events = False
        if isinstance(is_premium, Exception):
            logging.warning(f"⚠️ Ошибка при проверке премиума для user {user.id}: {is_premium}")
            is_premium = False
        
        # Отслеживание взаимодействия запускаем в фоне (не блокируем ответ)
        asyncio.create_task(track_user_interaction(user.id))
        
        has_profile = profile is not None
        if has_profile:
            logging.info(f"User {user.id} has profile: name={profile.child_name}, weight={profile.child_weight_kg}, age={profile.child_age_months}")
        else:
            logging.info(f"User {user.id} has no profile")
        
        # Определяем, первый ли это визит
        # Если у пользователя есть активный премиум - это точно не первый визит (он уже использовал бота)
        if is_premium:
            is_first_visit = False
            logging.debug(f"User {user.id} имеет активный премиум - это не первый визит")
        else:
            # Если нет профиля и нет записей - это первый визит
            # Убрали дополнительную проверку БД для ускорения - track_user_interaction уже обновит bot_users
            is_first_visit = not has_profile and not has_events
        
        logging.info(f"User {user.id} ({user_name}) - Bot Premium status: {is_premium}, First visit: {is_first_visit}")
        
        # Формируем приветственное сообщение
        if is_first_visit:
            # Расширенное приветствие для новых пользователей
            welcome_text = (
                f"Привет, {user_name}! 👋\n\n"
                f"Добро пожаловать! Я — твой помощник для бережного расчёта дозы лекарства для малыша. 👶💖\n\n"
                f"**Что я умею:**\n\n"
                f"💊 **Рассчитать дозу** — Помогу правильно рассчитать разовую дозу жаропонижающего "
                f"(парацетамол или ибупрофен) для вашего ребенка на основе его веса.\n\n"
                f"📋 **Подсказки и рекомендации** — Напомню о важных правилах приема и дозировках.\n\n"
                f"**Как начать:**\n\n"
                f"1️⃣ Нажмите кнопку **«💊 Рассчитать дозу»** ниже\n"
                f"2️⃣ Следуйте моим подсказкам — я задам несколько простых вопросов\n"
                f"3️⃣ Получите точный расчет дозы для вашего малыша\n\n"
                f"Все расчеты абсолютно бесплатны! 💚\n\n"
            )
        else:
            # Обычное приветствие для возвращающихся пользователей
            welcome_text = (
                f"Привет, {user_name}! 👋\n\n"
                f"Рада тебя видеть! Я — твой помощник для бережного расчёта дозы лекарства для малыша. 👶💖\n\n"
            )
        
        # Если профиля нет и пользователь премиум - предлагаем создать
        if not has_profile and is_premium:
            welcome_text += (
                "Чтобы считать дозу было ещё быстрее и удобнее, можно сохранить данные ребёнка. "
                "Это займёт меньше минуты, и тебе не придётся вводить их заново!\n\n"
            )
        
        # Информация о премиум (только если не премиум)
        if not is_premium:
            welcome_text += (
                "Хотите сэкономить время? У нас есть Премиум-доступ с дополнительными удобствами, "
                "но все основные расчёты абсолютно бесплатны.\n\n"
            )
        else:
            welcome_text += (
                "✨ Спасибо за Премиум-подписку! Вы получаете дополнительные удобства.\n\n"
            )
        
        welcome_text += "Итак, начнём? 😊"
        
        # Создаем inline кнопки
        keyboard = []
        
        keyboard.append([InlineKeyboardButton("💊 Рассчитать дозу", callback_data="start_calculate")])
        
        # Кнопка Профиль (только для премиум)
        if is_premium:
            keyboard.append([InlineKeyboardButton("👶 Профиль", callback_data="start_profile")])
            
            # Кнопка дневника (только если есть записи) - используем уже полученное значение has_events
            if has_events:
                keyboard.append([InlineKeyboardButton("📖 Посмотреть дневник приема лекарств", callback_data="dose_diary")])
        
        # Кнопки красных флагов (только для премиум)
        if is_premium:
            keyboard.append([
                InlineKeyboardButton("🚩 Красные флаги ОРВИ", callback_data="start_redflags_orvi"),
                InlineKeyboardButton("🚩 Красные флаги ЖКТ", callback_data="start_redflags_gi")
            ])
        
        if not is_premium:
            keyboard.append([InlineKeyboardButton("⭐ Узнать о Премиум", callback_data="start_premium_info")])
        
        keyboard.append([InlineKeyboardButton("📋 Все команды", callback_data="start_help")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем сообщение с обработкой таймаутов
        try:
            # Пытаемся отправить с Markdown для первого визита
            if is_first_visit:
                try:
                    await asyncio.wait_for(
                        update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown"),
                        timeout=10.0
                    )
                    logging.debug(f"✅ Сообщение отправлено с Markdown для user {user_id}")
                except asyncio.TimeoutError:
                    logging.warning(f"⚠️ Таймаут при отправке с Markdown для user {user_id}, пробуем без форматирования")
                    # Пробуем без Markdown
                    await asyncio.wait_for(
                        update.message.reply_text(welcome_text.replace("**", "").replace("*", ""), reply_markup=reply_markup),
                        timeout=10.0
                    )
                    logging.debug(f"✅ Сообщение отправлено без форматирования для user {user_id}")
            else:
                await asyncio.wait_for(
                    update.message.reply_text(welcome_text, reply_markup=reply_markup),
                    timeout=20.0  # Увеличиваем таймаут для стабильности
                )
                logging.debug(f"✅ Сообщение отправлено для user {user_id}")
        except Exception as send_error:
            # Обрабатываем таймауты и другие ошибки отправки
            from telegram.error import TimedOut
            if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
                logging.warning(f"⚠️ Таймаут при отправке сообщения пользователю {update.effective_user.id}, но сообщение может быть доставлено")
                # НЕ отправляем упрощенное сообщение - основное может быть доставлено
                # Просто логируем и продолжаем
            else:
                # Для других ошибок пробрасываем дальше
                raise
    except Exception as e:
        # Детальное логирование ошибки
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА в команде /start для user {update.effective_user.id}:")
        logging.error(f"Тип ошибки: {type(e).__name__}")
        logging.error(f"Сообщение: {str(e)}")
        logging.error(f"Полный traceback:\n{error_details}")
        
        # Пытаемся отправить сообщение пользователю
        try:
            await update.message.reply_text(
                "Произошла ошибка при обработке команды. Пожалуйста, попробуйте еще раз.\n\n"
                f"Ошибка: {type(e).__name__}: {str(e)[:100]}"
            )
        except Exception as send_error:
            logging.error(f"❌ Не удалось отправить сообщение об ошибке пользователю: {send_error}")

async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline кнопок из команды /start."""
    query = update.callback_query
    
    # Показываем индикатор загрузки сразу
    await query.answer(text="⏳ Загрузка...", show_alert=False)
    
    if query.data == "start_premium_info":
        # Информация о премиум
        premium_text = (
            "🌟 Премиум-доступ — ваше спокойствие и удобство!\n\n"
            "Теперь вы можете не запоминать, когда и сколько лекарства вы дали малышу. "
            "Мы сделаем это за вас!\n\n"
            "С Премиумом вы получаете:\n\n"
            "• 👶 Профиль ребенка — Создайте и сохраните вес, возраст и другие данные. "
            "Больше не нужно вводить их каждый раз!\n\n"
            "• 📊 Дневник лекарств — Отслеживайте каждый прием жаропонижающего. "
            "Мы покажем, сколько уже дали за сутки и предупредим, если приближаетесь к максимуму.\n\n"
            "И помните: все основные расчеты разовых доз остаются абсолютно бесплатными! 💚 "
            "Премиум — это для вашего дополнительного комфорта и уверенности."
        )
        
        # Создаем кнопки для покупки премиум
        premium_keyboard = [
            [InlineKeyboardButton("🌟 1 месяц - 99₽", callback_data="premium_buy_1month")],
            [InlineKeyboardButton("🌟 3 месяца - 270₽", callback_data="premium_buy_3months")],
            [InlineKeyboardButton("❤️ Поддержать проект", callback_data="premium_support")],
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ]
        premium_markup = InlineKeyboardMarkup(premium_keyboard)
        
        user_id = query.from_user.id if query.from_user else "unknown"
        try:
            # Отправляем с таймаутом
            await asyncio.wait_for(
                query.message.reply_text(premium_text, reply_markup=premium_markup),
                timeout=10.0
            )
            logging.debug(f"✅ Сообщение о премиум отправлено для user {user_id}")
        except (asyncio.TimeoutError, Exception) as send_error:
            from telegram.error import TimedOut
            if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
                logging.warning(f"⚠️ Таймаут при отправке сообщения о премиум пользователю {user_id}")
                # Пробуем отправить упрощенное сообщение
                try:
                    simple_text = (
                        "🌟 Премиум-доступ\n\n"
                        "• 👶 Профиль ребенка\n"
                        "• 📊 Дневник лекарств\n\n"
                        "Все расчеты бесплатны! 💚"
                    )
                    await asyncio.wait_for(
                        query.message.reply_text(simple_text, reply_markup=premium_markup),
                        timeout=5.0
                    )
                    logging.info(f"✅ Отправлено упрощенное сообщение о премиум для user {user_id}")
                except Exception:
                    logging.error(f"❌ Не удалось отправить даже упрощенное сообщение о премиум для user {user_id}")
            else:
                logging.error(f"❌ Ошибка при отправке сообщения о премиум пользователю {user_id}: {send_error}")
                raise
    
    elif query.data == "start_help":
        # Показываем все команды
        # Создаем правильный update с пользователем из callback_query
        from datetime import datetime
        
        class HelpMessage:
            def __init__(self, original_msg, user):
                self.message_id = original_msg.message_id
                self.date = datetime.now()
                self.chat = original_msg.chat
                self.from_user = user
                self.text = "/help"
                self.entities = None
                self._original = original_msg
            
            async def reply_text(self, *args, **kwargs):
                return await self._original.reply_text(*args, **kwargs)
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        help_message = HelpMessage(query.message, query.from_user)
        help_update = Update(update_id=update.update_id + 20000, message=help_message)
        await help_command(help_update, context)
    
    elif query.data == "start_home":
        # Вернуться на главную (показать приветственное сообщение)
        # Индикатор загрузки уже показан выше
        # Создаем fake message для вызова start
        from datetime import datetime
        
        class HomeMessage:
            def __init__(self, original_msg, user):
                self.message_id = original_msg.message_id
                self.date = datetime.now()
                self.chat = original_msg.chat
                self.from_user = user
                self.text = "/start"
                self.entities = None
                self._original = original_msg
            
            async def reply_text(self, *args, **kwargs):
                try:
                    return await self._original.reply_text(*args, **kwargs)
                except Exception as send_error:
                    from telegram.error import TimedOut
                    if isinstance(send_error, TimedOut):
                        logging.warning(f"⚠️ Таймаут при отправке сообщения пользователю {self.from_user.id}, но сообщение может быть доставлено")
                    else:
                        raise
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        home_message = HomeMessage(query.message, query.from_user)
        home_update = Update(update_id=update.update_id + 40000, message=home_message)
        try:
            await start(home_update, context)
        except Exception as e:
            from telegram.error import TimedOut
            if isinstance(e, TimedOut):
                logging.warning(f"⚠️ Таймаут при обработке команды /start для пользователя {query.from_user.id}, но сообщение может быть доставлено")
            else:
                raise
    
    elif query.data == "start_calculate":
        # Для кнопки "Рассчитать дозу" - просто отвечаем, обработка в ConversationHandler
        await query.answer(text="⏳ Загрузка...", show_alert=False)
    
    elif query.data == "start_profile":
        # Меню профиля для премиум-пользователей
        await query.answer(text="⏳ Загрузка...", show_alert=False)
        profile_keyboard = [
            [InlineKeyboardButton("👶 Посмотреть профили", callback_data="profile_show")],
            [InlineKeyboardButton("👶 Создать/добавить профиль", callback_data="start_create_profile")]
        ]
        profile_markup = InlineKeyboardMarkup(profile_keyboard)
        await query.message.reply_text(
            "👶 Управление профилями детей:\n\n"
            "Выберите действие:",
            reply_markup=profile_markup
        )
    
    elif query.data == "start_redflags_orvi":
        # Красные флаги ОРВИ
        await query.answer(text="⏳ Загрузка...", show_alert=False)
        from app.handlers.redflags import REDFLAGS_ORVI_TEXT
        redflags_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ])
        await query.message.reply_text(REDFLAGS_ORVI_TEXT, reply_markup=redflags_keyboard)
    
    elif query.data == "start_redflags_gi":
        # Красные флаги ЖКТ
        await query.answer(text="⏳ Загрузка...", show_alert=False)
        from app.handlers.redflags import REDFLAGS_GI_TEXT
        redflags_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ])
        await query.message.reply_text(REDFLAGS_GI_TEXT, reply_markup=redflags_keyboard)
    
    # Обработка callback'ов профиля (profile_show, profile_delete_confirm) будет в отдельном обработчике

async def handle_profile_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок профиля (profile_show, profile_delete_confirm)."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "profile_show":
        # Показать профиль
        from app.handlers.profile import show_profile
        from datetime import datetime
        
        class ProfileMessage:
            def __init__(self, original_msg, user):
                self.message_id = original_msg.message_id
                self.date = datetime.now()
                self.chat = original_msg.chat
                self.from_user = user
                self.text = "/profile"
                self.entities = None
                self._original = original_msg
            
            async def reply_text(self, *args, **kwargs):
                return await self._original.reply_text(*args, **kwargs)
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        profile_message = ProfileMessage(query.message, query.from_user)
        profile_update = Update(update_id=update.update_id + 30000, message=profile_message)
        await show_profile(profile_update, context)
    
    elif query.data.startswith("profile_delete_"):
        # Удаление конкретного профиля
        try:
            profile_id = int(query.data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("❌ Ошибка: неверный ID профиля.")
            return
        
        from app.storage import delete_child_profile, get_all_child_profiles
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        user_id = query.from_user.id
        
        deleted = await delete_child_profile(user_id, profile_id)
        
        if deleted:
            # Проверяем, остались ли еще профили
            remaining_profiles = await get_all_child_profiles(user_id)
            if remaining_profiles:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("👶 Посмотреть профили", callback_data="profile_show")]])
                await query.message.reply_text(
                    "✅ Профиль удален.",
                    reply_markup=kb
                )
            else:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("👶 Создать новый профиль", callback_data="start_create_profile")]])
                await query.message.reply_text(
                    "✅ Профиль удален.\n\n"
                    "Можете создать новый профиль:",
                    reply_markup=kb
                )
        else:
            await query.message.reply_text("❌ Профиль не найден или уже удален.")

async def handle_dose_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сохранения приема лекарства в дневник."""
    query = update.callback_query
    await query.answer(text="⏳ Сохранение...", show_alert=False)
    
    user = query.from_user
    user_id = user.id
    
    # Проверяем премиум-статус
    from app.utils import is_premium_user
    is_premium = await is_premium_user(user_id)
    
    if not is_premium:
        await query.message.reply_text(
            "❌ Эта функция доступна только для премиум-пользователей.\n\n"
            "Используйте /premium чтобы узнать больше о премиум-доступе."
        )
        return
    
    # Проверяем наличие профиля
    # Используем выбранный профиль, если он был выбран при вводе веса
    from app.storage import get_child_profile
    selected_profile_id = context.user_data.get("selected_profile_id")
    if selected_profile_id:
        profile = await get_child_profile(user_id, selected_profile_id)
    else:
        profile = await get_child_profile(user_id)
    
    if not profile:
        await query.message.reply_text(
            "❌ Чтобы сохранить прием лекарства, сначала создайте профиль ребенка.\n\n"
            "Используйте кнопку «👶 Создать профиль» или команду /profile_set"
        )
        return
    
    # Получаем данные о дозе из user_data
    # context.user_data автоматически привязан к пользователю и чату
    dose_data = context.user_data.get("last_dose_data")
    
    if not dose_data:
        await query.message.reply_text(
            "❌ Не удалось найти данные о расчете дозы.\n\n"
            "Пожалуйста, выполните расчет дозы заново."
        )
        return
    
    # Получаем данные о дозе
    from app.storage import save_dose_event, get_daily_total_mg
    drug_key = dose_data.get("drug")
    dose_mg = dose_data.get("dose_mg")
    
    if not drug_key or not dose_mg:
        await query.message.reply_text(
            "❌ Ошибка при сохранении данных.\n\n"
            "Пожалуйста, выполните расчет дозы заново."
        )
        return
    
    # Проверяем суточную дозу перед сохранением
    # Получаем текущую суточную дозу для конкретного ребенка (по имени)
    child_name = profile.child_name or "Ребенок"
    current_daily_total = await get_daily_total_mg(user_id, drug_key, child_name=child_name)
    
    # Получаем максимальную суточную дозу из формуляра
    from app.utils import load_formulary
    formulary = load_formulary()
    drug_info = formulary["drugs"].get(drug_key)
    
    if not drug_info:
        await query.message.reply_text(
            "❌ Ошибка: не удалось найти информацию о препарате.\n\n"
            "Пожалуйста, выполните расчет дозы заново."
        )
        return
    
    # Получаем вес ребенка из профиля для расчета максимальной суточной дозы
    if not profile.child_weight_kg:
        await query.message.reply_text(
            "❌ Не указан вес ребенка в профиле.\n\n"
            "Для проверки суточной дозы необходимо указать вес ребенка."
        )
        return
    
    max_daily_mg_per_kg = float(drug_info.get("max_daily_mg_per_kg", 0))
    max_daily_total = max_daily_mg_per_kg * profile.child_weight_kg
    
    # Проверяем, не превысит ли новая доза суточный максимум
    if current_daily_total + dose_mg > max_daily_total:
        # Превышена суточная доза - показываем предупреждение с именем ребенка
        drug_name = "Парацетамола" if drug_key == "paracetamol" else "Ибупрофена"
        
        warning_text = (
            f"⚠️ **Суточная доза достигнута**\n\n"
            f"Вы уже дали **{child_name}** максимальную суточную дозу **{drug_name}**. "
            f"Дальнейший прием этого препарата сегодня **небезопасен**.\n\n"
            f"**Рекомендации:**\n\n"
            f"• Отложите это лекарство до завтра\n"
            f"• Контролируйте температуру другими способами\n"
            f"• Обеспечьте ребенку покой и обильное питье\n"
            f"• При сохранении высокой температуры **обязательно проконсультируйтесь с педиатром**\n\n"
            f"Телефон неотложной помощи: **103**"
        )
        
        await query.message.reply_text(warning_text, parse_mode="Markdown")
        return
    
    # Сохраняем событие (дата и время сохраняются автоматически внутри функции)
    # Передаем метаданные для дневника
    metadata = {
        "form": dose_data.get("form", "syrup"),
        "dose_ml": dose_data.get("dose_ml"),
        "conc_label": dose_data.get("conc_label", ""),
        "weight_kg": profile.child_weight_kg,
        "dose_text": dose_data.get("dose_text", f"{dose_mg:.0f} мг"),
        "child_name": profile.child_name  # Сохраняем имя ребенка для отображения в дневнике
    }
    from datetime import datetime, timezone
    await save_dose_event(user_id, drug_key, dose_mg, metadata)
    
    # Получаем текущее время для отображения
    now = datetime.now(timezone.utc)
    # Форматируем время для отображения (в московском времени UTC+3)
    from datetime import timedelta
    moscow_tz = timezone(timedelta(hours=3))
    local_time = now.astimezone(moscow_tz)
    time_str = local_time.strftime("%d.%m.%Y %H:%M")
    
    # Формируем сообщение об успехе
    drug_name = "Парацетамол" if drug_key == "paracetamol" else "Ибупрофен"
    form_name = "сироп" if dose_data.get("form") == "syrup" else "свечи"
    
    if dose_data.get("form") == "syrup":
        dose_text = f"{dose_mg:.0f} мг ({dose_data.get('dose_ml', 0):.1f} мл)"
    else:
        dose_text = dose_data.get("dose_text", f"{dose_mg:.0f} мг")
    
    # Получаем суточную дозу для отображения (после сохранения) для конкретного ребенка
    daily_total = await get_daily_total_mg(user_id, drug_key, child_name=child_name)
    
    # Рассчитываем время следующего приема
    min_interval_hours = drug_info.get("min_interval_hours", 4)
    next_dose_time = now + timedelta(hours=min_interval_hours)
    next_dose_local = next_dose_time.astimezone(moscow_tz)
    next_dose_str = next_dose_local.strftime("%d.%m.%Y, %H:%M")
    
    success_text = (
        f"✅ Прием лекарства записан в дневник!\n\n"
        f"• Препарат: {drug_name} ({form_name})\n"
        f"• Доза: {dose_text}\n"
        f"• Дата и время: {time_str}\n"
        f"• Профиль: {profile.child_name or 'Ребенок'}\n\n"
        f"📊 Суточная доза за последние 24 часа: {daily_total:.0f} мг\n\n"
        f"💡 Рекомендация: Следующий прием возможен не ранее {next_dose_str}\n\n"
        f"Теперь вы можете отслеживать все приемы лекарства для вашего малыша."
    )
    
    # Добавляем кнопки для просмотра дневника и возврата на главную
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    diary_buttons = [
        [InlineKeyboardButton("📖 Посмотреть дневник приема лекарств", callback_data="dose_diary")],
        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
    ]
    diary_markup = InlineKeyboardMarkup(diary_buttons)
    
    await query.message.reply_text(success_text, reply_markup=diary_markup)

async def handle_dose_diary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик просмотра дневника приема лекарств."""
    query = update.callback_query
    await query.answer(text="⏳ Загрузка дневника...", show_alert=False)
    
    user = query.from_user
    user_id = user.id
    
    # Проверяем премиум-статус
    from app.utils import is_premium_user
    is_premium = await is_premium_user(user_id)
    
    if not is_premium:
        await query.message.reply_text(
            "❌ Эта функция доступна только для премиум-пользователей.\n\n"
            "Используйте /premium чтобы узнать больше о премиум-доступе."
        )
        return
    
    # Проверяем наличие профиля
    from app.storage import get_child_profile, get_all_child_profiles, get_all_dose_events, get_daily_total_mg
    profile = await get_child_profile(user_id)
    
    if not profile:
        await query.message.reply_text(
            "❌ Чтобы просмотреть дневник, сначала создайте профиль ребенка.\n\n"
            "Используйте кнопку «👶 Создать профиль» или команду /profile_set"
        )
        return
    
    # Проверяем, сколько профилей у пользователя
    all_profiles = await get_all_child_profiles(user_id)
    show_child_names = len(all_profiles) > 1  # Показывать имена только если профилей несколько
    
    # Получаем все записи за последние 24 часа
    all_events = await get_all_dose_events(user_id)
    
    if not all_events:
        await query.message.reply_text(
            "📖 Дневник приема лекарств пуст.\n\n"
            "Записи о приемах лекарств появятся здесь после того, как вы сохраните первый прием."
        )
        return
    
    # Получаем последнюю запись
    last_event = all_events[-1]  # Последняя запись (самая новая)
    last_ts, last_drug_key, last_dose_mg, last_meta = last_event
    
    # Форматируем время последней записи
    from datetime import timedelta
    from app.utils import to_local
    last_time_local = to_local(last_ts)
    last_time_str = last_time_local.strftime("%d.%m.%Y, %H:%M")
    
    # Информация о последней записи
    last_drug_name = "Парацетамол" if last_drug_key == "paracetamol" else "Ибупрофен"
    last_form_name = "сироп" if last_meta.get("form") == "syrup" else "свечи"
    last_weight = last_meta.get("weight_kg", profile.child_weight_kg) or profile.child_weight_kg
    
    if last_meta.get("form") == "syrup":
        last_dose_text = f"{last_dose_mg:.0f} мг ({last_meta.get('dose_ml', 0):.1f} мл)"
    else:
        last_dose_text = last_meta.get("dose_text", f"{last_dose_mg:.0f} мг")
    
    # Параметры расчета для последней записи
    from app.utils import load_formulary
    formulary = load_formulary()
    drug_info = formulary["drugs"].get(last_drug_key, {})
    
    conc_text = ""
    if last_meta.get("conc_label"):
        conc_text = f"{last_meta.get('conc_label')}"
    elif last_meta.get("form") == "syrup":
        # Пытаемся восстановить концентрацию из данных
        if last_meta.get("dose_ml"):
            conc_mg_per_ml = last_dose_mg / last_meta.get("dose_ml")
            conc_text = f"{conc_mg_per_ml:.1f} мг/мл"
    
    formula_text = "10 мг/кг (ибупрофен)" if last_drug_key == "ibuprofen" else "15 мг/кг (парацетамол)"
    interval_text = "6-8 часов" if last_drug_key == "ibuprofen" else "4-6 часов"
    max_daily_per_kg = drug_info.get("max_daily_mg_per_kg", 0)
    
    # Формируем список всех записей за сегодня (за последние 24 часа)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    moscow_tz = timezone(timedelta(hours=3))
    
    events_today = []
    for ts, drug_key, dose_mg, meta in all_events:
        ts_local = to_local(ts)
        time_str = ts_local.strftime("%d.%m.%Y, %H:%M")
        drug_name = "Парацетамол" if drug_key == "paracetamol" else "Ибупрофен"
        # Если профилей несколько, показываем имя ребенка
        if show_child_names and meta.get("child_name"):
            child_name = meta.get("child_name")
            events_today.append(f"{time_str} — {drug_name}, {dose_mg:.0f} мг ({child_name})")
        else:
            events_today.append(f"{time_str} — {drug_name}, {dose_mg:.0f} мг")
    
    # Считаем суточную дозу для конкретного ребенка (из последней записи)
    last_child_name = last_meta.get("child_name")
    if last_child_name:
        # Если есть имя в последней записи, считаем суточную дозу для этого ребенка
        paracetamol_total = await get_daily_total_mg(user_id, "paracetamol", child_name=last_child_name)
        ibuprofen_total = await get_daily_total_mg(user_id, "ibuprofen", child_name=last_child_name)
        current_total = await get_daily_total_mg(user_id, last_drug_key, child_name=last_child_name)
    else:
        # Для обратной совместимости (старые записи без имени)
        paracetamol_total = await get_daily_total_mg(user_id, "paracetamol")
        ibuprofen_total = await get_daily_total_mg(user_id, "ibuprofen")
        current_total = await get_daily_total_mg(user_id, last_drug_key)
    
    # Определяем, какой препарат показывать (берем последний использованный)
    current_drug_key = last_drug_key
    current_max = max_daily_per_kg * last_weight
    current_percent = int((current_total / current_max * 100)) if current_max > 0 else 0
    
    # Следующий прием
    min_interval_hours = drug_info.get("min_interval_hours", 4)
    next_dose_time = last_ts + timedelta(hours=min_interval_hours)
    next_dose_local = to_local(next_dose_time)
    next_dose_str = next_dose_local.strftime("%d.%m.%Y, %H:%M")
    
    # Формируем сообщение
    # Добавляем имя ребенка, если профилей несколько
    child_name_info = ""
    if show_child_names and last_meta.get("child_name"):
        child_name_info = f"• Ребенок: {last_meta.get('child_name')}\n"
    
    diary_text = (
        f"📖 **Дневник приема лекарств**\n\n"
        f"💊 **Последняя запись:**\n\n"
        f"{child_name_info}"
        f"• Лекарство: {last_drug_name} ({last_form_name})\n"
        f"• Вес ребенка: {last_weight} кг\n"
        f"• Дата и время: {last_time_str}\n"
        f"• Доза: {last_dose_text}\n\n"
        f"📊 **Параметры расчета:**\n\n"
    )
    
    if conc_text:
        diary_text += f"• Концентрация: {conc_text}\n"
    diary_text += (
        f"• Формула: {formula_text}\n"
        f"• Интервал между приёмами: {interval_text}\n"
        f"• Максимальная суточная доза: {max_daily_per_kg} мг/кг\n\n"
    )
    
    diary_text += f"📋 **Все записи за сегодня:**\n\n"
    for event_str in events_today:
        diary_text += f"{event_str}\n"
    
    # Добавляем имя ребенка в информацию о суточной дозе, если профилей несколько
    child_name_suffix = ""
    if show_child_names and last_child_name:
        child_name_suffix = f" ({last_child_name})"
    
    diary_text += (
        f"\n⚠️ **Суточная доза{child_name_suffix}**: {current_total:.0f} мг из {current_max:.0f} мг ({current_percent}%)\n\n"
        f"💡 **Рекомендация**: Следующий прием возможен не ранее {next_dose_str}"
    )
    
    # Добавляем кнопку "На главную"
    diary_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
    ])
    
    await query.message.reply_text(diary_text, parse_mode="Markdown", reply_markup=diary_keyboard)

async def handle_premium_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок покупки премиум - отправка инвойсов через ЮKassa или Telegram Payments."""
    query = update.callback_query
    
    # Пытаемся показать индикатор загрузки, но не блокируем выполнение при ошибке
    try:
        await query.answer(text="⏳ Подготовка платежа...", show_alert=False)
    except Exception as answer_error:
        from telegram.error import TimedOut, NetworkError
        if isinstance(answer_error, (TimedOut, NetworkError)):
            logging.warning(f"⚠️ Таймаут/ошибка сети при answer callback query для user {query.from_user.id}, продолжаем выполнение")
        else:
            logging.warning(f"⚠️ Ошибка при answer callback query для user {query.from_user.id}: {answer_error}")
        # Продолжаем выполнение - индикатор не критичен
    
    # Проверяем, настроен ли ЮKassa (приоритет) или Telegram Payments
    use_yookassa = is_yookassa_configured()
    
    if not use_yookassa and not PROVIDER_TOKEN:
        await query.message.reply_text(
            "❌ Платежная система не настроена.\n\n"
            "Обратитесь к администратору бота."
        )
        return
    
    user_id = query.from_user.id
    
    if query.data == "premium_buy_1month":
        # Премиум на 1 месяц - 99₽
        amount = 99.0
        subscription_type = "1month"
        subscription_days = 30
        title = "🌟 Премиум-подписка на 1 месяц"
        description = (
            "Получите доступ ко всем премиум-функциям бота на 1 месяц:\n\n"
            "• 👶 Профиль ребенка\n"
            "• 📊 Дневник лекарств\n"
            "• 🚩 Красные флаги"
        )
        
        if use_yookassa:
            # Используем ЮKassa API (поддерживает СБП)
            try:
                # Получаем username бота для создания правильного return_url
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username if bot_info else None
                
                payment_result = await create_payment(
                    user_id=user_id,
                    amount=amount,
                    description=description,
                    subscription_type=subscription_type,
                    subscription_days=subscription_days,
                    bot_username=bot_username
                )
                
                if payment_result:
                    payment_id = payment_result["payment_id"]
                    confirmation_url = payment_result["confirmation_url"]
                    payload = payment_result["payload"]
                    
                    # Сохраняем платеж в БД
                    try:
                        await save_payment(
                            user_id=user_id,
                            invoice_payload=payload,
                            amount=int(amount * 100),  # в копейках
                            currency="RUB",
                            subscription_type=subscription_type,
                            subscription_days=subscription_days,
                            yookassa_payment_id=payment_id,
                            confirmation_url=confirmation_url
                        )
                        logging.info(f"✅ Платеж ЮKassa сохранен: user_id={user_id}, payment_id={payment_id}")
                    except Exception as save_error:
                        logging.error(f"❌ Ошибка при сохранении платежа: {save_error}", exc_info=True)
                    
                    # Отправляем пользователю ссылку на оплату
                    payment_text = (
                        f"{title}\n\n"
                        f"{description}\n\n"
                        f"💰 Сумма: {amount:.0f}₽\n\n"
                        f"💳 Для оплаты нажмите кнопку 'Оплатить' ниже.\n\n"
                        f"💡 После оплаты премиум будет активирован автоматически.\n"
                        f"📱 После оплаты нажмите кнопку 'Вернуться в бот' или перейдите в бот."
                    )
                    
                    payment_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Оплатить", url=confirmation_url)],
                        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
                    ])
                    
                    await query.message.reply_text(payment_text, reply_markup=payment_keyboard)
                else:
                    raise Exception("Не удалось создать платеж через ЮKassa")
                    
            except Exception as e:
                logging.error(f"❌ Ошибка при создании платежа через ЮKassa: {e}", exc_info=True)
                await query.message.reply_text(
                    "❌ Ошибка при создании платежа.\n\n"
                    "Пожалуйста, попробуйте позже или свяжитесь с поддержкой."
                )
        else:
            # Используем Telegram Payments (старый способ)
            payload = f"premium_1month_{user_id}_{int(datetime.now(timezone.utc).timestamp())}"
            prices = [LabeledPrice("Премиум-подписка на 1 месяц", 99 * 100)]  # 99₽ в копейках
            
            try:
                await query.message.reply_invoice(
                    title=title,
                    description=description,
                    payload=payload,
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=prices,
                    need_name=False,
                    need_phone_number=False,
                    need_email=False,
                    need_shipping_address=False,
                    send_phone_number_to_provider=False,
                    send_email_to_provider=False,
                    is_flexible=False,
                )
                # Сохраняем информацию о платеже в БД (amount в копейках)
                try:
                    await save_payment(user_id, payload, 99 * 100, "RUB", "1month", 30)
                    logging.info(f"✅ Платеж сохранен в БД: user_id={user_id}, payload={payload}")
                except Exception as save_error:
                    logging.error(f"❌ Ошибка при сохранении платежа: {save_error}", exc_info=True)
                    # Продолжаем - инвойс уже отправлен
            except Exception as e:
                from telegram.error import TimedOut, NetworkError
                error_type = type(e).__name__
                logging.error(f"❌ Ошибка при отправке инвойса для 1 месяца: {error_type}: {e}", exc_info=True)
                
                # Пытаемся отправить сообщение об ошибке
                try:
                    if isinstance(e, (TimedOut, NetworkError)):
                        error_msg = (
                            "⚠️ Проблема с подключением к серверу.\n\n"
                            "Пожалуйста, попробуйте еще раз через несколько секунд."
                        )
                    else:
                        error_msg = (
                            "❌ Ошибка при создании счета.\n\n"
                            "Пожалуйста, попробуйте позже или свяжитесь с поддержкой."
                        )
                    await query.message.reply_text(error_msg)
                except Exception as send_error:
                    logging.error(f"❌ Не удалось отправить сообщение об ошибке: {send_error}")
    
    elif query.data == "premium_buy_3months":
        # Премиум на 3 месяца - 270₽
        amount = 270.0
        subscription_type = "3months"
        subscription_days = 90
        title = "🌟 Премиум-подписка на 3 месяца"
        description = (
            "Получите доступ ко всем премиум-функциям бота на 3 месяца:\n\n"
            "• 👶 Профиль ребенка\n"
            "• 📊 Дневник лекарств\n"
            "• 🚩 Красные флаги\n\n"
            "💰 Выгоднее на 9%!"
        )
        
        if use_yookassa:
            # Используем ЮKassa API (поддерживает СБП)
            try:
                # Получаем username бота для создания правильного return_url
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username if bot_info else None
                
                payment_result = await create_payment(
                    user_id=user_id,
                    amount=amount,
                    description=description,
                    subscription_type=subscription_type,
                    subscription_days=subscription_days,
                    bot_username=bot_username
                )
                
                if payment_result:
                    payment_id = payment_result["payment_id"]
                    confirmation_url = payment_result["confirmation_url"]
                    payload = payment_result["payload"]
                    
                    # Сохраняем платеж в БД
                    try:
                        await save_payment(
                            user_id=user_id,
                            invoice_payload=payload,
                            amount=int(amount * 100),  # в копейках
                            currency="RUB",
                            subscription_type=subscription_type,
                            subscription_days=subscription_days,
                            yookassa_payment_id=payment_id,
                            confirmation_url=confirmation_url
                        )
                        logging.info(f"✅ Платеж ЮKassa сохранен: user_id={user_id}, payment_id={payment_id}")
                    except Exception as save_error:
                        logging.error(f"❌ Ошибка при сохранении платежа: {save_error}", exc_info=True)
                    
                    # Отправляем пользователю ссылку на оплату
                    payment_text = (
                        f"{title}\n\n"
                        f"{description}\n\n"
                        f"💰 Сумма: {amount:.0f}₽\n\n"
                        f"💳 Для оплаты нажмите кнопку 'Оплатить' ниже.\n\n"
                        f"💡 После оплаты премиум будет активирован автоматически.\n"
                        f"📱 После оплаты нажмите кнопку 'Вернуться в бот' или перейдите в бот."
                    )
                    
                    payment_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Оплатить", url=confirmation_url)],
                        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
                    ])
                    
                    await query.message.reply_text(payment_text, reply_markup=payment_keyboard)
                else:
                    raise Exception("Не удалось создать платеж через ЮKassa")
                    
            except Exception as e:
                logging.error(f"❌ Ошибка при создании платежа через ЮKassa: {e}", exc_info=True)
                await query.message.reply_text(
                    "❌ Ошибка при создании платежа.\n\n"
                    "Пожалуйста, попробуйте позже или свяжитесь с поддержкой."
                )
        else:
            # Используем Telegram Payments (старый способ)
            payload = f"premium_3months_{user_id}_{int(datetime.now(timezone.utc).timestamp())}"
            prices = [LabeledPrice("Премиум-подписка на 3 месяца", 270 * 100)]  # 270₽ в копейках
            
            try:
                await query.message.reply_invoice(
                    title=title,
                    description=description,
                    payload=payload,
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=prices,
                    need_name=False,
                    need_phone_number=False,
                    need_email=False,
                    need_shipping_address=False,
                    send_phone_number_to_provider=False,
                    send_email_to_provider=False,
                    is_flexible=False,
                )
                # Сохраняем информацию о платеже в БД (amount в копейках) - не блокируем при ошибке
                try:
                    await save_payment(user_id, payload, 270 * 100, "RUB", "3months", 90)
                    logging.info(f"✅ Платеж сохранен в БД: user_id={user_id}, payload={payload}")
                except Exception as save_error:
                    logging.error(f"❌ Ошибка при сохранении платежа: {save_error}", exc_info=True)
                    # Продолжаем - инвойс уже отправлен
            except Exception as e:
                from telegram.error import TimedOut, NetworkError
                error_type = type(e).__name__
                logging.error(f"❌ Ошибка при отправке инвойса для 3 месяцев: {error_type}: {e}", exc_info=True)
                
                # Пытаемся отправить сообщение об ошибке
                try:
                    if isinstance(e, (TimedOut, NetworkError)):
                        error_msg = (
                            "⚠️ Проблема с подключением к серверу.\n\n"
                            "Пожалуйста, попробуйте еще раз через несколько секунд."
                        )
                    else:
                        error_msg = (
                            "❌ Ошибка при создании счета.\n\n"
                            "Пожалуйста, попробуйте позже или свяжитесь с поддержкой."
                        )
                    await query.message.reply_text(error_msg)
                except Exception as send_error:
                    logging.error(f"❌ Не удалось отправить сообщение об ошибке: {send_error}")
    
    elif query.data == "premium_support":
        await query.message.reply_text(
            "❤️ Спасибо за желание поддержать проект!\n\n"
            "💳 Система поддержки находится в разработке.\n\n"
            "Скоро вы сможете поддержать проект! "
            "Ваша поддержка поможет развивать бота и делать его еще лучше 💚"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список команд. Для премиум и бесплатных пользователей разные списки."""
    user = update.effective_user
    is_premium = await is_premium_user(user.id)
    logging.info(f"help_command: User {user.id} - Premium status: {is_premium}")
    
    if is_premium:
        # Полный список команд для премиум-пользователей
        help_text = (
            "Привет! Я помогу быстро и бережно посчитать разовую дозу сиропа для малыша 👶💊\n\n"
            "Команды:\n\n"
            "🛠 /calculate — расчёт дозы (шаг за шагом)\n\n"
            "👶 /profile — посмотреть профиль ребенка\n"
            "👶 /profile_set — создать/изменить профиль\n"
            "👶 /profile_delete — удалить профиль\n\n"
            "🚩 /redflags — красные флаги при ОРВИ (когда нужна срочная помощь)\n\n"
            "🚩 /redflags_gi — красные флаги при поносе/рвоте и обезвоживании\n\n"
            "📝 /feedback — предложения и обратная связь 💬\n\n"
            "💡 /help — помощь и подсказки ℹ️\n\n"
            "Подсказка: команды появляются в меню «/».\n\n"
            "Важно: я ИИ-помощник, не врач. При тревожных симптомах обращайтесь к педиатру. "
            "В экстренной ситуации звоните 103/112."
        )
    else:
        # Упрощенный список для бесплатных пользователей
        help_text = (
            "Привет! Помогу рассчитать лекарство для вашего крохи быстро и без лишних волнений 👶💊\n\n"
            "Чем я могу быть полезен:\n\n"
            "🛠 /calculate — Начнем расчет дозы. Я буду задавать вопросы и направлять вас на каждом шагу.\n\n"
            "📝 /feedback — Обратная связь поможет мне стать лучше для вас.\n\n"
            "⭐ /premium — Узнать о Премиум-доступе и дополнительных возможностях\n\n"
            "💡 /help — Если запутались, я всегда подскажу.\n\n"
            "Подсказка: просто нажми на команду в меню «/», и я сработаю!\n\n"
            "И самое важное: Я — ваш ИИ-помощник. Мои расчеты — это ориентир. "
            "При любых сомнениях в состоянии ребенка консультируйтесь с врачом. "
            "В экстренных случаях — 103/112."
        )
    
    await update.message.reply_text(help_text)

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать информацию о премиум-доступе и кнопки для покупки."""
    user = update.effective_user
    is_premium = await is_premium_user(user.id)
    
    if is_premium:
        await update.message.reply_text(
            "✨ Спасибо за Премиум-подписку! Вы уже получаете все дополнительные возможности.\n\n"
            "Если у вас есть вопросы, используйте /help для списка всех команд."
        )
        return
    
    # Информация о премиум для бесплатных пользователей
    premium_text = (
        "🌟 Премиум-доступ — ваше спокойствие и удобство!\n\n"
        "Теперь вы можете не запоминать, когда и сколько лекарства вы дали малышу. "
        "Мы сделаем это за вас!\n\n"
        "С Премиумом вы получаете:\n\n"
        "• 👶 Профиль ребенка — Создайте и сохраните вес, возраст и другие данные. "
        "Больше не нужно вводить их каждый раз!\n\n"
        "• 📊 Дневник лекарств — Отслеживайте каждый прием жаропонижающего. "
        "Мы покажем, сколько уже дали за сутки и предупредим, если приближаетесь к максимуму.\n\n"
        "И помните: все основные расчеты разовых доз остаются абсолютно бесплатными! 💚 "
        "Премиум — это для вашего дополнительного комфорта и уверенности."
    )
    
    # Создаем кнопки для покупки премиум
    premium_keyboard = [
        [InlineKeyboardButton("🌟 1 месяц - 99₽", callback_data="premium_buy_1month")],
        [InlineKeyboardButton("🌟 3 месяца - 270₽", callback_data="premium_buy_3months")],
        [InlineKeyboardButton("❤️ Поддержать проект", callback_data="premium_support")],
        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
    ]
    premium_markup = InlineKeyboardMarkup(premium_keyboard)
    
    await update.message.reply_text(premium_text, reply_markup=premium_markup)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику бота (только для администратора)."""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    if not ADMIN_USER_ID or user_id != ADMIN_USER_ID:
        await update.message.reply_text(
            "❌ У вас нет доступа к этой команде.\n\n"
            "Эта команда доступна только администратору бота."
        )
        logging.warning(f"User {user_id} attempted to access /stats command")
        return
    
    try:
        # Получаем статистику с таймаутом (30 секунд)
        try:
            stats = await asyncio.wait_for(get_bot_statistics(), timeout=30.0)
        except asyncio.TimeoutError:
            logging.error(f"Timeout while getting statistics for admin {user_id}")
            await update.message.reply_text(
                "❌ Превышено время ожидания при получении статистики.\n\n"
                "База данных может быть перегружена. Попробуйте позже."
            )
            return
        except Exception as stats_error:
            logging.error(f"Error getting statistics: {stats_error}", exc_info=True)
            raise
        
        # Форматируем выручку (из копеек в рубли)
        revenue_rub = stats["revenue_total"] / 100 if stats["revenue_total"] else 0
        
        # Формируем сообщение со статистикой
        stats_text = (
            f"📊 **Статистика бота**\n\n"
            f"👥 **Пользователи:**\n"
            f"• Всего пользователей: {stats['total_users']}\n"
            f"• Активных за 30 дней: {stats['active_users_30d']}\n"
            f"• Активных за 7 дней: {stats['active_users_7d']}\n\n"
            f"⭐ **Премиум подписки:**\n"
            f"• Активных подписок: {stats['premium_active']}\n"
            f"• Всего оформлено: {stats['premium_total']}\n\n"
            f"💳 **Платежи:**\n"
            f"• Успешных платежей: {stats['payments_completed']}\n"
            f"• Ожидающих платежей: {stats['payments_pending']}\n"
            f"• Общая выручка: {revenue_rub:.2f} ₽\n\n"
            f"📦 **Подписки по типам:**\n"
            f"• На 1 месяц: {stats['subscriptions_1month']}\n"
            f"• На 3 месяца: {stats['subscriptions_3months']}\n"
        )
        
        # Отправляем сообщение с обработкой таймаутов
        try:
            await asyncio.wait_for(
                update.message.reply_text(stats_text, parse_mode="Markdown"),
                timeout=10.0
            )
        except (asyncio.TimeoutError, Exception) as send_error:
            from telegram.error import TimedOut
            if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
                # Пробуем отправить упрощенное сообщение
                try:
                    simplified_text = (
                        f"📊 Статистика бота\n\n"
                        f"👥 Пользователей: {stats['total_users']}\n"
                        f"⭐ Премиум активных: {stats['premium_active']}\n"
                        f"💳 Платежей: {stats['payments_completed']}\n"
                        f"💰 Выручка: {revenue_rub:.2f} ₽"
                    )
                    await asyncio.wait_for(
                        update.message.reply_text(simplified_text),
                        timeout=5.0
                    )
                except Exception:
                    pass
            else:
                raise
        
        logging.info(f"Admin {user_id} requested statistics")
        
    except Exception as e:
        logging.error(f"Error in stats_command: {e}", exc_info=True)
        error_details = str(e)
        # Отправляем сообщение об ошибке с обработкой таймаутов
        try:
            await asyncio.wait_for(
                update.message.reply_text(
                    f"❌ Произошла ошибка при получении статистики.\n\n"
                    f"Ошибка: {error_details}\n\n"
                    f"Пожалуйста, проверьте логи для подробностей."
                ),
                timeout=5.0
            )
        except Exception:
            pass

async def test_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для тестирования премиум-статуса (переключает статус)."""
    if not update.message:
        logging.warning("Received /test_premium command but update.message is None")
        return
    
    logging.info(f"Received /test_premium command from user {update.effective_user.id}")
    try:
        user = update.effective_user
        user_id = user.id
        
        # Получаем текущий статус
        current_status = await is_user_premium(user_id)
        
        # Переключаем статус
        new_status = not current_status
        
        # Устанавливаем новый статус (на 1 год для тестирования)
        premium_until = datetime.now(timezone.utc) + timedelta(days=365) if new_status else None
        await set_user_premium(user_id, new_status, premium_until)
        
        if new_status:
            await update.message.reply_text(
                f"✅ Премиум-статус активирован для тестирования!\n\n"
                f"Теперь вы можете протестировать все премиум-функции:\n"
                f"• 👶 Создание профиля ребенка\n"
                f"• 📊 Дневник лекарств\n\n"
                f"Используйте /start чтобы увидеть изменения.\n\n"
                f"Чтобы отключить премиум, используйте /test_premium снова."
            )
        else:
            # Создаем кнопки для отключенного премиум-статуса
            premium_off_keyboard = [
                [InlineKeyboardButton("⭐ Узнать о Премиум", callback_data="start_premium_info")],
                [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
            ]
            premium_off_markup = InlineKeyboardMarkup(premium_off_keyboard)
            
            await update.message.reply_text(
                f"❌ Премиум-статус отключен.\n\n"
                f"Теперь вы снова бесплатный пользователь.",
                reply_markup=premium_off_markup
            )
    except Exception as e:
        logging.error(f"Error in test_premium_command: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "Произошла ошибка при обработке команды. Пожалуйста, попробуйте еще раз."
            )
        except:
            pass

def check_running_bot_processes():
    """Проверяет, не запущен ли уже другой экземпляр бота."""
    try:
        # Ищем процессы Python, которые запускают наш бот
        current_pid = os.getpid()
        script_name = os.path.basename(__file__)
        
        # Проверяем процессы через ps (работает на macOS/Linux)
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            bot_processes = []
            for line in lines:
                # Ищем процессы, которые запускают app.main или bot.py
                if ('app.main' in line or 'bot.py' in line) and 'python' in line.lower():
                    parts = line.split()
                    if len(parts) > 1:
                        pid = int(parts[1])
                        if pid != current_pid:  # Игнорируем текущий процесс
                            bot_processes.append(pid)
            
            if bot_processes:
                logging.warning(f"⚠️ Обнаружены запущенные процессы бота: {bot_processes}")
                logging.warning("⚠️ Это может вызывать конфликты 409!")
                logging.warning("⚠️ Рекомендуется завершить старые процессы перед запуском.")
                return bot_processes
        
        return []
    except Exception as e:
        logging.debug(f"Не удалось проверить процессы (это нормально): {e}")
        return []

async def send_premium_expiry_notification(context: ContextTypes.DEFAULT_TYPE, user_id: int, days_until: int) -> None:
    """
    Отправить уведомление пользователю о скором истечении премиума.
    
    Args:
        context: Контекст приложения
        user_id: ID пользователя
        days_until: Количество дней до истечения
    """
    try:
        # Формируем сообщение
        if days_until == 3:
            days_text = "3 дня"
        elif days_until == 4:
            days_text = "4 дня"
        elif days_until == 5:
            days_text = "5 дней"
        else:
            days_text = f"{days_until} дней"
        
        notification_text = (
            f"⏰ **Напоминание о премиум-подписке**\n\n"
            f"Ваша премиум-подписка истекает через {days_text}.\n\n"
            f"Чтобы продолжить пользоваться всеми удобными функциями:\n"
            f"• 👶 Профиль ребенка\n"
            f"• 📊 Дневник лекарств\n"
            f"• 🚩 Красные флаги\n\n"
            f"Продлите подписку прямо сейчас! ✨"
        )
        
        # Создаем кнопки для продления
        premium_keyboard = [
            [InlineKeyboardButton("🌟 1 месяц - 99₽", callback_data="premium_buy_1month")],
            [InlineKeyboardButton("🌟 3 месяца - 270₽", callback_data="premium_buy_3months")],
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ]
        premium_markup = InlineKeyboardMarkup(premium_keyboard)
        
        # Отправляем сообщение
        await context.bot.send_message(
            chat_id=user_id,
            text=notification_text,
            reply_markup=premium_markup,
            parse_mode="Markdown"
        )
        
        logging.info(f"✅ Отправлено уведомление о истечении премиума пользователю {user_id} (осталось {days_until} дней)")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке уведомления пользователю {user_id}: {e}", exc_info=True)

async def send_premium_expired_notification(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """
    Отправить уведомление пользователю об истечении премиума.
    
    Args:
        context: Контекст приложения
        user_id: ID пользователя
    """
    try:
        notification_text = (
            f"⏰ **Ваша премиум-подписка истекла**\n\n"
            f"К сожалению, срок действия вашей премиум-подписки закончился.\n\n"
            f"Чтобы снова получить доступ ко всем удобным функциям:\n"
            f"• 👶 Профиль ребенка\n"
            f"• 📊 Дневник лекарств\n"
            f"• 🚩 Красные флаги\n\n"
            f"Продлите подписку прямо сейчас! ✨"
        )
        
        # Создаем кнопки для продления
        premium_keyboard = [
            [InlineKeyboardButton("🌟 1 месяц - 99₽", callback_data="premium_buy_1month")],
            [InlineKeyboardButton("🌟 3 месяца - 270₽", callback_data="premium_buy_3months")],
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ]
        premium_markup = InlineKeyboardMarkup(premium_keyboard)
        
        # Отправляем сообщение
        await context.bot.send_message(
            chat_id=user_id,
            text=notification_text,
            reply_markup=premium_markup,
            parse_mode="Markdown"
        )
        
        logging.info(f"✅ Отправлено уведомление об истечении премиума пользователю {user_id}")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке уведомления об истечении пользователю {user_id}: {e}", exc_info=True)

async def check_and_send_premium_expiry_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача для проверки и отправки уведомлений о истечении премиума.
    """
    try:
        # 1. Получаем пользователей с истекающим премиумом (3-5 дней)
        users_expiring = await get_users_with_expiring_premium(min_days=3, max_days=5)
        
        if users_expiring:
            logging.info(f"Найдено {len(users_expiring)} пользователей с истекающим премиумом (3-5 дней)")
            
            for user_id, premium_until, days_until in users_expiring:
                # Проверяем, не было ли уже отправлено уведомление
                if await has_notification_been_sent(user_id, premium_until):
                    logging.debug(f"Уведомление уже отправлено пользователю {user_id} для даты {premium_until}")
                    continue
                
                # Отправляем уведомление
                await send_premium_expiry_notification(context, user_id, days_until)
                
                # Отмечаем, что уведомление отправлено
                await mark_notification_sent(user_id, premium_until, days_until)
                
                # Небольшая задержка между отправками, чтобы не перегружать API
                await asyncio.sleep(0.5)
            
            logging.info(f"✅ Обработано {len(users_expiring)} уведомлений о скором истечении премиума")
        
        # 2. Получаем пользователей с истекшим премиумом (сегодня)
        users_expired = await get_users_with_expired_premium()
        
        if users_expired:
            logging.info(f"Найдено {len(users_expired)} пользователей с истекшим премиумом")
            
            for user_id, premium_until in users_expired:
                # Проверяем, не было ли уже отправлено уведомление об истечении
                if await has_notification_been_sent(user_id, premium_until):
                    logging.debug(f"Уведомление об истечении уже отправлено пользователю {user_id} для даты {premium_until}")
                    continue
                
                # Отправляем уведомление об истечении
                await send_premium_expired_notification(context, user_id)
                
                # Отмечаем, что уведомление отправлено (используем days_until_expiry = 0 для истекших)
                await mark_notification_sent(user_id, premium_until, 0)
                
                # Обновляем статус премиума в БД (на случай если он еще не обновлен)
                from app.storage import set_user_premium
                await set_user_premium(user_id, False, None)
                
                # Небольшая задержка между отправками
                await asyncio.sleep(0.5)
            
            logging.info(f"✅ Обработано {len(users_expired)} уведомлений об истечении премиума")
        
        if not users_expiring and not users_expired:
            logging.debug("Нет пользователей с истекающим или истекшим премиумом")
            
    except Exception as e:
        logging.error(f"❌ Ошибка при проверке уведомлений о истечении премиума: {e}", exc_info=True)

async def disable_expired_subscriptions_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача для автоматического отключения всех истекших премиум подписок.
    """
    try:
        disabled_count = await disable_expired_premium_subscriptions()
        if disabled_count > 0:
            logging.info(f"✅ Автоматически отключено {disabled_count} истекших премиум подписок")
    except Exception as e:
        logging.error(f"❌ Ошибка при отключении истекших подписок: {e}", exc_info=True)


async def check_yookassa_payments_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача для проверки статуса pending платежей через ЮKassa.
    Если платеж успешно оплачен, активирует премиум.
    """
    if not is_yookassa_configured():
        return  # ЮKassa не настроен, пропускаем проверку
    
    try:
        # Получаем список pending платежей
        pending_payments = await check_pending_payments()
        
        if not pending_payments:
            return  # Нет pending платежей
        
        logging.info(f"🔍 Проверка статуса {len(pending_payments)} pending платежей через ЮKassa...")
        
        for payment_info in pending_payments:
            payment_id = payment_info["payment_id"]
            user_id = payment_info["user_id"]
            
            try:
                # Получаем статус платежа из ЮKassa
                payment_status = await get_payment_status(payment_id)
                
                if not payment_status:
                    continue  # Не удалось получить статус, пропускаем
                
                status = payment_status.get("status")
                
                if status == "succeeded":
                    # Платеж успешно оплачен - активируем премиум
                    logging.info(f"✅ Платеж {payment_id} успешно оплачен, активируем премиум для user_id={user_id}")
                    
                    result = await complete_yookassa_payment(payment_id)
                    
                    # Если result None, но платеж succeeded - проверяем, может быть премиум уже активирован
                    if not result:
                        logging.warning(f"⚠️ complete_yookassa_payment вернул None для платежа {payment_id}, проверяем статус премиума...")
                        # Проверяем, есть ли премиум у пользователя
                        from app.storage import is_user_premium
                        has_premium = await is_user_premium(user_id)
                        if has_premium:
                            logging.info(f"ℹ️ Премиум уже активирован для user_id={user_id}, пропускаем отправку уведомления")
                            continue
                        else:
                            logging.error(f"❌ Премиум НЕ активирован для user_id={user_id} после успешной оплаты платежа {payment_id}!")
                            # Пытаемся активировать вручную
                            try:
                                from app.storage import set_user_premium
                                premium_until = datetime.now(timezone.utc) + timedelta(days=30)  # По умолчанию 30 дней
                                await set_user_premium(user_id, True, premium_until)
                                logging.info(f"✅ Премиум активирован вручную для user_id={user_id}")
                                result = {
                                    "user_id": user_id,
                                    "subscription_days": 30,
                                    "premium_until": premium_until,
                                    "payment_id": payment_id
                                }
                            except Exception as manual_error:
                                logging.error(f"❌ Не удалось активировать премиум вручную: {manual_error}", exc_info=True)
                                continue
                    
                    if result:
                        # Отправляем уведомление пользователю
                        premium_until = result["premium_until"]
                        subscription_days = result["subscription_days"]
                        
                        # Форматируем дату окончания для отображения
                        moscow_tz = timezone(timedelta(hours=3))
                        until_local = premium_until.astimezone(moscow_tz)
                        until_str = until_local.strftime("%d.%m.%Y")
                        
                        success_text = (
                            f"✅ **Платеж успешно обработан!**\n\n"
                            f"✨ Ваша премиум-подписка активирована на {subscription_days} дней!\n\n"
                            f"📅 Подписка действует до: {until_str}\n\n"
                            f"Теперь вам доступны все премиум-функции:\n"
                            f"• 👶 Профиль ребенка\n"
                            f"• 📊 Дневник лекарств\n"
                            f"• 🚩 Красные флаги\n\n"
                            f"Спасибо за поддержку! 💚"
                        )
                        
                        # Добавляем кнопку "На главную"
                        home_keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
                        ])
                        
                        try:
                            # Проверяем, не было ли уже отправлено уведомление для этого платежа
                            # (защита от дублирования при одновременной обработке)
                            import aiosqlite
                            async with aiosqlite.connect(DB_PATH) as check_db:
                                check_db.row_factory = aiosqlite.Row
                                async with check_db.execute("""
                                    SELECT notification_sent_at FROM payments
                                    WHERE yookassa_payment_id = ?
                                """, (payment_id,)) as check_cursor:
                                    check_row = await check_cursor.fetchone()
                                    if check_row:
                                        # Проверяем наличие notification_sent_at (sqlite3.Row не имеет метода .get())
                                        try:
                                            notification_sent = check_row["notification_sent_at"] if check_row["notification_sent_at"] else None
                                        except (KeyError, IndexError):
                                            notification_sent = None
                                        
                                        if notification_sent:
                                            logging.info(f"ℹ️ Уведомление для платежа {payment_id} уже было отправлено ранее, пропускаем")
                                            continue  # Пропускаем отправку, если уже отправлено
                            
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=success_text,
                                parse_mode="Markdown",
                                reply_markup=home_keyboard
                            )
                            logging.info(f"✅ Уведомление об активации премиума отправлено user_id={user_id}")
                            
                            # Отмечаем, что уведомление отправлено (атомарно, чтобы избежать дублирования)
                            await mark_payment_notification_sent(payment_id)
                        except Exception as send_error:
                            logging.error(f"❌ Не удалось отправить уведомление user_id={user_id}: {send_error}")
                    else:
                        logging.warning(f"⚠️ Не удалось активировать премиум для платежа {payment_id}")
                
                elif status == "canceled":
                    logging.info(f"ℹ️ Платеж {payment_id} отменен")
                    # Обновляем статус в БД, чтобы не проверять его повторно
                    try:
                        from app.storage import DB_PATH
                        import aiosqlite
                        async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
                            await db.execute("""
                                UPDATE payments
                                SET status = 'canceled'
                                WHERE yookassa_payment_id = ? AND status = 'pending'
                            """, (payment_id,))
                            await db.commit()
                            logging.debug(f"✅ Статус платежа {payment_id} обновлен на 'canceled' в БД")
                    except Exception as update_error:
                        logging.warning(f"⚠️ Не удалось обновить статус отмененного платежа {payment_id}: {update_error}")
                
                # Небольшая задержка между проверками, чтобы не перегружать API
                await asyncio.sleep(0.5)
                
            except Exception as payment_error:
                logging.error(f"❌ Ошибка при проверке платежа {payment_id}: {payment_error}", exc_info=True)
                continue
        
        logging.info(f"✅ Проверка статуса платежей завершена")
        
    except Exception as e:
        logging.error(f"❌ Ошибка при проверке статуса платежей ЮKassa: {e}", exc_info=True)

async def post_init(application: Application) -> None:
    """Инициализация БД при старте приложения."""
    await init_db()
    logging.info("База данных инициализирована")
    
    # Запускаем периодическую задачу для проверки уведомлений о истечении премиума
    # Проверяем каждый день в 10:00 по UTC (13:00 по Москве)
    job_queue = application.job_queue
    if job_queue:
        # Запускаем проверку сразу при старте (через 10 секунд, для тестирования)
        job_queue.run_once(check_and_send_premium_expiry_notifications, when=10)
        
        # Затем запускаем ежедневную проверку в 10:00 UTC
        # Используем time.time() для создания объекта time
        check_time = dt_time(hour=10, minute=0, second=0)
        job_queue.run_daily(
            check_and_send_premium_expiry_notifications,
            time=check_time,
            name="premium_expiry_check"
        )
        logging.info("✅ Периодическая задача для проверки уведомлений о истечении премиума настроена (ежедневно в 10:00 UTC)")
        
        # Запускаем задачу для автоматического отключения истекших подписок
        # Запускаем сразу при старте (через 30 секунд)
        job_queue.run_once(disable_expired_subscriptions_task, when=30)
        
        # Затем запускаем ежедневно в 00:00 UTC (03:00 по Москве) для отключения истекших подписок
        disable_time = dt_time(hour=0, minute=0, second=0)
        job_queue.run_daily(
            disable_expired_subscriptions_task,
            time=disable_time,
            name="disable_expired_premium"
        )
        logging.info("✅ Периодическая задача для отключения истекших премиум подписок настроена (ежедневно в 00:00 UTC)")
        
        # Запускаем периодическую проверку статуса платежей ЮKassa (каждые 5 минут)
        if is_yookassa_configured():
            job_queue.run_repeating(
                check_yookassa_payments_status,
                interval=300,  # 5 минут
                first=60,  # Первый запуск через 1 минуту после старта
                name="yookassa_payments_check"
            )
            logging.info("✅ Периодическая проверка статуса платежей ЮKassa настроена (каждые 5 минут)")
    
    # Явно очищаем webhook перед запуском polling с несколькими попытками
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            # Сначала проверяем, есть ли активный webhook
            try:
                webhook_info = await asyncio.wait_for(
                    application.bot.get_webhook_info(),
                    timeout=5.0
                )
            except (asyncio.TimeoutError, Exception) as e:
                logging.warning(f"⚠️ Таймаут/ошибка при проверке webhook (попытка {attempt + 1}/{max_attempts}): {e}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2)
                    continue
                else:
                    logging.warning("⚠️ Пропускаем очистку webhook из-за таймаутов, продолжаем запуск")
                    break
            
            if webhook_info.url:
                logging.warning(f"⚠️ Обнаружен активный webhook: {webhook_info.url}")
                logging.warning("⚠️ Это может вызывать конфликты с polling!")
            
            # Удаляем webhook с очисткой pending updates
            try:
                await asyncio.wait_for(
                    application.bot.delete_webhook(drop_pending_updates=True),
                    timeout=5.0
                )
                logging.info("✅ Webhook очищен, pending updates удалены")
            except asyncio.TimeoutError:
                logging.warning("⚠️ Таймаут при удалении webhook, но продолжаем запуск")
                break
            except Exception as e:
                logging.warning(f"⚠️ Ошибка при удалении webhook: {e}, но продолжаем запуск")
                break
            
            # Небольшая задержка для обработки на стороне Telegram
            await asyncio.sleep(1)
            
            # Проверяем еще раз после удаления (не критично)
            try:
                webhook_info = await asyncio.wait_for(
                    application.bot.get_webhook_info(),
                    timeout=3.0
                )
                if not webhook_info.url:
                    logging.info("✅ Webhook успешно удален")
                    break
            except:
                # Не критично, продолжаем
                break
        except Exception as e:
            if attempt < max_attempts - 1:
                logging.warning(f"⚠️ Ошибка при очистке webhook (попытка {attempt + 1}/{max_attempts}): {e}")
                await asyncio.sleep(2)
            else:
                logging.warning(f"⚠️ Не удалось полностью очистить webhook после {max_attempts} попыток, но продолжаем запуск: {e}")
                # Не прерываем запуск - продолжаем работу
    
    # Дополнительная задержка после очистки webhook, чтобы Telegram успел закрыть все соединения
    # Это критично для предотвращения ошибок 409 Conflict
    logging.info("⏳ Ожидание завершения всех соединений на стороне Telegram...")
    await asyncio.sleep(3)
    logging.info("✅ Готово к запуску polling")

def main():
    """Главная функция запуска бота."""
    try:
        logging.info("=" * 60)
        logging.info("🚀 Запуск бота...")
        logging.info("=" * 60)
        
        # Проверка токена уже выполнена при загрузке модуля
        if not API_TOKEN:
            raise SystemExit("Токен не установлен. Проверьте переменную окружения TELEGRAM_BOT_TOKEN.")
        
        logging.info("✅ Токен проверен")

        # Проверяем, не запущен ли уже другой экземпляр бота
        running_processes = check_running_bot_processes()
        if running_processes:
            logging.warning("=" * 60)
            logging.warning("⚠️  ВНИМАНИЕ: Обнаружены запущенные процессы бота!")
            logging.warning(f"⚠️  PID процессов: {running_processes}")
            logging.warning("⚠️  Это может вызывать ошибки 409 Conflict.")
            logging.warning("⚠️  Рекомендуется завершить старые процессы:")
            for pid in running_processes:
                logging.warning(f"⚠️    kill {pid}")
            logging.warning("=" * 60)
            logging.warning("Продолжаем запуск через 3 секунды...")
            time.sleep(3)

        # Оптимизированные таймауты для HTTP запросов к Telegram API
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(
            connection_pool_size=16,  # Увеличиваем пул соединений для лучшей производительности
            read_timeout=30.0,  # Увеличиваем для стабильности при медленном интернете
            write_timeout=30.0,  # Увеличиваем для стабильности
            connect_timeout=15.0,  # Увеличиваем таймаут подключения для сетевых проблем
            pool_timeout=10.0,  # Таймаут ожидания свободного соединения из пула
        )
        
        application = Application.builder().token(API_TOKEN).request(request).post_init(post_init).build()
        
        # Команды (должны быть ПЕРВЫМИ, чтобы не перехватывались другими обработчиками)
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("premium", premium_command))
        application.add_handler(CommandHandler("stats", stats_command))
        # application.add_handler(CommandHandler("test_premium", test_premium_command))  # Команда для тестирования премиума (закомментирована)
        
        # Диалоги/обработчики (должны быть ПЕРЕД общими обработчиками кнопок)
        # ВАЖНО: Профиль регистрируем ПЕРЕД расчетом дозы, чтобы его ConversationHandler обрабатывал сообщения первым
        for h in build_profile_handlers():
            application.add_handler(h)
        
        # Расчет дозы (после профиля, чтобы не перехватывать сообщения)
        application.add_handler(build_calculate_conversation())
        
        # Обработчики inline кнопок из /start (после ConversationHandler)
        # Исключаем start_calculate и start_create_profile, так как они обрабатываются ConversationHandler
        application.add_handler(CallbackQueryHandler(handle_start_button, pattern="^start_(?!calculate|create_profile)"))
        
        # Обработчики кнопок профиля (исключаем profile_edit_, так как он обрабатывается ConversationHandler)
        application.add_handler(CallbackQueryHandler(handle_profile_buttons, pattern="^profile_(show|delete_)"))
        
        # Обработчик сохранения дозы в дневник
        application.add_handler(CallbackQueryHandler(handle_dose_save, pattern="^dose_save$"))
        
        # Обработчик просмотра дневника
        application.add_handler(CallbackQueryHandler(handle_dose_diary, pattern="^dose_diary$"))
        
        # Обработчики кнопок покупки премиум
        application.add_handler(CallbackQueryHandler(handle_premium_buttons, pattern="^premium_"))
        
        # Обработчики платежей
        async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Обработчик запроса на проверку платежа перед оплатой."""
            query = update.pre_checkout_query
            if query:
                # Всегда подтверждаем платеж (в реальном приложении здесь можно добавить дополнительную проверку)
                await query.answer(ok=True)
                logging.info(f"✅ Pre-checkout query approved for user {query.from_user.id}, payload: {query.invoice_payload}")
        
        async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Обработчик успешного платежа."""
            logging.info(f"💰 Получен успешный платеж от user {update.effective_user.id if update.effective_user else 'unknown'}")
            
            if not update.message or not update.message.successful_payment:
                logging.warning("⚠️ successful_payment_callback вызван, но нет update.message или successful_payment")
                return
            
            payment = update.message.successful_payment
            user_id = update.message.from_user.id
            
            logging.info(f"💰 Обработка платежа для user_id={user_id}, payload={payment.invoice_payload}, charge_id={payment.provider_payment_charge_id}")
            
            try:
                # Завершаем платеж и активируем премиум
                result = await complete_payment(
                    invoice_payload=payment.invoice_payload,
                    provider_payment_charge_id=payment.provider_payment_charge_id
                )
                
                if result:
                    premium_until = result["premium_until"]
                    subscription_days = result["subscription_days"]
                    
                    # Форматируем дату окончания для отображения
                    moscow_tz = timezone(timedelta(hours=3))
                    until_local = premium_until.astimezone(moscow_tz)
                    until_str = until_local.strftime("%d.%m.%Y")
                    
                    success_text = (
                        f"✅ **Платеж успешно обработан!**\n\n"
                        f"✨ Ваша премиум-подписка активирована на {subscription_days} дней!\n\n"
                        f"📅 Подписка действует до: {until_str}\n\n"
                        f"Теперь вам доступны все премиум-функции:\n"
                        f"• 👶 Профиль ребенка\n"
                        f"• 📊 Дневник лекарств\n"
                        f"• 🚩 Красные флаги\n\n"
                        f"Спасибо за поддержку! 💚"
                    )
                    
                    # Добавляем кнопку "На главную"
                    home_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
                    ])
                    
                    await update.message.reply_text(success_text, parse_mode="Markdown", reply_markup=home_keyboard)
                    logging.info(f"✅ Премиум активирован для пользователя {user_id} до {until_str}")
                    return  # Успешно обработано, выходим из функции
                else:
                    # Платеж не найден - это критическая ошибка
                    error_msg = (
                        f"❌ Ошибка при обработке платежа.\n\n"
                        f"Платеж получен, но не найден в базе данных.\n\n"
                        f"**Не волнуйтесь!** Ваши деньги в безопасности.\n\n"
                        f"Пожалуйста, свяжитесь с поддержкой и укажите:\n"
                        f"• Ваш user_id: {user_id}\n"
                        f"• Payload: {payment.invoice_payload}\n"
                        f"• Payment ID: {payment.provider_payment_charge_id}\n\n"
                        f"Мы активируем премиум вручную."
                    )
                    await update.message.reply_text(error_msg, parse_mode="Markdown")
                    logging.error(
                        f"❌ КРИТИЧЕСКАЯ ОШИБКА: Платеж не найден в БД!\n"
                        f"User ID: {user_id}\n"
                        f"Payload: {payment.invoice_payload}\n"
                        f"Provider Payment ID: {payment.provider_payment_charge_id}\n"
                        f"Total Amount: {payment.total_amount}\n"
                        f"Currency: {payment.currency}"
                    )
                    return  # Ошибка обработана, выходим из функции
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                logging.error(
                    f"❌ КРИТИЧЕСКАЯ ОШИБКА при обработке успешного платежа: {e}\n"
                    f"User ID: {user_id if 'user_id' in locals() else 'unknown'}\n"
                    f"Полный traceback:\n{error_details}"
                    f"Payload: {payment.invoice_payload if 'payment' in locals() else 'unknown'}",
                    exc_info=True
                )
                # Формируем сообщение об ошибке без Markdown, чтобы избежать ошибок парсинга
                error_msg = (
                    f"❌ Произошла ошибка при активации премиума.\n\n"
                    f"Не волнуйтесь! Ваши деньги в безопасности.\n\n"
                    f"Пожалуйста, свяжитесь с поддержкой и укажите:\n"
                    f"• Ваш user_id: {user_id if 'user_id' in locals() else 'неизвестен'}\n"
                    f"• Payload: {payment.invoice_payload if 'payment' in locals() else 'неизвестен'}\n\n"
                    f"Мы активируем премиум вручную."
                )
                try:
                    await update.message.reply_text(error_msg)
                except Exception as send_error:
                    logging.error(f"❌ Не удалось отправить сообщение об ошибке пользователю: {send_error}")
        
        application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
        application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
        
        # Команда для ручной активации премиума администратором
        async def activate_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Ручная активация премиума администратором (формат: /activate_premium user_id days)."""
            if not update.message:
                return
            
            user_id = update.effective_user.id
            
            # Проверяем, является ли пользователь администратором
            if not ADMIN_USER_ID or user_id != ADMIN_USER_ID:
                await update.message.reply_text("❌ У вас нет доступа к этой команде.")
                return
            
            try:
                # Парсим аргументы: /activate_premium user_id days
                args = context.args
                if len(args) < 2:
                    await update.message.reply_text(
                        "Использование: /activate_premium <user_id> <days>\n\n"
                        "Пример: /activate_premium 123456789 30"
                    )
                    return
                
                target_user_id = int(args[0])
                days = int(args[1])
                
                # Активируем премиум
                now = datetime.now(timezone.utc)
                premium_until = now + timedelta(days=days)
                await set_user_premium(target_user_id, True, premium_until)
                
                # Проверяем, что премиум действительно активирован
                is_premium = await is_user_premium(target_user_id)
                if not is_premium:
                    logging.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Премиум не активирован для user_id={target_user_id} после вызова set_user_premium!")
                    await update.message.reply_text(
                        f"⚠️ Ошибка: Премиум не активирован в БД.\n\n"
                        f"Проверьте логи для деталей.\n"
                        f"User ID: {target_user_id}"
                    )
                    return
                
                moscow_tz = timezone(timedelta(hours=3))
                until_local = premium_until.astimezone(moscow_tz)
                until_str = until_local.strftime("%d.%m.%Y")
                
                await update.message.reply_text(
                    f"✅ Премиум активирован!\n\n"
                    f"User ID: {target_user_id}\n"
                    f"Дней: {days}\n"
                    f"Действует до: {until_str}\n\n"
                    f"✅ Проверка: Премиум статус подтвержден в БД"
                )
                
                # Отправляем уведомление пользователю
                try:
                    # Создаем кнопки для быстрого доступа
                    premium_keyboard = [
                        [InlineKeyboardButton("🏠 На главную (/start)", callback_data="start_home")],
                        [InlineKeyboardButton("👶 Профиль", callback_data="start_profile")]
                    ]
                    premium_markup = InlineKeyboardMarkup(premium_keyboard)
                    
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=(
                            f"✅ **Ваш премиум активирован администратором!**\n\n"
                            f"✨ Премиум-подписка активна на {days} дней!\n\n"
                            f"📅 Подписка действует до: {until_str}\n\n"
                            f"Теперь вам доступны все премиум-функции:\n"
                            f"• 👶 Профиль ребенка\n"
                            f"• 📊 Дневник лекарств\n"
                            f"• 🚩 Красные флаги\n\n"
                            f"💡 Используйте /start чтобы увидеть все премиум-функции!\n\n"
                            f"Спасибо! 💚"
                        ),
                        parse_mode="Markdown",
                        reply_markup=premium_markup
                    )
                except Exception as notify_error:
                    logging.warning(f"Не удалось отправить уведомление пользователю {target_user_id}: {notify_error}")
                
                logging.info(f"Admin {user_id} manually activated premium for user {target_user_id} for {days} days - VERIFIED")
                
                logging.info(f"Admin {user_id} manually activated premium for user {target_user_id} for {days} days")
                
            except ValueError:
                await update.message.reply_text("❌ Ошибка: user_id и days должны быть числами.")
            except Exception as e:
                logging.error(f"Error in activate_premium_command: {e}", exc_info=True)
                await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        
        application.add_handler(CommandHandler("activate_premium", activate_premium_command))
        
        # Команда для проверки статуса премиума
        async def check_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Проверить статус премиум-подписки."""
            if not update.message:
                return
            
            user_id = update.effective_user.id
            
            try:
                is_premium = await is_premium_user(user_id)
                
                if is_premium:
                    # Получаем информацию о подписке
                    import aiosqlite
                    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                        db.row_factory = aiosqlite.Row
                        async with db.execute(
                            "SELECT premium_until FROM user_premium WHERE user_id = ?",
                            (user_id,)
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row and row["premium_until"]:
                                premium_until = datetime.fromisoformat(row["premium_until"])
                                moscow_tz = timezone(timedelta(hours=3))
                                until_local = premium_until.astimezone(moscow_tz)
                                until_str = until_local.strftime("%d.%m.%Y %H:%M")
                                
                                await update.message.reply_text(
                                    f"✅ **У вас активна премиум-подписка!**\n\n"
                                    f"📅 Подписка действует до: {until_str}\n\n"
                                    f"Вам доступны все премиум-функции:\n"
                                    f"• 👶 Профиль ребенка\n"
                                    f"• 📊 Дневник лекарств\n"
                                    f"• 🚩 Красные флаги\n\n"
                                    f"Используйте /start чтобы увидеть все функции!",
                                    parse_mode="Markdown"
                                )
                            else:
                                await update.message.reply_text(
                                    "✅ У вас активна премиум-подписка!\n\n"
                                    "Используйте /start чтобы увидеть все функции."
                                )
                else:
                    await update.message.reply_text(
                        "❌ У вас нет активной премиум-подписки.\n\n"
                        "Используйте /premium чтобы узнать больше о премиум-доступе."
                    )
            except Exception as e:
                logging.error(f"Error in check_premium_command: {e}", exc_info=True)
                await update.message.reply_text(
                    "❌ Произошла ошибка при проверке статуса. Попробуйте позже."
                )
        
        application.add_handler(CommandHandler("check_premium", check_premium_command))
        
        application.add_handler(build_feedback_conversation())

        # Красные флаги (ОРВИ + ЖКТ)
        for h in build_redflags_handlers():
            application.add_handler(h)

        # Добавляем обработчик ошибок
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Обработчик ошибок."""
            from telegram.error import Conflict
            
            # Ошибка Conflict (409) обычно происходит когда:
            # 1. Другой экземпляр бота уже получает обновления
            # 2. При переключении между webhook/polling
            # 3. Telegram еще не закрыл старое соединение
            # Библиотека автоматически обрабатывает это, но нужно время для синхронизации.
            if isinstance(context.error, Conflict):
                error_msg = str(context.error)
                # Логируем на INFO, чтобы видеть проблему, но не паникуем
                logging.info(f"⚠️ Conflict error (409): {error_msg}")
                logging.info("⚠️ Это может быть из-за другого запущенного процесса бота или незакрытого соединения.")
                logging.info("⚠️ Библиотека автоматически обработает это через несколько секунд.")
                logging.info("⚠️ Если ошибка повторяется, попробуйте:")
                logging.info("   1. Подождать 10-15 секунд и перезапустить бота")
                logging.info("   2. Проверить, нет ли других запущенных процессов: ps aux | grep app.main")
                return  # Не отправляем сообщение пользователю для этой ошибки
            
            # Обрабатываем сетевые ошибки и таймауты
            from telegram.error import TimedOut, NetworkError
            if isinstance(context.error, (TimedOut, NetworkError)):
                error_type = type(context.error).__name__
                logging.warning(f"⚠️ Сетевая ошибка/таймаут ({error_type}): {context.error}")
                # Не отправляем сообщение пользователю - библиотека автоматически повторит запрос
                return
            
            # Логируем информацию об update для отладки
            update_info = "None"
            if isinstance(update, Update):
                if update.message:
                    update_info = f"Message from {update.message.from_user.id if update.message.from_user else 'unknown'}"
                elif update.callback_query:
                    update_info = f"CallbackQuery from {update.callback_query.from_user.id if update.callback_query.from_user else 'unknown'}"
            
            logging.error(f"Exception while handling an update ({update_info}): {context.error}", exc_info=context.error)
            
            # Пытаемся отправить сообщение об ошибке пользователю, если это возможно
            if isinstance(update, Update) and update.effective_message:
                try:
                    await update.effective_message.reply_text(
                        "Произошла ошибка. Пожалуйста, попробуйте еще раз или используйте /start"
                    )
                except:
                    pass
        
        application.add_error_handler(error_handler)

        # Добавляем логирование перед запуском polling
        async def log_update_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Логируем получение обновлений для отладки."""
            if update.message:
                logging.info(f"📨 Update received: message from {update.message.from_user.id if update.message.from_user else 'unknown'}: {update.message.text}")
            elif update.callback_query:
                logging.info(f"🔘 Update received: callback_query from {update.callback_query.from_user.id if update.callback_query.from_user else 'unknown'}: {update.callback_query.data}")
            # Не обрабатываем, просто логируем - другие обработчики обработают
        
        # Добавляем в конец, чтобы не мешать командам
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_update_received))
        application.add_handler(CallbackQueryHandler(log_update_received, pattern=".*"))

        print("Бот запущен... (polling)")
        logging.info("Bot is ready to receive updates")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logging.error("=" * 60)
        logging.error("❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ БОТА:")
        logging.error(f"Тип ошибки: {type(e).__name__}")
        logging.error(f"Сообщение: {str(e)}")
        logging.error(f"Полный traceback:\n{error_details}")
        logging.error("=" * 60)
        raise  # Пробрасываем ошибку, чтобы Docker увидел проблему

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        import traceback
        logging.error(f"❌ Фатальная ошибка: {e}")
        logging.error(traceback.format_exc())
        sys.exit(1)