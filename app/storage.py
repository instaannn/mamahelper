# app/storage.py
import json
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from collections import defaultdict

from app.models import ChildProfile

# ---------- Пути и файлы ----------
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"
DB_PATH = DATA_DIR / "bot.db"

# ---------- Хранение записей дневника в БД ----------
# Теперь все записи сохраняются в таблице dose_events в SQLite

async def save_dose_event(user_id: int, drug_key: str, dose_mg: float, metadata: dict = None) -> None:
    """Сохранить событие приема лекарства в БД.
    
    Args:
        user_id: ID пользователя
        drug_key: Ключ препарата (paracetamol/ibuprofen)
        dose_mg: Доза в мг
        metadata: Дополнительные данные (form, dose_ml, conc_label, weight_kg, dose_text)
    """
    ts = datetime.now(timezone.utc)
    metadata = metadata or {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO dose_events 
            (user_id, drug_key, dose_mg, form, dose_ml, conc_label, weight_kg, dose_text, child_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            drug_key,
            dose_mg,
            metadata.get("form"),
            metadata.get("dose_ml"),
            metadata.get("conc_label", ""),
            metadata.get("weight_kg"),
            metadata.get("dose_text", f"{dose_mg:.0f} мг"),
            metadata.get("child_name"),
            ts.isoformat()
        ))
        await db.commit()
    
    # Очищаем старые записи (старше 24 часов) из БД
    await _prune_older_than_24h(user_id, drug_key)

async def _prune_older_than_24h(user_id: int, drug: str) -> None:
    """Удалить записи старше 24 часов из БД."""
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    async with aiosqlite.connect(DB_PATH) as db:
        if drug:
            await db.execute("""
                DELETE FROM dose_events 
                WHERE user_id = ? AND drug_key = ? AND created_at < ?
            """, (user_id, drug, cutoff_time.isoformat()))
        else:
            await db.execute("""
                DELETE FROM dose_events 
                WHERE user_id = ? AND created_at < ?
            """, (user_id, cutoff_time.isoformat()))
        await db.commit()

async def get_daily_total_mg(user_id: int, drug: str, child_name: str = None) -> float:
    """Получить суточную дозу за последние 24 часа из БД.
    
    Args:
        user_id: ID пользователя
        drug: Ключ препарата (paracetamol/ibuprofen)
        child_name: Имя ребенка (опционально, для фильтрации по профилю)
    """
    await _prune_older_than_24h(user_id, drug)
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    async with aiosqlite.connect(DB_PATH) as db:
        if child_name:
            # Фильтруем по имени ребенка, если указано
            async with db.execute("""
                SELECT SUM(dose_mg) as total
                FROM dose_events
                WHERE user_id = ? AND drug_key = ? AND child_name = ? AND created_at >= ?
            """, (user_id, drug, child_name, cutoff_time.isoformat())) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)
        else:
            # Без фильтрации по имени (для обратной совместимости)
            async with db.execute("""
                SELECT SUM(dose_mg) as total
                FROM dose_events
                WHERE user_id = ? AND drug_key = ? AND created_at >= ?
            """, (user_id, drug, cutoff_time.isoformat())) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)

async def get_last_dose_time(user_id: int, drug_key: str):
    """Получить время последнего приема лекарства из БД."""
    await _prune_older_than_24h(user_id, drug_key)
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT created_at
            FROM dose_events
            WHERE user_id = ? AND drug_key = ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id, drug_key, cutoff_time.isoformat())) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return datetime.fromisoformat(row[0]).astimezone(timezone.utc)

async def get_all_dose_events(user_id: int, drug_key: str = None):
    """Получить все записи приема лекарств за последние 24 часа из БД.
    
    Args:
        user_id: ID пользователя
        drug_key: Ключ препарата (paracetamol/ibuprofen) или None для всех
    
    Returns:
        Список кортежей (timestamp, drug_key, dose_mg, metadata), отсортированный по времени
    """
    await _prune_older_than_24h(user_id, drug_key or "")
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    all_events = []
    
    async with aiosqlite.connect(DB_PATH) as db:
        if drug_key:
            async with db.execute("""
                SELECT created_at, drug_key, dose_mg, form, dose_ml, conc_label, weight_kg, dose_text, child_name
                FROM dose_events
                WHERE user_id = ? AND drug_key = ? AND created_at >= ?
                ORDER BY created_at ASC
            """, (user_id, drug_key, cutoff_time.isoformat())) as cursor:
                async for row in cursor:
                    ts = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
                    metadata = {
                        "form": row[3],
                        "dose_ml": row[4],
                        "conc_label": row[5] or "",
                        "weight_kg": row[6],
                        "dose_text": row[7] or f"{row[2]:.0f} мг",
                        "child_name": row[8] if len(row) > 8 else None
                    }
                    all_events.append((ts, row[1], row[2], metadata))
        else:
            async with db.execute("""
                SELECT created_at, drug_key, dose_mg, form, dose_ml, conc_label, weight_kg, dose_text, child_name
                FROM dose_events
                WHERE user_id = ? AND created_at >= ?
                ORDER BY created_at ASC
            """, (user_id, cutoff_time.isoformat())) as cursor:
                async for row in cursor:
                    ts = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
                    metadata = {
                        "form": row[3],
                        "dose_ml": row[4],
                        "conc_label": row[5] or "",
                        "weight_kg": row[6],
                        "dose_text": row[7] or f"{row[2]:.0f} мг",
                        "child_name": row[8] if len(row) > 8 else None
                    }
                    all_events.append((ts, row[1], row[2], metadata))
    
    return all_events

