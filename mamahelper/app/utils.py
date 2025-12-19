# app/utils.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from typing import Any, Dict, Optional

from zoneinfo import ZoneInfo
import yaml

# ---------- Локализация времени: Москва ----------
# Москва (MSK): UTC+3 круглый год, без перехода на летнее/зимнее
LOCAL_TZ = ZoneInfo("Europe/Moscow")


def to_local(dt: datetime) -> datetime:
    """
    Перевести aware datetime в московское время.
    Если пришёл naive — считаем, что это UTC и помечаем tzinfo=UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def humanize_dt(dt: datetime) -> str:
    """
    Вернуть строку вида:
    'сегодня в HH:MM (через X ч Y мин)'
    во времени Москвы (Europe/Moscow).
    """
    dt_local = to_local(dt)
    now_local = datetime.now(LOCAL_TZ)

    delta = dt_local - now_local
    total_seconds = int(delta.total_seconds())

    # если время уже наступило
    if total_seconds <= 0:
        day_label = (
            "сегодня" if dt_local.date() == now_local.date()
            else "завтра" if dt_local.date() == (now_local.date() + timedelta(days=1))
            else dt_local.strftime("%Y-%m-%d")
        )
        return f"{day_label} в {dt_local.strftime('%H:%M')}"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    parts = []
    if hours > 0:
        parts.append(f"{hours} ч")
    if minutes > 0:
        parts.append(f"{minutes} мин")
    rel = "через " + " ".join(parts) if parts else "скоро"

    day_label = (
        "сегодня" if dt_local.date() == now_local.date()
        else "завтра" if dt_local.date() == (now_local.date() + timedelta(days=1))
        else dt_local.strftime("%Y-%m-%d")
    )
    return f"{day_label} в {dt_local.strftime('%H:%M')} ({rel})"


# ---------- Загрузка формуляра ----------
# Путь к YAML с формуляром
_FORMULARY_PATH = Path(__file__).resolve().parent / "data" / "formulary_lv.yml"


@lru_cache(maxsize=1)
def load_formulary() -> Dict[str, Any]:
    """
    Прочитать формуляр (YAML) и вернуть как dict.
    Кешируем результат в памяти; чтобы сбросить — вызвать load_formulary.cache_clear().
    """
    if not _FORMULARY_PATH.exists():
        raise FileNotFoundError(f"Не найден формуляр: {_FORMULARY_PATH}")

    with _FORMULARY_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # На всякий случай приведём ожидаемую структуру
    if "drugs" not in data:
        data["drugs"] = {}
    return data


def formulary_path() -> Path:
    """Утилита: вернуть путь к текущему YAML с формуляром (иногда полезно для отладки)."""
    return _FORMULARY_PATH
