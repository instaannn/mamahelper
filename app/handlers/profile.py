# app/handlers/profile.py
import re
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from app.storage import get_child_profile, get_all_child_profiles, save_child_profile, delete_child_profile

# Состояния диалога
ASK_NAME, ASK_AGE, ASK_WEIGHT = range(3)

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать профили детей."""
    user_id = update.effective_user.id
    all_profiles = await get_all_child_profiles(user_id)
    
    if not all_profiles:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("👶 Создать профиль", callback_data="start_create_profile")]])
        await update.message.reply_text(
            "👶 Профиль ребенка пока не сохранен.\n\n"
            "Создайте профиль, чтобы не вводить данные каждый раз:",
            reply_markup=kb
        )
        return
    
    # Если профиль один - показываем его детально
    if len(all_profiles) == 1:
        profile = all_profiles[0]
        lines = ["👶 Профиль ребенка:\n"]
        
        if profile.child_name:
            lines.append(f"• Имя: {profile.child_name}")
        if profile.child_age_months is not None:
            years = profile.child_age_months // 12
            months = profile.child_age_months % 12
            if years > 0:
                age_text = f"{years} г. {months} мес." if months > 0 else f"{years} г."
            else:
                age_text = f"{months} мес."
            lines.append(f"• Возраст: {age_text}")
        if profile.child_weight_kg is not None:
            lines.append(f"• Вес: {profile.child_weight_kg} кг")
        
        lines.append(f"\nОбновлено: {profile.updated_at.strftime('%d.%m.%Y %H:%M')}")
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить профиль", callback_data=f"profile_edit_{profile.profile_id}")],
            [InlineKeyboardButton("🗑 Удалить профиль", callback_data=f"profile_delete_{profile.profile_id}")],
            [InlineKeyboardButton("➕ Добавить еще одного ребенка", callback_data="start_create_profile")],
            [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
        ])
        
        await update.message.reply_text("\n".join(lines), reply_markup=kb)
    else:
        # Если профилей несколько - показываем список
        lines = ["👶 Ваши профили детей:\n\n"]
        buttons = []
        
        for profile in all_profiles:
            name = profile.child_name or "Без имени"
            age_info = ""
            if profile.child_age_months is not None:
                years = profile.child_age_months // 12
                months = profile.child_age_months % 12
                if years > 0:
                    age_info = f", {years} г. {months} мес." if months > 0 else f", {years} г."
                else:
                    age_info = f", {months} мес."
            weight_info = f", {profile.child_weight_kg} кг" if profile.child_weight_kg else ""
            
            lines.append(f"• {name}{age_info}{weight_info}")
            buttons.append([
                InlineKeyboardButton(f"✏️ {name}", callback_data=f"profile_edit_{profile.profile_id}"),
                InlineKeyboardButton(f"🗑", callback_data=f"profile_delete_{profile.profile_id}")
            ])
        
        buttons.append([InlineKeyboardButton("➕ Добавить еще одного ребенка", callback_data="start_create_profile")])
        buttons.append([InlineKeyboardButton("🏠 На главную", callback_data="start_home")])
        
        kb = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("\n".join(lines), reply_markup=kb)

async def start_set_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать диалог установки профиля."""
    user_id = update.effective_user.id
    
    # Очищаем состояние других ConversationHandler'ов
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)
    if key in context.application.user_data:
        user_data = context.application.user_data[key]
        # Удаляем ключи, связанные с расчетом дозы
        for k in list(user_data.keys()):
            if k.startswith('_conversation_handler_') or k in ['form', 'drug', 'conc_mg_per_ml', 'conc_label', 'weight', 'safety_queue', 'current_check']:
                del user_data[k]
    
    # Проверяем, редактируем ли мы существующий профиль
    # Если profile_id установлен - редактируем, если нет - создаем новый
    profile_id = context.user_data.get("profile_id")
    
    if profile_id:
        # Редактируем существующий профиль
        existing = await get_child_profile(user_id, profile_id)
        if existing:
            # Предзаполняем данные из существующего профиля
            context.user_data["child_name"] = existing.child_name
            context.user_data["child_age_months"] = existing.child_age_months
            context.user_data["child_weight_kg"] = existing.child_weight_kg
            
            await update.message.reply_text(
                f"✏️ Изменение имени\n\n"
                f"Введите новое имя ребенка:\n"
                f"Сейчас: {existing.child_name or '(не указано)'}\n"
                f"Не обязательно — можно написать «пропустить»",
                reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
            )
            return ASK_NAME
    
    # Создаем новый профиль
    # Проверяем, есть ли уже профили
    all_profiles = await get_all_child_profiles(user_id)
    
    if all_profiles:
        # Есть профили - создаем еще один
        await update.message.reply_text(
            "👶 Давайте сохраним данные еще одного малыша для быстрых расчётов!\n\n"
            "Как зовут ребенка?\n"
            "Не обязательно — можно пропустить",
            reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
        )
    else:
        # Профилей нет - создаем первый
        await update.message.reply_text(
            "👶 Давайте сохраним данные малыша для быстрых расчётов!\n\n"
            "Как зовут ребенка?\n"
            "Не обязательно — можно пропустить",
            reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
        )
    return ASK_NAME

