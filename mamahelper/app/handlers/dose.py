# app/handlers/dose.py
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
        await update.message.reply_text(
            "Свечи (Цефекон) скоро добавим 🧸\n"
            "Пока доступен расчёт для сиропов: введите /calculate и выберите «Сироп».",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    await update.message.reply_text("Не понял выбор. Давайте начнём заново: /calculate")
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

    await update.message.reply_text("Сколько весит ребёнок? Напишите число, например: 11.2", reply_markup=ReplyKeyboardRemove())
    return ASK_WEIGHT

async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод веса + ранние стопы по массе и подготовка чекеров безопасности (Да/Нет)."""
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        weight = float(text)
    except Exception:
        await update.message.reply_text("Не получилось понять вес 😅 Введите просто число, например: 11.2")
        return ASK_WEIGHT

    context.user_data["weight"] = weight

    # Ранние стопы по массе для ибупрофена
    if context.user_data.get("drug") == "ibuprofen":
        # 40 мг/мл — противопоказание <10 кг
        if context.user_data.get("conc_label") == "40 мг/мл" and weight < 10:
            await update.message.reply_text(
                "Для ибупрофена 40 мг/мл: масса тела ребёнка менее 10 кг — противопоказание. "
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
    queue.append(("recent_vax", "Была вакцинация сегодня или вчера? 💉"))

    if context.user_data.get("drug") == "paracetamol":
        queue.append(("under2m", "Ребёнку меньше 2 месяцев?"))

    if context.user_data.get("drug") == "ibuprofen" and context.user_data.get("conc_label") == "40 мг/мл":
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

    if key == "recent_vax":
        context.user_data["recent_vax"] = (answer == "да")
        if context.user_data["recent_vax"]:
            context.user_data["current_check"] = ("menb", "Это была прививка MenB у малыша примерно 2–4 месяцев? 👶")
            kb = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(context.user_data["current_check"][1], reply_markup=kb)
            return ASK_SAFETY

    elif key == "menb":
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
            "Для ибупрофена 40 мг/мл: возраст до 12 месяцев — противопоказание. "
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
        interval_line = "Интервал между приёмами: каждые 6–8 часов. Максимум за сутки: 30 мг/кг."
    else:
        formula_line = "Формула: 15 мг/кг (парацетамол)"
        interval_line = "Интервал между приёмами: каждые 4–6 часов. Максимум за сутки: 60 мг/кг."

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
        "Проверьте, пожалуйста:",
        f"• Форма: {form_name}",
        f"• Препарат: {drug_name}",
        f"• Вес: {u['weight']} кг",
        f"• Концентрация: {conc_text}",
        f"• {formula_line}",
        "",
        interval_line,
    ]
    if post_vax_lines:
        details_lines += ["", "Советы после вакцинации:"] + [f"- {l}" for l in post_vax_lines]

    footer = (
        "\n\n" + DISCLAIMER + "\n\n"
        "Чтобы посчитать ещё раз — введите /calculate."
    )

    full_text = dose_top + "\n\n" + "\n".join(details_lines) + footer

    await update.message.reply_text(full_text, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def build_calculate_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("calculate", start_calculate)],
        states={
            ASK_FORM:        [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_form)],
            ASK_DRUG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_drug)],
            ASK_CONC_FIXED:  [MessageHandler(filters.TEXT & ~filters.COMMAND, set_fixed_conc)],
            ASK_WEIGHT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_weight)],
            ASK_SAFETY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_safety_answer)],
        },
        fallbacks=[],
        allow_reentry=False,
    )