async def has_dose_events(user_id: int) -> bool:
    """Проверить, есть ли записи в дневнике за последние 24 часа в БД."""
    await _prune_older_than_24h(user_id, "")
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) as count
            FROM dose_events
            WHERE user_id = ? AND created_at >= ?
        """, (user_id, cutoff_time.isoformat())) as cursor:
            row = await cursor.fetchone()
            return bool(row[0] and row[0] > 0)

# ---------- Feedback (JSONL) ----------
def save_feedback(text: str, meta: dict) -> None:
    """Сохраняем одну запись обратной связи в JSONL (по строке на отзыв)."""
    record = {"text": text, "meta": meta}
    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ---------- База данных (SQLite) ----------
async def init_db() -> None:
    """Инициализация БД: создание таблиц при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица профилей детей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS child_profiles (
                profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                child_name TEXT,
                child_age_months INTEGER,
                child_weight_kg REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Миграция: если таблица существует без profile_id, добавляем колонку
        try:
            # Проверяем, есть ли колонка profile_id
            async with db.execute("PRAGMA table_info(child_profiles)") as cursor:
                columns = await cursor.fetchall()
                has_profile_id = any(col[1] == "profile_id" for col in columns)
            
            if not has_profile_id:
                # Создаем новую таблицу с profile_id
                await db.execute("""
                    CREATE TABLE child_profiles_new (
                        profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        child_name TEXT,
                        child_age_months INTEGER,
                        child_weight_kg REAL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                # Копируем данные из старой таблицы
                await db.execute("""
                    INSERT INTO child_profiles_new 
                    (user_id, child_name, child_age_months, child_weight_kg, created_at, updated_at)
                    SELECT user_id, child_name, child_age_months, child_weight_kg, created_at, updated_at
                    FROM child_profiles
                """)
                # Удаляем старую таблицу и переименовываем новую
                await db.execute("DROP TABLE child_profiles")
                await db.execute("ALTER TABLE child_profiles_new RENAME TO child_profiles")
                await db.commit()
        except Exception as e:
            # Ошибка миграции - логируем, но продолжаем работу
            import logging
            logging.warning(f"Migration error (may be expected): {e}")
            pass
        
        # Таблица премиум-подписок пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_premium (
                user_id INTEGER NOT NULL,
                is_premium INTEGER NOT NULL DEFAULT 0,
                premium_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id)
            )
        """)
        
        # Таблица записей дневника приема лекарств
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dose_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                drug_key TEXT NOT NULL,
                dose_mg REAL NOT NULL,
                form TEXT,
                dose_ml REAL,
                conc_label TEXT,
                weight_kg REAL,
                dose_text TEXT,
                child_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES user_premium(user_id)
            )
        """)
        
        # Миграция: добавляем поле child_name, если его нет
        try:
            await db.execute("ALTER TABLE dose_events ADD COLUMN child_name TEXT")
            await db.commit()
        except Exception:
            # Поле уже существует, игнорируем ошибку
            pass
        
        # Создаем индекс для быстрого поиска по user_id и created_at
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dose_events_user_created 
            ON dose_events(user_id, created_at)
        """)
        
        await db.commit()

