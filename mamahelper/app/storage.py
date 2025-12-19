from collections import defaultdict
from datetime import datetime, timedelta, timezone


# user_id -> drug -> list[(ts_utc, mg)]
_store = defaultdict(lambda: defaultdict(list))

def save_dose_event(user_id: int, drug_key: str, dose_mg: float) -> None:
    # было: datetime.now(timezone.utc) — оставляем именно так
    ts = datetime.now(timezone.utc).isoformat()
    # ... сохраняем вместе с ts ...

def _prune_older_than_24h(user_id: int, drug: str) -> None:
    now = datetime.now(timezone.utc)
    _store[user_id][drug] = [(ts, mg) for ts, mg in _store[user_id][drug] if now - ts <= timedelta(hours=24)]

def get_daily_total_mg(user_id: int, drug: str) -> float:
    _prune_older_than_24h(user_id, drug)
    return sum(mg for _, mg in _store[user_id][drug])

def get_last_dose_time(user_id: int, drug_key: str):
    # ... читаем записи и находим последнюю ts (ISO-строка) ...
    # предположим, что переменная last_iso = "2025-09-09T20:35:00+00:00" или без смещения
    try:
        dt = datetime.fromisoformat(last_iso)
    except Exception:
        return None

    if dt.tzinfo is None:
        # на старых записях мог не сохраниться tz — считаем, что это UTC
        dt = dt.replace(tzinfo=timezone.utc)

    # Возвращаем в UTC — так ждёт калькулятор
    return dt.astimezone(timezone.utc)

# app/storage.py
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"

def save_feedback(text: str, meta: dict) -> None:
    """Сохраняем одну запись обратной связи в JSONL (по строке на отзыв)."""
    record = {"text": text, "meta": meta}
    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
