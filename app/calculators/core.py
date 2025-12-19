# app/calculators/core.py
from datetime import datetime, timedelta, timezone
from app.models import DoseRequest, DoseResult
from app.utils import load_formulary

def _find_ibuprofen_conc_cfg(mg_per_ml: float):
    f = load_formulary()
    fixed = f["drugs"]["ibuprofen"]["routes"]["oral"].get("fixed_concentrations", [])
    for item in fixed:
        if float(item.get("mg_per_ml", 0)) == float(mg_per_ml):
            return item
    return None

def _age_band_ibuprofen_ml(age_months: int | None, mg_per_ml: float) -> float | None:
    if age_months is None:
        return None
    cfg = _find_ibuprofen_conc_cfg(mg_per_ml)
    if not cfg:
        return None
    for row in cfg.get("age_band_ml", []):
        if row["min_months"] <= age_months <= row["max_months"]:
            return float(row["ml"])
    return None

def calc_dose(req: DoseRequest) -> DoseResult:
    f = load_formulary()
    drug = f["drugs"].get(req.drug_key)
    if not drug:
        return DoseResult(
            ok=False,
            message="Ой, не узнаю препарат 😕 Проверьте название или попробуйте снова.",
            flags=["unknown_drug"]
        )

    # Парацетамол <2 мес — только если возраст известен
    if req.drug_key == "paracetamol" and req.child_age_months is not None and req.child_age_months < 2:
        return DoseResult(
            ok=False,
            message=(
                "Для парацетамола: возраст ребёнка младше 2 месяцев — противопоказание без назначения врача. "
                "Пожалуйста, обсудите это с педиатром ❤️‍🩹"
            ),
            flags=["paracetamol_contra_age_under_2m"]
        )

    # Глобальный <3 мес (если известен) — дополнительная защита
    if req.child_age_months is not None and req.child_age_months < 3:
        return DoseResult(
            ok=False,
            message=(
                "Малыш младше 3 месяцев. Не давайте жаропонижающее без назначения врача. "
                "Если температура ≥ 38 °C — это «красный флаг»: нужна срочная очная оценка врача."
            ),
            flags=["age_under_3_months"]
        )

    # Порог по возрасту из формуляра — только если возраст известен
    if req.child_age_months is not None and req.child_age_months < int(drug["min_age_months"]):
        return DoseResult(
            ok=False,
            message="Для этого возраста препарат без назначения врача не рекомендуем. Пожалуйста, обсудите с педиатром.",
            flags=["age_restriction"]
        )

    # Ибупрофен: базовая защита по массе ≥5 кг
    if req.drug_key == "ibuprofen" and req.child_weight_kg < 5:
        return DoseResult(
            ok=False,
            message=(
                "Для ибупрофена: масса тела до 5 кг — противопоказание без назначения врача. "
                "Пожалуйста, обратитесь к педиатру ❤️‍🩹"
            ),
            flags=["ibuprofen_weight_gate_any_age"]
        )

    # Ибупрофен 40 мг/мл (200 мг/5мл): концентрация-специфичные «ворота»
    if req.drug_key == "ibuprofen" and req.route == "oral":
        conc_cfg = _find_ibuprofen_conc_cfg(req.concentration_mg_per_ml)
        if conc_cfg:
            min_w = float(conc_cfg.get("min_weight_kg", 0) or 0)
            if min_w and req.child_weight_kg < min_w:
                return DoseResult(
                    ok=False,
                    message=(
                        "Для ибупрофена 200 мг/5мл (40 мг/мл): масса тела ребёнка менее 10 кг — противопоказание. "
                        "Нужна консультация педиатра ❤️‍🩹"
                    ),
                    flags=["ibuprofen_40_contra_weight"]
                )
            min_age = int(conc_cfg.get("min_age_months", 0) or 0)
            if min_age and (req.child_age_months is not None) and req.child_age_months < min_age:
                return DoseResult(
                    ok=False,
                    message=(
                        "Для ибупрофена 200 мг/5мл (40 мг/мл): возраст до 12 месяцев — противопоказание. "
                        "Нужна консультация педиатра ❤️‍🩹"
                    ),
                    flags=["ibuprofen_40_contra_age"]
                )

    # Концентрация
    if req.concentration_mg_per_ml <= 0 or req.concentration_mg_per_ml > 200:
        return DoseResult(
            ok=False,
            message="Пожалуйста, проверьте концентрацию на флаконе (мг/мл).",
            flags=["bad_concentration"]
        )

    # Разовая доза
    lo, hi = map(float, drug["mg_per_kg_single_dose_range"])
    if req.drug_key == "ibuprofen":
        target = 10.0   # 10 мг/кг
    elif req.drug_key == "paracetamol":
        target = 15.0   # 15 мг/кг
    else:
        target = (lo + hi) / 2.0
    dose_mg = req.child_weight_kg * target

    # Интервал
    if req.last_dose_at:
        min_next = req.last_dose_at + timedelta(hours=int(drug["min_interval_hours"]))
        now = datetime.now(timezone.utc)
        if now < min_next:
            return DoseResult(
                ok=False,
                message=f"Ещё рано для следующей дозы. Минимальный интервал — {drug['min_interval_hours']} ч.",
                min_next_time=min_next,
                flags=["interval_violation"]
            )

    # Суточный максимум
    max_daily = float(drug["max_daily_mg_per_kg"]) * req.child_weight_kg
    if req.daily_total_mg + dose_mg > max_daily:
        return DoseResult(
            ok=False,
            message=(
                "Похоже, суточный максимум будет превышен 😔 "
                "Проверьте предыдущие приёмы и при сомнениях свяжитесь с педиатром."
            ),
            flags=["max_daily_exceeded"]
        )

    # мг → мл (округление 0.5 мл)
    dose_ml = round((dose_mg / req.concentration_mg_per_ml) * 2) / 2.0

    # Текст и подсказки
    msg = "Готово: посчитали разовую дозу по весу (подсказка, не заменяет врача)."
    if req.drug_key == "ibuprofen":
        msg += " В расчёте использовано 10 мг/кг на приём."
    if req.drug_key == "paracetamol":
        msg += " В расчёте использовано 15 мг/кг на приём."

    # Подсказка по «возрастной полосе» только если возраст известен
    if req.drug_key == "ibuprofen" and req.route == "oral":
        band_ml = _age_band_ibuprofen_ml(req.child_age_months, req.concentration_mg_per_ml)
        if band_ml is not None:
            postfix = " при 100 мг/5 мл" if req.concentration_mg_per_ml == 20 else " при 200 мг/5мл (40 мг/мл)"
            msg += f" По возрастной подсказке обычно используют ~{band_ml:.1f} мл{postfix}."

    daily_remaining = max_daily - (req.daily_total_mg + dose_mg)
    return DoseResult(
        ok=True,
        message=msg,
        dose_mg=round(dose_mg, 0),
        dose_ml=round(dose_ml, 1),
        min_next_time=None,
        daily_remaining_mg=round(daily_remaining, 0),
        flags=[]
    )