async def get_child_profile(user_id: int, profile_id: Optional[int] = None) -> Optional[ChildProfile]:
    """Получить профиль ребенка для пользователя.
    
    Args:
        user_id: ID пользователя
        profile_id: ID профиля (если None, возвращает первый профиль пользователя)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if profile_id:
            query = "SELECT * FROM child_profiles WHERE user_id = ? AND profile_id = ?"
            params = (user_id, profile_id)
        else:
            # Возвращаем первый профиль (самый новый)
            query = "SELECT * FROM child_profiles WHERE user_id = ? ORDER BY created_at DESC LIMIT 1"
            params = (user_id,)
        
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            
            return ChildProfile(
                profile_id=row["profile_id"],
                user_id=row["user_id"],
                child_name=row["child_name"],
                child_age_months=row["child_age_months"],
                child_weight_kg=row["child_weight_kg"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )

async def get_all_child_profiles(user_id: int) -> List[ChildProfile]:
    """Получить все профили детей для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM child_profiles WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                ChildProfile(
                    profile_id=row["profile_id"],
                    user_id=row["user_id"],
                    child_name=row["child_name"],
                    child_age_months=row["child_age_months"],
                    child_weight_kg=row["child_weight_kg"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in rows
            ]

async def save_child_profile(
    user_id: int,
    child_name: Optional[str] = None,
    child_age_months: Optional[int] = None,
    child_weight_kg: Optional[float] = None,
    profile_id: Optional[int] = None,
) -> ChildProfile:
    """Сохранить или обновить профиль ребенка.
    
    Args:
        user_id: ID пользователя
        child_name: Имя ребенка
        child_age_months: Возраст в месяцах
        child_weight_kg: Вес в кг
        profile_id: ID профиля для обновления (если None, создается новый профиль)
    """
    now = datetime.now(timezone.utc)
    
    if profile_id:
        # Обновляем существующий профиль
        existing = await get_child_profile(user_id, profile_id)
        if not existing:
            raise ValueError(f"Profile {profile_id} not found for user {user_id}")
        
        updated_name = child_name if child_name is not None else existing.child_name
        updated_age = child_age_months if child_age_months is not None else existing.child_age_months
        updated_weight = child_weight_kg if child_weight_kg is not None else existing.child_weight_kg
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE child_profiles
                SET child_name = ?,
                    child_age_months = ?,
                    child_weight_kg = ?,
                    updated_at = ?
                WHERE profile_id = ? AND user_id = ?
            """, (
                updated_name,
                updated_age,
                updated_weight,
                now.isoformat(),
                profile_id,
                user_id,
            ))
            await db.commit()
        
        return ChildProfile(
            profile_id=profile_id,
            user_id=user_id,
            child_name=updated_name,
            child_age_months=updated_age,
            child_weight_kg=updated_weight,
            created_at=existing.created_at,
            updated_at=now,
        )
    else:
        # Создаем новый профиль
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO child_profiles
                (user_id, child_name, child_age_months, child_weight_kg, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                child_name,
                child_age_months,
                child_weight_kg,
                now.isoformat(),
                now.isoformat(),
            ))
            await db.commit()
            profile_id = cursor.lastrowid
        
        return ChildProfile(
            profile_id=profile_id,
            user_id=user_id,
            child_name=child_name,
            child_age_months=child_age_months,
            child_weight_kg=child_weight_kg,
            created_at=now,
            updated_at=now,
        )

async def delete_child_profile(user_id: int, profile_id: Optional[int] = None) -> bool:
    """Удалить профиль ребенка. Возвращает True если профиль был удален.
    
    Args:
        user_id: ID пользователя
        profile_id: ID профиля для удаления (если None, удаляет все профили пользователя)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if profile_id:
            cursor = await db.execute(
                "DELETE FROM child_profiles WHERE user_id = ? AND profile_id = ?",
                (user_id, profile_id)
            )
        else:
            cursor = await db.execute(
                "DELETE FROM child_profiles WHERE user_id = ?",
                (user_id,)
            )
        await db.commit()
        return cursor.rowcount > 0

# ---------- Премиум-подписка пользователей ----------
async def is_user_premium(user_id: int) -> bool:
    """Проверить, есть ли у пользователя премиум-подписка бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT is_premium, premium_until FROM user_premium WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            
            # Проверяем, не истекла ли подписка
            if row["is_premium"]:
                if row["premium_until"]:
                    premium_until = datetime.fromisoformat(row["premium_until"])
                    if datetime.now(timezone.utc) > premium_until:
                        # Подписка истекла - обновляем статус напрямую в БД
                        now = datetime.now(timezone.utc)
                        await db.execute("""
                            UPDATE user_premium
                            SET is_premium = 0,
                                updated_at = ?
                            WHERE user_id = ?
                        """, (now.isoformat(), user_id))
                        await db.commit()
                        return False
                return True
            
            return False

async def set_user_premium(user_id: int, is_premium: bool, premium_until: Optional[datetime] = None) -> None:
    """Установить премиум-статус пользователя."""
    now = datetime.now(timezone.utc)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, есть ли уже запись
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id FROM user_premium WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            existing = await cursor.fetchone()
            
            if existing:
                # Обновляем существующую запись
                await db.execute("""
                    UPDATE user_premium
                    SET is_premium = ?,
                        premium_until = ?,
                        updated_at = ?
                    WHERE user_id = ?
                """, (
                    1 if is_premium else 0,
                    premium_until.isoformat() if premium_until else None,
                    now.isoformat(),
                    user_id,
                ))
            else:
                # Создаем новую запись
                await db.execute("""
                    INSERT INTO user_premium
                    (user_id, is_premium, premium_until, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    user_id,
                    1 if is_premium else 0,
                    premium_until.isoformat() if premium_until else None,
                    now.isoformat(),
                    now.isoformat(),
                ))
        
        await db.commit()