async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода имени."""
    text = (update.message.text or "").strip()
    
    if text.lower() in ("отмена", "cancel"):
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    
    # Сохраняем имя (или None если пропущено)
    # Если редактируем и нажали "Пропустить", сохраняем None (удаляем значение)
    if text.lower() in ("пропустить", "-", ""):
        context.user_data["child_name"] = None
    else:
        context.user_data["child_name"] = text
    
    # Показываем текущее значение возраста, если редактируем
    profile_id = context.user_data.get("profile_id")
    age_hint = ""
    if profile_id:
        from app.storage import get_child_profile
        user_id = update.effective_user.id
        existing = await get_child_profile(user_id, profile_id)
        if existing and existing.child_age_months is not None:
            age_hint = f"\nТекущее значение: {existing.child_age_months} мес."
    
    await update.message.reply_text(
        f"Сколько лет ребенку?{age_hint}\n\n"
        "Можно ввести:\n"
        "• Возраст в годах с точкой (например: 3.5 или 2.0)\n"
        "• Возраст в месяцах целым числом (например: 5, 12, 18, 24)\n"
        "• Формат «лет и месяцев» (например: 3 года 6 месяцев или 2 г. 3 мес.)\n\n"
        "💡 Подсказка: целые числа до 24 считаются месяцами (5 = 5 месяцев, 12 = 12 месяцев).\n"
        "Для ввода в годах используйте десятичную дробь (2.0 = 2 года, 3.5 = 3.5 года).\n\n"
        "Или нажмите «Пропустить» чтобы не указывать возраст.",
        reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_AGE

async def got_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода возраста."""
    text = (update.message.text or "").strip().lower()
    
    if text == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    
    # Если редактируем и нажали "Пропустить", сохраняем None (удаляем значение)
    if text in ("пропустить", "-", ""):
        context.user_data["child_age_months"] = None
    else:
        age_months = None
        
        # Пытаемся распарсить разные форматы
        # 1. Формат "X лет Y месяцев" или "X г. Y мес." или "X года Y месяца"
        # Паттерн для "лет/г/года" и "месяцев/мес/месяца"
        pattern = r'(\d+(?:[.,]\d+)?)\s*(?:лет|г|года|год)\s*(?:и|,)?\s*(\d+)?\s*(?:месяцев|мес|месяца|месяц)?'
        match = re.search(pattern, text)
        if match:
            years = float(match.group(1).replace(',', '.'))
            months = int(match.group(2)) if match.group(2) else 0
            age_months = int(years * 12 + months)
        else:
            # 2. Просто число - может быть годами или месяцами
            try:
                # Пробуем как десятичное число (годы, если есть точка/запятая)
                if '.' in text or ',' in text:
                    age_float = float(text.replace(',', '.'))
                    if age_float < 0 or age_float > 20:
                        await update.message.reply_text(
                            "Пожалуйста, введите корректный возраст (0-20 лет).",
                            reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
                        )
                        return ASK_AGE
                    # Десятичное число - это годы
                    age_months = int(age_float * 12)
                else:
                    # Целое число - определяем по значению
                    age_int = int(text)
                    if age_int < 0 or age_int > 240:
                        await update.message.reply_text(
                            "Пожалуйста, введите корректный возраст (0-240 месяцев или 0-20 лет).",
                            reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
                        )
                        return ASK_AGE
                    # Логика определения целых чисел:
                    # - Если число < 24 - считаем месяцами (дети до 2 лет обычно считаются в месяцах: 5, 12, 18 месяцев)
                    # - Если число >= 24 - считаем месяцами (24, 30, 42 месяца и т.д.)
                    # Для ввода в годах используйте десятичную дробь: 1.0, 2.0, 3.5 и т.д.
                    if age_int < 24:
                        # Дети до 2 лет - месяцы (5 = 5 месяцев, 12 = 12 месяцев, 18 = 18 месяцев)
                        age_months = age_int
                    else:
                        # 24 и больше - месяцы
                        age_months = age_int
            except ValueError:
                await update.message.reply_text(
                    "Не удалось распознать возраст. Пожалуйста, введите:\n"
                    "• Возраст в годах (например: 3.5 или 2.3)\n"
                    "• Возраст в месяцах (например: 18 или 42)\n"
                    "• Формат «лет и месяцев» (например: 3 года 6 месяцев)\n\n"
                    "Или нажмите «Пропустить».",
                    reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_AGE
        
        if age_months is not None:
            if age_months < 0 or age_months > 240:
                await update.message.reply_text(
                    "Пожалуйста, введите корректный возраст (0-240 месяцев или 0-20 лет).",
                    reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_AGE
            context.user_data["child_age_months"] = age_months
    
    # Показываем текущее значение веса, если редактируем
    profile_id = context.user_data.get("profile_id")
    weight_hint = ""
    if profile_id:
        from app.storage import get_child_profile
        user_id = update.effective_user.id
        existing = await get_child_profile(user_id, profile_id)
        if existing and existing.child_weight_kg is not None:
            weight_hint = f"\nТекущее значение: {existing.child_weight_kg} кг"
    
    await update.message.reply_text(
        f"Какой вес ребенка в килограммах? (введите число, например: 11.5){weight_hint}\n\n"
        "Или нажмите «Пропустить» чтобы не указывать вес.",
        reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_WEIGHT

async def got_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода веса и сохранение профиля."""
    text = (update.message.text or "").strip().lower()
    
    if text == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    
    # Если редактируем и нажали "Пропустить", сохраняем None (удаляем значение)
    if text in ("пропустить", "-", ""):
        context.user_data["child_weight_kg"] = None
    else:
        try:
            weight = float(text.replace(",", "."))
            if weight <= 0 or weight > 100:  # разумные пределы
                await update.message.reply_text(
                    "Пожалуйста, введите корректный вес (0-100 кг).",
                    reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_WEIGHT
            context.user_data["child_weight_kg"] = weight
        except ValueError:
            await update.message.reply_text(
                "Пожалуйста, введите число (вес в кг) или нажмите «Пропустить».",
                reply_markup=ReplyKeyboardMarkup([["Пропустить", "Отмена"]], one_time_keyboard=True, resize_keyboard=True)
            )
            return ASK_WEIGHT
    
    # Сохраняем профиль
    user_id = update.effective_user.id
    profile_id = context.user_data.get("profile_id")  # Если есть - обновляем, если нет - создаем новый
    
    # Получаем значения из user_data (если редактируем и не указано новое - используем существующее)
    # Если ключ есть в user_data (даже если None) - используем его, иначе берем из существующего профиля
    if profile_id:
        existing = await get_child_profile(user_id, profile_id)
        if existing:
            # Если значение было установлено в user_data (включая None) - используем его
            # Иначе используем существующее значение
            child_name = context.user_data["child_name"] if "child_name" in context.user_data else existing.child_name
            child_age_months = context.user_data["child_age_months"] if "child_age_months" in context.user_data else existing.child_age_months
            child_weight_kg = context.user_data["child_weight_kg"] if "child_weight_kg" in context.user_data else existing.child_weight_kg
        else:
            child_name = context.user_data.get("child_name")
            child_age_months = context.user_data.get("child_age_months")
            child_weight_kg = context.user_data.get("child_weight_kg")
    else:
        child_name = context.user_data.get("child_name")
        child_age_months = context.user_data.get("child_age_months")
        child_weight_kg = context.user_data.get("child_weight_kg")
    
    profile = await save_child_profile(
        user_id=user_id,
        child_name=child_name,
        child_age_months=child_age_months,
        child_weight_kg=child_weight_kg,
        profile_id=profile_id,
    )
    
    # Формируем сообщение
    action_text = "✅ Профиль обновлен!" if profile_id else "✅ Данные сохранены! Теперь вам удобнее.\n"
    lines = [action_text]
    lines.append("Ваш профиль малыша:\n")
    
    if profile.child_name:
        lines.append(f"• Имя: {profile.child_name}")
    if profile.child_age_months is not None:
        years = profile.child_age_months // 12
        months = profile.child_age_months % 12
        if years > 0:
            age_text = f"{years} г. {months} мес." if months > 0 else f"{years} г."
        else:
            age_text = f"{months} мес."
        lines.append(f"• Возраст: {age_text}")
    if profile.child_weight_kg is not None:
        lines.append(f"• Вес: {profile.child_weight_kg} кг")
    
    lines.append("\nПри следующем расчете я предложу использовать эти данные — вам не придется вводить вес заново!")
    
    # Добавляем кнопки для расчета дозы и возврата на главную
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💊 Рассчитать дозу", callback_data="start_calculate")],
        [InlineKeyboardButton("🏠 На главную", callback_data="start_home")]
    ])
    
    await update.message.reply_text("\n".join(lines), reply_markup=kb)
    
    # Очищаем временные данные
    for key in ["child_name", "child_age_months", "child_weight_kg"]:
        context.user_data.pop(key, None)
    
    return ConversationHandler.END

async def delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить профиль ребенка (обработчик команды /profile_delete)."""
    user_id = update.effective_user.id
    all_profiles = await get_all_child_profiles(user_id)
    
    if not all_profiles:
        await update.message.reply_text(
            "Профиль не найден. Нечего удалять.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    
    if len(all_profiles) == 1:
        # Если профиль один - удаляем его
        deleted = await delete_child_profile(user_id, all_profiles[0].profile_id)
        if deleted:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("👶 Создать новый профиль", callback_data="start_create_profile")]])
            await update.message.reply_text(
                "✅ Профиль удален.\n\n"
                "Можете создать новый профиль:",
                reply_markup=kb
            )
    else:
        # Если профилей несколько - показываем список для выбора
        buttons = []
        for profile in all_profiles:
            name = profile.child_name or "Без имени"
            buttons.append([InlineKeyboardButton(f"🗑 Удалить {name}", callback_data=f"profile_delete_{profile.profile_id}")])
        
        kb = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Выберите профиль для удаления:",
            reply_markup=kb
        )

