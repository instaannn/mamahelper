# app/handlers/dose.py
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
)
from app.models import DoseRequest
from app.calculators.core import calc_dose
from app.i18n_ru import DISCLAIMER
from app.utils import load_formulary  # humanize_dt не нужен, т.к. подтверждение/таймеры убраны

# Состояния (без подтверждения)
ASK_FORM, ASK_DRUG, ASK_CONC_FIXED, ASK_WEIGHT, ASK_SAFETY = range(5)

# /calculate — старт
async def start_calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([["Сироп", "Свечи"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Какую форму даёте сейчас? Выберите кнопку ниже 👇", reply_markup=kb)
    return ASK_FORM

async def choose_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if "сироп" in text:
        context.user_data["form"] = "syrup"
        kb = ReplyKeyboardMarkup([["Парацетамол", "Ибупрофен"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Выберите препарат, с которого начнём 💊", reply_markup=kb)
        return ASK_DRUG
    elif "свеч" in text:
        context.user_data["form"] = "suppository"
        kb = ReplyKeyboardMarkup([["Цефекон"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Выберите препарат 🧸", reply_markup=kb)
        return ASK_DRUG
    else:
        await update.message.reply_text("Пожалуйста, выберите кнопку ниже: «Сироп» или «Свечи» 🙂")
        return ASK_FORM

async def choose_drug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    form = context.user_data.get("form")
    text = (update.message.text or "").lower()

    if form == "syrup":
        if "парацет" in text:
            context.user_data["drug"] = "paracetamol"
        elif "ибупроф" in text:
            context.user_data["drug"] = "ibuprofen"
        else:
            await update.message.reply_text("Пожалуйста, нажмите кнопку «Парацетамол» или «Ибупрофен» 🙂")
            return ASK_DRUG

        # показываем фиксированные концентрации для выбранного препарата
        f = load_formulary()
        drug_key = context.user_data["drug"]
        fixed = f["drugs"][drug_key]["routes"]["oral"].get("fixed_concentrations", [])
        labels = [[fc["label"]] for fc in fixed]
        kb = ReplyKeyboardMarkup(labels, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Какая концентрация указана на флаконе? Нажмите кнопку 👇", reply_markup=kb)
        return ASK_CONC_FIXED

    elif form == "suppository":
        if "цефекон" in text:
            context.user_data["drug"] = "paracetamol"
            context.user_data["form"] = "suppository"
            
            # Проверяем, есть ли сохраненные профили (только для премиум пользователей)
            from app.storage import get_all_child_profiles
            from app.utils import is_premium_user
            user_id = update.effective_user.id
            is_premium = await is_premium_user(user_id)
            
            if is_premium:
                all_profiles = await get_all_child_profiles(user_id)
                profiles_with_weight = [p for p in all_profiles if p.child_weight_kg is not None]
                
                if profiles_with_weight:
                    # Показываем все профили с весами
                    buttons = []
                    for profile in profiles_with_weight:
                        name = profile.child_name or "Без имени"
                        weight_text = f"{name} ({profile.child_weight_kg} кг)"
                        buttons.append([weight_text])
                    buttons.append(["Ввести другой вес"])
                    
                    kb = ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True)
                    
                    if len(profiles_with_weight) == 1:
                        text = f"У вас сохранен вес: {profiles_with_weight[0].child_weight_kg} кг\n\nВыберите профиль или введите вес вручную:"
                    else:
                        text = "Выберите профиль ребенка или введите вес вручную:"
                    
                    await update.message.reply_text(text, reply_markup=kb)
                    return ASK_WEIGHT
            
            # Для не премиум пользователей или если профиля нет - сразу спрашиваем вес
            await update.message.reply_text(
                "Сколько весит ребёнок? Напишите число, например: 11.2",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_WEIGHT
        else:
            await update.message.reply_text("Пожалуйста, нажмите кнопку «Цефекон» 🧸")
            return ASK_DRUG

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💊 Рассчитать дозу", callback_data="start_calculate")]])
    await update.message.reply_text("Не понял выбор. Давайте начнём заново:", reply_markup=kb)
    return ConversationHandler.END

async def set_fixed_conc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фиксируем выбранную концентрацию (для парацетамола/ибупрофена)."""
    chosen_label = (update.message.text or "").strip()
    f = load_formulary()
    drug_key = context.user_data.get("drug")
    fixed = f["drugs"][drug_key]["routes"]["oral"].get("fixed_concentrations", [])
    found = next((fc for fc in fixed if fc["label"] == chosen_label), None)
    if not found:
        labels = [[fc["label"]] for fc in fixed]
        kb = ReplyKeyboardMarkup(labels, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Пожалуйста, нажмите одну из кнопок с концентрацией 🙂", reply_markup=kb)
        return ASK_CONC_FIXED

    context.user_data["conc_mg_per_ml"] = float(found["mg_per_ml"])
    context.user_data["conc_label"] = found["label"]

    # Проверяем, есть ли сохраненные профили (только для премиум пользователей)
    from app.storage import get_all_child_profiles
    from app.utils import is_premium_user
    import logging
    user_id = update.effective_user.id
    is_premium = await is_premium_user(user_id)
    
    if is_premium:
        all_profiles = await get_all_child_profiles(user_id)
        profiles_with_weight = [p for p in all_profiles if p.child_weight_kg is not None]
        
        if profiles_with_weight:
            logging.info(f"Found {len(profiles_with_weight)} profiles with weight for user {user_id} in set_fixed_conc")
            
            # Показываем все профили с весами
            buttons = []
            for profile in profiles_with_weight:
                name = profile.child_name or "Без имени"
                weight_text = f"{name} ({profile.child_weight_kg} кг)"
                buttons.append([weight_text])
            buttons.append(["Ввести другой вес"])
            
            kb = ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True)
            
            if len(profiles_with_weight) == 1:
                text = f"У вас сохранен вес: {profiles_with_weight[0].child_weight_kg} кг\n\nВыберите профиль или введите вес вручную:"
            else:
                text = "Выберите профиль ребенка или введите вес вручную:"
            
            await update.message.reply_text(text, reply_markup=kb)
            return ASK_WEIGHT
        else:
            logging.info(f"No profiles with weight found for user {user_id} in set_fixed_conc")
    
    # Для не премиум пользователей или если профиля нет - спрашиваем вес
    await update.message.reply_text("Сколько весит ребёнок? Напишите число, например: 11.2", reply_markup=ReplyKeyboardRemove())
    return ASK_WEIGHT

async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод веса + ранние стопы по массе и подготовка чекеров безопасности (Да/Нет)."""
    text = (update.message.text or "").strip().replace(",", ".")
    
    # Проверяем, выбрал ли пользователь профиль или хочет ввести другой вес
    from app.storage import get_all_child_profiles
    from app.utils import is_premium_user
    user_id = update.effective_user.id
    is_premium = await is_premium_user(user_id)
    
    weight = None
    
    if is_premium:
        all_profiles = await get_all_child_profiles(user_id)
        profiles_with_weight = [p for p in all_profiles if p.child_weight_kg is not None]
        
        # Проверяем, выбрал ли пользователь профиль (формат: "Имя (вес кг)")
        if "ввести другой" in text.lower() or "другой" in text.lower():
            # Пользователь хочет ввести другой вес
            await update.message.reply_text("Сколько весит ребёнок? Напишите число, например: 11.2", reply_markup=ReplyKeyboardRemove())
            return ASK_WEIGHT
        elif profiles_with_weight:
            # Ищем профиль по тексту кнопки (формат: "Имя (вес кг)")
            for profile in profiles_with_weight:
                name = profile.child_name or "Без имени"
                weight_str = str(profile.child_weight_kg)
                # Проверяем, содержит ли текст имя ребенка и вес
                # Формат кнопки: "Имя (вес кг)"
                if name.lower() in text.lower() and weight_str in text:
                    weight = profile.child_weight_kg
                    # Сохраняем profile_id для дальнейшего использования
                    context.user_data["selected_profile_id"] = profile.profile_id
                    break
                # Также проверяем просто по весу, если имя не найдено (на случай, если имя изменилось)
                if weight_str in text and "кг" in text:
                    # Проверяем, что это точно наш вес, а не часть другого числа
                    weight_with_kg = f"{weight_str} кг"
                    if weight_with_kg in text:
                        weight = profile.child_weight_kg
                        context.user_data["selected_profile_id"] = profile.profile_id
                        break
    
    # Если профиль не выбран, пытаемся распарсить как число
    if weight is None:
        try:
            weight = float(text)
            context.user_data["weight"] = weight
        except Exception:
            await update.message.reply_text("Не получилось понять вес 😅 Введите просто число, например: 11.2")
            return ASK_WEIGHT
    else:
        context.user_data["weight"] = weight
    
    weight = context.user_data["weight"]

    # Обработка свечей (суппозиториев)
    if context.user_data.get("form") == "suppository":
        return await calculate_suppository_dose(update, context, weight)

    # Ранние стопы по массе для ибупрофена
    if context.user_data.get("drug") == "ibuprofen":
        # 40 мг/мл (200 мг/5мл) — противопоказание <10 кг
        if context.user_data.get("conc_mg_per_ml") == 40.0 and weight < 10:
            await update.message.reply_text(
                "Для ибупрофена 200 мг/5мл (40 мг/мл): масса тела ребёнка менее 10 кг — противопоказание. "
                "Пожалуйста, обсудите с педиатром ❤️‍🩹",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        # Любой ибупрофен — противопоказание <5 кг
        if weight < 5:
            await update.message.reply_text(
                "Для ибупрофена: масса тела до 5 кг — противопоказание без назначения врача. "
                "Пожалуйста, обратитесь к педиатру ❤️‍🩹",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END

    # ——— Чекеры безопасности (Да/Нет), возраста не спрашиваем цифрами ———
    queue = []
    # Вопрос о вакцинации убран по запросу пользователя

    # Проверяем профиль для премиум-пользователей, чтобы не спрашивать о возрасте, если он уже известен
    from app.storage import get_child_profile
    from app.utils import is_premium_user
    user_id = update.effective_user.id
    is_premium = await is_premium_user(user_id)
    profile = None
    if is_premium:
        profile = await get_child_profile(user_id)

    if context.user_data.get("drug") == "paracetamol":
        # Не спрашиваем о возрасте < 2 месяцев, если в профиле указан возраст >= 2 месяцев
        should_ask_under2m = True
        if profile and profile.child_age_months is not None:
            if profile.child_age_months >= 2:
                should_ask_under2m = False
        
        if should_ask_under2m:
            queue.append(("under2m", "Ребёнку меньше 2 месяцев?"))

    if context.user_data.get("drug") == "ibuprofen" and context.user_data.get("conc_mg_per_ml") == 40.0:
        # Не спрашиваем о возрасте < 12 месяцев, если в профиле указан возраст >= 12 месяцев
        should_ask_under12m = True
        if profile and profile.child_age_months is not None:
            if profile.child_age_months >= 12:
                should_ask_under12m = False
        
        if should_ask_under12m:
            queue.append(("under12m", "Ребёнку меньше 12 месяцев?"))

    context.user_data["safety_queue"] = queue

    if queue:
        kb = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
        context.user_data["current_check"] = queue.pop(0)
        await update.message.reply_text(context.user_data["current_check"][1], reply_markup=kb)
        return ASK_SAFETY

    # Если вопросов нет — сразу считаем дозу
    return await calculate_and_finish(update, context)

async def handle_safety_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем ответ Да/Нет на текущий чекер и переходим дальше или стопим."""
    answer = (update.message.text or "").strip().lower()
    key, _ = context.user_data.get("current_check", ("", ""))

    if answer not in ("да", "нет"):
        kb = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Пожалуйста, ответьте кнопкой «Да» или «Нет».", reply_markup=kb)
        return ASK_SAFETY

    # Обработка вопроса о вакцинации убрана по запросу пользователя
    
    if key == "menb":
        context.user_data["menb"] = (answer == "да")

    elif key == "under2m" and answer == "да":
        await update.message.reply_text(
            "Для парацетамола: возраст ребёнка младше 2 месяцев — противопоказание без назначения врача. "
            "Пожалуйста, обсудите это с педиатром ❤️‍🩹",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    elif key == "under12m" and answer == "да":
        await update.message.reply_text(
            "Для ибупрофена 200 мг/5мл (40 мг/мл): возраст до 12 месяцев — противопоказание. "
            "Нужна консультация педиатра ❤️‍🩹",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Переходим к следующему чекеру из очереди (если есть)
    queue = context.user_data.get("safety_queue", [])
    if queue:
        context.user_data["current_check"] = queue.pop(0)
        kb = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(context.user_data["current_check"][1], reply_markup=kb)
        return ASK_SAFETY

    # Очередь пустая — считаем дозу и завершаем
    return await calculate_and_finish(update, context)

async def calculate_suppository_dose(update: Update, context: ContextTypes.DEFAULT_TYPE, weight: float):
    """Расчет дозы для свечей (суппозиториев) Цефекон на основе веса."""
    # Определяем возрастную группу и дозировку по весу
    if 4 <= weight <= 6:
        # 1-3 месяца - специальный случай
        text = (
            "⚠️ У детей в возрасте до 3 месяцев данный препарат применяется однократно "
            "(1 суппозиторий) в случае развития лихорадки (повышения температуры тела) "
            "на фоне прививок, которые проводятся в возрасте 2 месяцев.\n\n"
            "Препарат применяется только по назначению врача!"
        )
        # Отправляем с обработкой таймаутов
        import asyncio
        try:
            await asyncio.wait_for(
                update.message.reply_text(text, reply_markup=ReplyKeyboardRemove()),
                timeout=10.0
            )
        except (asyncio.TimeoutError, Exception) as send_error:
            from telegram.error import TimedOut
            if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
                # Пробуем отправить упрощенное сообщение
                try:
                    await asyncio.wait_for(
                        update.message.reply_text(
                            "⚠️ Препарат применяется только по назначению врача!",
                            reply_markup=ReplyKeyboardRemove()
                        ),
                        timeout=5.0
                    )
                except Exception:
                    pass
            else:
                raise
        return ConversationHandler.END
    
    # Определяем дозировку по весу
    if 7 <= weight <= 10:
        # 3-12 месяцев
        dose_text = "1 суппозиторий по 100 мг"
        age_group = "3-12 месяцев"
        dose_mg = 100
        supp_count = 1
    elif 11 <= weight <= 16:
        # 1-3 года
        dose_text = "1-2 суппозитория по 100 мг"
        age_group = "1-3 года"
        dose_mg = 100
        supp_count = 1  # Рекомендуем начать с 1, можно до 2
    elif 17 <= weight <= 30:
        # 3-10 лет
        dose_text = "1 суппозиторий по 250 мг"
        age_group = "3-10 лет"
        dose_mg = 250
        supp_count = 1
    elif 31 <= weight <= 35:
        # 10-12 лет
        dose_text = "2 суппозитория по 250 мг"
        age_group = "10-12 лет"
        dose_mg = 250
        supp_count = 2
    else:
        # Вес вне диапазона
        import asyncio
        try:
            if weight < 4:
                await asyncio.wait_for(
                    update.message.reply_text(
                        "⚠️ Для детей с весом менее 4 кг применение препарата возможно только "
                        "по назначению врача. Пожалуйста, обратитесь к педиатру ❤️‍🩹",
                        reply_markup=ReplyKeyboardRemove()
                    ),
                    timeout=10.0
                )
            else:
                await asyncio.wait_for(
                    update.message.reply_text(
                        "⚠️ Для детей с весом более 35 кг рекомендуется консультация с педиатром "
                        "для подбора дозировки. ❤️‍🩹",
                        reply_markup=ReplyKeyboardRemove()
                    ),
                    timeout=10.0
                )
        except (asyncio.TimeoutError, Exception) as send_error:
            from telegram.error import TimedOut
            if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
                # Пробуем отправить упрощенное сообщение
                try:
                    await asyncio.wait_for(
                        update.message.reply_text(
                            "⚠️ Пожалуйста, обратитесь к педиатру",
                            reply_markup=ReplyKeyboardRemove()
                        ),
                        timeout=5.0
                    )
                except Exception:
                    pass
            else:
                raise
        return ConversationHandler.END
    
    # Формируем финальное сообщение
    dose_top = f"🔶 Рекомендуемая доза: {dose_text}"
    
    details_lines = [
        "Проверьте параметры расчета:",
        f"• Форма: Свечи (суппозитории)",
        f"• Препарат: Парацетамол",
        f"• Вес ребенка: {weight} кг",
        f"• Возрастная группа: {age_group}",
        "",
        "💡 Важно помнить:",
        "",
        "• Разовая доза составляет 10-15 мг/кг",
        "• Интервал между введениями: не менее 6 часов",
        "• Максимальная суточная доза: не более 60 мг/кг",
        "",
        "📋 Справка по дозировкам:",
        "",
        "• 3-12 месяцев (7-10 кг) — 1 суппозиторий 100 мг",
        "• 1-3 года (11-16 кг) — 1-2 суппозитория 100 мг",
        "• 3-10 лет (17-30 кг) — 1 суппозиторий 250 мг",
    ]
    
    footer = (
        "\n\n"
        "⚠️ Обратите внимание: Я — ИИ-помощник, а не врач. Мои подсказки носят справочный характер и не заменяют консультацию специалиста.\n\n"
        "Полезные напоминания:\n"
        "• Используйте свечи согласно инструкции.\n"
        "• При ухудшении состояния — обратитесь к педиатру\n"
        "• В неотложной ситуации — немедленно звоните 103"
    )
    
    full_text = dose_top + "\n\n" + "\n".join(details_lines) + footer
    
    # Кнопки для повторного расчета и премиума
    from app.utils import is_premium_user
    from app.storage import get_child_profile
    user = update.effective_user
    is_premium = await is_premium_user(user.id)
    
    buttons = [[InlineKeyboardButton("🔄 Посчитать другую дозу", callback_data="start_calculate")]]
    
    # Для премиум-пользователей: проверяем наличие профиля
    if is_premium:
        profile = await get_child_profile(user.id)
        if profile:
            # Есть профиль - добавляем кнопку "Записать приём в дневник"
            # Для свечей сохраняем данные расчета
            dose_data = {
                "drug": "paracetamol",  # Для свечей всегда парацетамол
                "dose_mg": dose_mg,
                "dose_ml": None,  # Для свечей нет мл
                "form": "suppository",
                "dose_text": dose_text
            }
            # Сохраняем в user_data для обработчика
            # context.user_data автоматически привязан к пользователю и чату
            context.user_data["last_dose_data"] = dose_data
            
            # Проверяем, есть ли записи в дневнике, чтобы показать кнопку просмотра
            from app.storage import has_dose_events
            has_events = await has_dose_events(user.id)
            
            buttons.append([InlineKeyboardButton("✅ Записать приём в дневник", callback_data="dose_save")])
            if has_events:
                buttons.append([InlineKeyboardButton("📖 Посмотреть дневник приема лекарств", callback_data="dose_diary")])
        else:
            # Нет профиля - показываем текст и кнопку создания профиля
            full_text += "\n\n💡 Чтобы сохранить прием лекарства, создайте профиль ребенка"
            buttons.append([InlineKeyboardButton("👶 Создать профиль", callback_data="start_create_profile")])
    else:
        # Добавляем кнопку о премиуме только для бесплатных пользователей
        buttons.append([InlineKeyboardButton("⭐ Узнать о Премиум", callback_data="start_premium_info")])
    
    # Добавляем кнопку "На главную" в конце
    buttons.append([InlineKeyboardButton("🏠 На главную", callback_data="start_home")])
    
    kb = InlineKeyboardMarkup(buttons)
    
    # Отправляем сообщение с обработкой таймаутов
    import asyncio
    try:
        await asyncio.wait_for(
            update.message.reply_text(full_text, reply_markup=kb),
            timeout=10.0
        )
    except (asyncio.TimeoutError, Exception) as send_error:
        from telegram.error import TimedOut
        if isinstance(send_error, (TimedOut, asyncio.TimeoutError)):
            # Пробуем отправить упрощенное сообщение
            try:
                simple_text = (
                    f"🔶 Рекомендуемая доза: {dose_text}\n\n"
                    f"Вес: {weight} кг\n"
                    f"Возрастная группа: {age_group}\n\n"
                    f"Интервал: не менее 6 часов"
                )
                await asyncio.wait_for(
                    update.message.reply_text(simple_text, reply_markup=kb),
                    timeout=5.0
                )
            except Exception:
                # Если и это не получилось, отправляем минимальное сообщение
                try:
                    await update.message.reply_text(
                        f"Доза: {dose_text}",
                        reply_markup=ReplyKeyboardRemove()
                    )
                except Exception:
                    pass  # Не критично, пользователь может попробовать еще раз
        else:
            raise
    
    return ConversationHandler.END

async def calculate_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Считаем дозу и сразу завершаем диалог.
    Никаких записей/остатков/подтверждений — только информация.
    """
    u = context.user_data

    req = DoseRequest(
        child_age_months=None,
        child_weight_kg=u["weight"],
        drug_key=u["drug"],
        concentration_mg_per_ml=u["conc_mg_per_ml"],
        last_dose_at=None,
        daily_total_mg=0.0
    )
    res = calc_dose(req)

    if not res.ok:
        await update.message.reply_text(f"⚠️ {res.message}\n{DISCLAIMER}", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    conc_text = f"{u['conc_mg_per_ml']:.1f} мг/мл" + (f" ({u.get('conc_label')})" if u.get("conc_label") else "")
    drug_name = "Парацетамол" if u["drug"] == "paracetamol" else "Ибупрофен"
    form_name = "Сироп" if u.get("form") == "syrup" else "Свечи"

    # Подпись формулы
    if u["drug"] == "ibuprofen":
        formula_line = "Формула: 10 мг/кг (ибупрофен)"
        interval_line = (
            "💡 Важно помнить:\n\n"
            "• Интервал между приёмами: каждые 6-8 часов\n"
            "• Максимальная суточная доза: 30 мг/кг"
        )
    else:
        formula_line = "Формула: 15 мг/кг (парацетамол)"
        interval_line = (
            "💡 Важно помнить:\n\n"
            "• Интервал между приёмами: каждые 4–6 часов\n"
            "• Максимальная суточная доза: 60 мг/кг"
        )

    # Мягкие подсказки после вакцинации (если отмечали ранее)
    post_vax_lines = []
    if u.get("recent_vax"):
        post_vax_lines.append(
            "После прививки жаропонижающее не дают заранее — только если есть жар/дискомфорт."
        )
        if u.get("menb") and u["drug"] == "paracetamol" and u.get("conc_label") == "120 мг/5 мл":
            post_vax_lines.append(
                "Если это была MenB у малыша ~2–4 мес: в некоторых протоколах (UK) советуют 3 дозы "
                "парацетамола по 2.5 мл (120 мг/5 мл) каждые 4–6 часов, начиная сразу после прививки. "
                "Уточните в вашей поликлинике."
            )
        elif u.get("menb") and u["drug"] == "ibuprofen":
            post_vax_lines.append("При MenB обычно используют парацетамол; обсудите выбор с врачом.")

    # ——— СБОРКА СООБЩЕНИЯ ———
    # 1) ВЫДВИГАЕМ ДОЗУ НА ПЕРВОЕ МЕСТО
    dose_top = f"🔶 Разовая доза по весу: ≈{res.dose_mg:.0f} мг (≈{res.dose_ml:.1f} мл)"

    # 2) Детали ниже
    details_lines = [
        "Проверьте параметры расчета:",
        f"• Форма: {form_name}",
        f"• Препарат: {drug_name}",
        f"• Вес ребенка: {u['weight']} кг",
        f"• Концентрация: {conc_text}",
        f"• {formula_line}",
        "",
        interval_line,
    ]
    if post_vax_lines:
        details_lines += ["", "Советы после вакцинации:"] + [f"- {l}" for l in post_vax_lines]

    footer = (
        "\n\n"
        "⚠️ Обратите внимание: Я — ИИ-помощник, а не врач. Мои подсказки носят справочный характер и не заменяют консультацию специалиста.\n\n"
        "Полезные напоминания:\n"
        "• Используйте мерный шприц/ложку из упаковки.\n"
        "• Тщательно взболтайте суспензию\n"
        "• При ухудшении состояния — обратитесь к педиатру\n"
        "• В неотложной ситуации — немедленно звоните 103"
    )

    full_text = dose_top + "\n\n" + "\n".join(details_lines) + footer
    
    # Кнопки для повторного расчета и премиума
    # Проверяем, является ли пользователь премиум
    from app.utils import is_premium_user
    from app.storage import get_child_profile
    user = update.effective_user
    is_premium = await is_premium_user(user.id)
    
    buttons = [[InlineKeyboardButton("🔄 Посчитать другую дозу", callback_data="start_calculate")]]
    
    # Для премиум-пользователей: проверяем наличие профиля
    if is_premium:
        profile = await get_child_profile(user.id)
        if profile:
            # Есть профиль - добавляем кнопку "Записать приём в дневник"
            # Сохраняем данные расчета для последующего сохранения
            dose_data = {
                "drug": u["drug"],
                "dose_mg": res.dose_mg,
                "dose_ml": res.dose_ml,
                "form": u.get("form", "syrup"),
                "conc_label": u.get("conc_label", "")
            }
            # Сохраняем в user_data для обработчика
            # context.user_data автоматически привязан к пользователю и чату
            context.user_data["last_dose_data"] = dose_data
            
            # Проверяем, есть ли записи в дневнике, чтобы показать кнопку просмотра
            from app.storage import has_dose_events
            has_events = await has_dose_events(user.id)
            
            buttons.append([InlineKeyboardButton("✅ Записать приём в дневник", callback_data="dose_save")])
            if has_events:
                buttons.append([InlineKeyboardButton("📖 Посмотреть дневник приема лекарств", callback_data="dose_diary")])
        else:
            # Нет профиля - показываем текст и кнопку создания профиля
            full_text += "\n\n💡 Чтобы сохранить прием лекарства, создайте профиль ребенка"
            buttons.append([InlineKeyboardButton("👶 Создать профиль", callback_data="start_create_profile")])
    else:
        # Добавляем кнопку о премиуме только для бесплатных пользователей
        buttons.append([InlineKeyboardButton("⭐ Узнать о Премиум", callback_data="start_premium_info")])
    
    # Добавляем кнопку "На главную" в конце
    buttons.append([InlineKeyboardButton("🏠 На главную", callback_data="start_home")])
    
    kb = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(full_text, reply_markup=kb)
    return ConversationHandler.END

def build_calculate_conversation():
    from telegram.ext import CallbackQueryHandler
    
    # Обработчик для кнопки "Рассчитать дозу" из /start
    async def handle_calculate_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        # Вызываем start_calculate напрямую, но через правильный механизм
        # Создаем обертку для сообщения, чтобы start_calculate мог работать
        class MessageWrapper:
            def __init__(self, original_msg):
                self._original = original_msg
                # Копируем все атрибуты
                for attr in dir(original_msg):
                    if not attr.startswith('_') and not callable(getattr(original_msg, attr, None)):
                        try:
                            setattr(self, attr, getattr(original_msg, attr))
                        except:
                            pass
                # Устанавливаем текст команды
                self.text = "/calculate"
                self.entities = None
                self.chat_id = original_msg.chat_id
                self.from_user = query.from_user
                
            async def reply_text(self, *args, **kwargs):
                return await self._original.reply_text(*args, **kwargs)
        
        wrapped_msg = MessageWrapper(query.message)
        fake_update = Update(update_id=update.update_id + 10000, message=wrapped_msg)
        
        # Вызываем start_calculate - он вернет ASK_FORM, и ConversationHandler установит состояние
        return await start_calculate(fake_update, context)
    
    return ConversationHandler(
        entry_points=[
            CommandHandler("calculate", start_calculate),
            CallbackQueryHandler(handle_calculate_button, pattern="^start_calculate$")
        ],
        states={
            ASK_FORM:        [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_form)],
            ASK_DRUG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_drug)],
            ASK_CONC_FIXED:  [MessageHandler(filters.TEXT & ~filters.COMMAND, set_fixed_conc)],
            ASK_WEIGHT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_weight)],
            ASK_SAFETY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_safety_answer)],
        },
        fallbacks=[CommandHandler("calculate", start_calculate)],  # Добавляем fallback для перезапуска
        allow_reentry=True,  # Разрешаем повторный вход
    )
