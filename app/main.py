# app/main.py
import logging
import os
import sys
import subprocess
import time
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.handlers.dose import build_calculate_conversation
from app.handlers.feedback import build_feedback_conversation
from app.handlers.redflags import build_redflags_handlers
from app.handlers.profile import build_profile_handlers
from app.storage import init_db, get_child_profile, set_user_premium, is_user_premium
from app.utils import is_premium_user

# Загружаем переменные окружения из .env файла
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start с новым приветственным сценарием."""
    if not update.message:
        logging.warning("Received /start command but update.message is None")
        return
    
    logging.info(f"Received /start command from user {update.effective_user.id}")
    try:
        user = update.effective_user
        # Используем имя профиля (first_name), если нет - username, если нет - "друг"
        user_name = user.first_name or user.username or "друг"
        
        # Проверяем, есть ли уже профиль
        profile = await get_child_profile(user.id)
        has_profile = profile is not None
        if has_profile:
            logging.info(f"User {user.id} has profile: name={profile.child_name}, weight={profile.child_weight_kg}, age={profile.child_age_months}")
        else:
            logging.info(f"User {user.id} has no profile")
        
        # Проверяем, первый ли это визит (нет профиля и нет записей в дневнике)
        from app.storage import has_dose_events
        has_events = await has_dose_events(user.id)
        
        # Проверяем премиум-статус (подписка на бота, не Telegram Premium)
        is_premium = await is_premium_user(user.id)
        
        # Если нет профиля и нет записей - это первый визит
        # Но если премиум-статус True, возможно остались старые данные - сбрасываем
        is_first_visit = not has_profile and not has_events
        if is_first_visit and is_premium:
            # Если это первый визит, но премиум-статус True - это странно, логируем и сбрасываем
            logging.warning(f"⚠️ User {user.id} - First visit but premium status is True! This might be stale data.")
            logging.warning(f"⚠️ Resetting premium status for new user.")
            from app.storage import set_user_premium
            await set_user_premium(user.id, False)
            is_premium = False
        
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
            
            # Кнопка дневника (только если есть записи)
            from app.storage import has_dose_events
            if await has_dose_events(user.id):
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
        
        # Для первого визита используем Markdown для форматирования
        if is_first_visit:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Error in start command: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "Произошла ошибка при обработке команды. Пожалуйста, попробуйте еще раз."
            )
        except:
            pass

async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline кнопок из команды /start."""
    query = update.callback_query
    await query.answer()  # Убираем "часики" у кнопки
    
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
        
        await query.message.reply_text(premium_text, reply_markup=premium_markup)
    
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
        await query.answer()
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
                return await self._original.reply_text(*args, **kwargs)
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        home_message = HomeMessage(query.message, query.from_user)
        home_update = Update(update_id=update.update_id + 40000, message=home_message)
        await start(home_update, context)
    
    elif query.data == "start_calculate":
        # Для кнопки "Рассчитать дозу" - просто отвечаем, обработка в ConversationHandler
        await query.answer()
    
    elif query.data == "start_profile":
        # Меню профиля для премиум-пользователей
        await query.answer()
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
        await query.answer()
        from app.handlers.redflags import REDFLAGS_ORVI_TEXT
        redflags_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ])
        await query.message.reply_text(REDFLAGS_ORVI_TEXT, reply_markup=redflags_keyboard)
    
    elif query.data == "start_redflags_gi":
        # Красные флаги ЖКТ
        await query.answer()
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
    await query.answer()
    
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
    await query.answer()
    
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
    """Обработчик кнопок покупки премиум (пока заглушки)."""
    query = update.callback_query
    await query.answer()  # Убираем "часики" у кнопки
    
    if query.data == "premium_buy_1month":
        await query.message.reply_text(
            "🌟 Покупка премиум-подписки на 1 месяц (99₽)\n\n"
            "💳 Система оплаты находится в разработке.\n\n"
            "Скоро вы сможете оформить подписку прямо здесь! "
            "А пока все основные функции бота остаются бесплатными 💚"
        )
    elif query.data == "premium_buy_3months":
        await query.message.reply_text(
            "🌟 Покупка премиум-подписки на 3 месяца (270₽)\n\n"
            "💳 Система оплаты находится в разработке.\n\n"
            "Скоро вы сможете оформить подписку прямо здесь! "
            "А пока все основные функции бота остаются бесплатными 💚"
        )
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

async def post_init(application: Application) -> None:
    """Инициализация БД при старте приложения."""
    await init_db()
    logging.info("База данных инициализирована")
    
    # Явно очищаем webhook перед запуском polling с несколькими попытками
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            # Сначала проверяем, есть ли активный webhook
            webhook_info = await application.bot.get_webhook_info()
            if webhook_info.url:
                logging.warning(f"⚠️ Обнаружен активный webhook: {webhook_info.url}")
                logging.warning("⚠️ Это может вызывать конфликты с polling!")
            
            # Удаляем webhook с очисткой pending updates
            await application.bot.delete_webhook(drop_pending_updates=True)
            logging.info("✅ Webhook очищен, pending updates удалены")
            
            # Увеличенная задержка для обработки на стороне Telegram
            # Это важно, чтобы Telegram успел закрыть старое соединение
            await asyncio.sleep(2)
            
            # Проверяем еще раз после удаления
            webhook_info = await application.bot.get_webhook_info()
            if not webhook_info.url:
                logging.info("✅ Webhook успешно удален")
                break
            else:
                if attempt < max_attempts - 1:
                    logging.warning(f"⚠️ Webhook все еще активен, попытка {attempt + 1}/{max_attempts}...")
                    await asyncio.sleep(1)
                else:
                    logging.error(f"❌ Не удалось удалить webhook после {max_attempts} попыток! URL: {webhook_info.url}")
        except Exception as e:
            if attempt < max_attempts - 1:
                logging.warning(f"⚠️ Ошибка при очистке webhook (попытка {attempt + 1}/{max_attempts}): {e}")
                await asyncio.sleep(1)
            else:
                logging.error(f"❌ Ошибка при очистке webhook после {max_attempts} попыток: {e}", exc_info=True)
    
    # Дополнительная задержка после очистки webhook, чтобы Telegram успел закрыть все соединения
    # Это критично для предотвращения ошибок 409 Conflict
    logging.info("⏳ Ожидание завершения всех соединений на стороне Telegram...")
    await asyncio.sleep(3)
    logging.info("✅ Готово к запуску polling")

def main():
    # Проверка токена уже выполнена при загрузке модуля
    if not API_TOKEN:
        raise SystemExit("Токен не установлен. Проверьте переменную окружения TELEGRAM_BOT_TOKEN.")

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

    application = Application.builder().token(API_TOKEN).post_init(post_init).build()
    
    # Команды (должны быть ПЕРВЫМИ, чтобы не перехватывались другими обработчиками)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("premium", premium_command))
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
    
    # Обработчики кнопок покупки премиум (пока заглушки)
    application.add_handler(CallbackQueryHandler(handle_premium_buttons, pattern="^premium_"))
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

if __name__ == "__main__":
    main()