def build_profile_handlers():
    """Создать обработчики для работы с профилем."""
    
    # Обработчик для кнопки "Создать профиль" из /start
    async def handle_create_profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        # Очищаем состояние других ConversationHandler'ов, чтобы они не перехватывали сообщения
        # Ключи состояний для разных ConversationHandler'ов
        chat_id = query.message.chat_id
        user_id = query.from_user.id
        key = (chat_id, user_id)
        
        # Очищаем состояние расчета дозы, если оно есть
        if key in context.application.user_data:
            user_data = context.application.user_data[key]
            # Удаляем ключи, связанные с расчетом дозы
            for k in list(user_data.keys()):
                if k.startswith('_conversation_handler_') or k in ['form', 'drug', 'conc_mg_per_ml', 'conc_label', 'weight', 'safety_queue', 'current_check']:
                    del user_data[k]
        
        # ВАЖНО: Очищаем profile_id из user_data, чтобы создавался новый профиль, а не редактировался существующий
        if "profile_id" in context.user_data:
            del context.user_data["profile_id"]
        
        # Создаем fake update с сообщением для start_set_profile
        from datetime import datetime
        
        class FakeMessage:
            def __init__(self, original_msg, user):
                self.message_id = original_msg.message_id
                self.date = datetime.now()
                self.chat = original_msg.chat
                self.from_user = user
                self.text = "/profile_set"
                self.entities = None
                self._original = original_msg
            
            async def reply_text(self, *args, **kwargs):
                return await self._original.reply_text(*args, **kwargs)
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        fake_message = FakeMessage(query.message, query.from_user)
        fake_update = Update(update_id=update.update_id + 10000, message=fake_message)
        
        # Вызываем start_set_profile
        return await start_set_profile(fake_update, context)
    
    # Обработчик для кнопки "Редактировать профиль" (profile_edit_*)
    async def handle_edit_profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        # Извлекаем profile_id из callback_data
        try:
            profile_id = int(query.data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("❌ Ошибка: неверный ID профиля.")
            return ConversationHandler.END
        
        # Очищаем состояние других ConversationHandler'ов
        chat_id = query.message.chat_id
        user_id = query.from_user.id
        key = (chat_id, user_id)
        
        if key in context.application.user_data:
            user_data = context.application.user_data[key]
            for k in list(user_data.keys()):
                if k.startswith('_conversation_handler_') or k in ['form', 'drug', 'conc_mg_per_ml', 'conc_label', 'weight', 'safety_queue', 'current_check']:
                    del user_data[k]
        
        # Сохраняем profile_id в user_data для обновления
        context.user_data["profile_id"] = profile_id
        
        # Создаем fake update с сообщением для start_set_profile
        from datetime import datetime
        
        class FakeMessage:
            def __init__(self, original_msg, user):
                self.message_id = original_msg.message_id
                self.date = datetime.now()
                self.chat = original_msg.chat
                self.from_user = user
                self.text = "/profile_set"
                self.entities = None
                self._original = original_msg
            
            async def reply_text(self, *args, **kwargs):
                return await self._original.reply_text(*args, **kwargs)
            
            def __getattr__(self, name):
                return getattr(self._original, name)
        
        fake_message = FakeMessage(query.message, query.from_user)
        fake_update = Update(update_id=update.update_id + 20000, message=fake_message)
        
        # Вызываем start_set_profile
        return await start_set_profile(fake_update, context)
    
    return [
        CommandHandler("profile", show_profile),
        CommandHandler("profile_set", start_set_profile),
        CommandHandler("profile_delete", delete_profile),
        ConversationHandler(
            entry_points=[
                CommandHandler("profile_set", start_set_profile),
                CallbackQueryHandler(handle_create_profile_button, pattern="^start_create_profile$"),
                CallbackQueryHandler(handle_edit_profile_button, pattern="^profile_edit_")
            ],
            states={
                ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
                ASK_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_age)],
                ASK_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_weight)],
            },
            fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
            allow_reentry=True,
        ),
    ]

