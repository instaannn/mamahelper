# app/storage.py
import json
import aiosqlite
import asyncio
import logging
import re
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

# ---------- Настройки БД ----------
# Timeout для подключений к БД (в секундах)
DB_TIMEOUT = 30.0
# Максимальное количество попыток при блокировке БД
MAX_RETRIES = 5
# Начальная задержка между попытками (в секундах)
RETRY_DELAY = 0.1

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
    db = await _get_db_connection()
    try:
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
        
        # Таблица для отслеживания пользователей бота
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER NOT NULL PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                total_interactions INTEGER NOT NULL DEFAULT 0
            )
        """)
        
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
        
        # Таблица платежей для отслеживания транзакций
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                invoice_payload TEXT NOT NULL,
                provider_payment_charge_id TEXT,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                subscription_type TEXT,
                subscription_days INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES user_premium(user_id)
            )
        """)
        
        # Создаем индекс для быстрого поиска по user_id и статусу
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_payments_user_status 
            ON payments(user_id, status)
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
        
        # Таблица для отслеживания отправленных уведомлений о истечении премиума
        await db.execute("""
            CREATE TABLE IF NOT EXISTS premium_expiry_notifications (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                premium_until TEXT NOT NULL,
                notification_sent_at TEXT NOT NULL,
                days_until_expiry INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES user_premium(user_id)
            )
        """)
        
        # Создаем индекс для быстрого поиска по user_id и premium_until
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_premium_notifications_user_until 
            ON premium_expiry_notifications(user_id, premium_until)
        """)
        
        await db.commit()
    finally:
        await db.close()

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

# ---------- Вспомогательные функции для работы с БД ----------
async def _get_db_connection():
    """
    Создать подключение к БД с оптимизированными настройками.
    Используйте эту функцию для всех подключений вместо прямого вызова aiosqlite.connect.
    """
    db = await aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT)
    # Оптимизация SQLite для лучшей производительности и меньших блокировок
    await db.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
    await db.execute("PRAGMA synchronous=NORMAL")  # Баланс между скоростью и надежностью
    await db.execute("PRAGMA busy_timeout=30000")  # 30 секунд timeout
    await db.execute("PRAGMA cache_size=-64000")  # 64MB кэш (отрицательное значение в KB)
    await db.execute("PRAGMA temp_store=MEMORY")  # Временные таблицы в памяти
    await db.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
    await db.execute("PRAGMA foreign_keys=ON")  # Включаем внешние ключи
    await db.commit()
    return db

async def _db_connect_with_retry():
    """
    Подключиться к БД с повторными попытками при блокировке.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await _get_db_connection()
        except aiosqlite.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)  # Экспоненциальная задержка
                logging.warning(f"⚠️ БД заблокирована, попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"❌ Ошибка подключения к БД: {e}")
                raise
        except Exception as e:
            logging.error(f"❌ Ошибка подключения к БД: {e}")
            raise

# ---------- Премиум-подписка пользователей ----------
async def is_user_premium(user_id: int) -> bool:
    """Проверить, есть ли у пользователя премиум-подписка бота."""
    # Используем retry логику для подключения
    for attempt in range(MAX_RETRIES):
        db = None
        try:
            db = await _db_connect_with_retry()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT is_premium, premium_until FROM user_premium WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await db.close()
                    return False
                
                # Проверяем is_premium (может быть 0, 1, или булево значение)
                is_premium_value = row["is_premium"]
                # Преобразуем в булево значение (SQLite хранит как INTEGER: 0 или 1)
                is_premium_bool = bool(int(is_premium_value)) if is_premium_value is not None else False
                
                # Проверяем, не истекла ли подписка
                if is_premium_bool:
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
                            await db.close()
                            return False
                    await db.close()
                    return True
                
                await db.close()
                return False
        except aiosqlite.OperationalError as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                logging.warning(f"⚠️ БД заблокирована при проверке премиума для user_id={user_id}, попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                await asyncio.sleep(delay)
                continue
            else:
                # В случае ошибки БД считаем, что премиум нет (безопасный вариант)
                logging.warning(f"⚠️ Ошибка БД при проверке премиума для user_id={user_id}: {e}, возвращаем False")
                return False
        except Exception as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            # В случае любой ошибки считаем, что премиум нет (безопасный вариант)
            logging.warning(f"⚠️ Ошибка при проверке премиума для user_id={user_id}: {e}, возвращаем False")
            return False
    
    # Если все попытки исчерпаны
    logging.warning(f"⚠️ Не удалось проверить премиум для user_id={user_id} после {MAX_RETRIES} попыток, возвращаем False")
    return False

async def set_user_premium(user_id: int, is_premium: bool, premium_until: Optional[datetime] = None) -> None:
    """Установить премиум-статус пользователя."""
    now = datetime.now(timezone.utc)
    
    # Используем retry логику для подключения
    for attempt in range(MAX_RETRIES):
        db = None
        try:
            db = await _db_connect_with_retry()
            db.row_factory = aiosqlite.Row
            
            # Проверяем, есть ли уже запись
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
            
            # Проверяем, что данные действительно сохранились
            async with db.execute(
                "SELECT is_premium, premium_until FROM user_premium WHERE user_id = ?",
                (user_id,)
            ) as verify_cursor:
                verify_row = await verify_cursor.fetchone()
                if verify_row:
                    saved_is_premium = bool(int(verify_row["is_premium"])) if verify_row["is_premium"] is not None else False
                    saved_premium_until = verify_row["premium_until"]
                    logging.info(
                        f"✅ Премиум сохранен для user_id={user_id}: "
                        f"is_premium={saved_is_premium}, premium_until={saved_premium_until}"
                    )
                    if not saved_is_premium and is_premium:
                        logging.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Премиум не сохранился для user_id={user_id}!")
                        raise ValueError(f"Премиум не сохранился в БД для user_id={user_id}")
                else:
                    logging.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Запись не найдена после сохранения для user_id={user_id}!")
                    raise ValueError(f"Запись не найдена после сохранения для user_id={user_id}")
            
            await db.close()
            return
        except aiosqlite.OperationalError as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                logging.warning(f"⚠️ БД заблокирована при установке премиума для user_id={user_id}, попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"❌ Ошибка БД при установке премиума для user_id={user_id}: {e}")
                raise
        except Exception as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            logging.error(f"❌ Неожиданная ошибка при установке премиума для user_id={user_id}: {e}", exc_info=True)
            raise

# ---------- Отслеживание пользователей ----------
async def track_user_interaction(user_id: int) -> None:
    """
    Отследить взаимодействие пользователя с ботом.
    Вызывается при каждом взаимодействии (команда, сообщение и т.д.)
    """
    now = datetime.now(timezone.utc)
    
    # Используем retry логику для подключения
    for attempt in range(MAX_RETRIES):
        db = None
        try:
            db = await _db_connect_with_retry()
            
            # Проверяем, есть ли уже запись
            async with db.execute(
                "SELECT user_id FROM bot_users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                existing = await cursor.fetchone()
                
                if existing:
                    # Обновляем последнее взаимодействие
                    await db.execute("""
                        UPDATE bot_users
                        SET last_seen_at = ?,
                            total_interactions = total_interactions + 1
                        WHERE user_id = ?
                    """, (now.isoformat(), user_id))
                else:
                    # Создаем новую запись
                    await db.execute("""
                        INSERT INTO bot_users
                        (user_id, first_seen_at, last_seen_at, total_interactions)
                        VALUES (?, ?, ?, 1)
                    """, (user_id, now.isoformat(), now.isoformat()))
            
            await db.commit()
            await db.close()
            return
        except aiosqlite.OperationalError as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                logging.warning(f"⚠️ БД заблокирована при отслеживании взаимодействия для user_id={user_id}, попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                await asyncio.sleep(delay)
                continue
            else:
                # Не критично, просто логируем
                logging.warning(f"⚠️ Ошибка БД при отслеживании взаимодействия для user_id={user_id}: {e}")
                return
        except Exception as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            # Не критично, просто логируем
            logging.warning(f"⚠️ Ошибка при отслеживании взаимодействия для user_id={user_id}: {e}")
            return

# ---------- Статистика ----------
async def get_bot_statistics() -> dict:
    """
    Получить статистику бота.
    
    Returns:
        Словарь со статистикой:
        - total_users: всего уникальных пользователей
        - active_users_30d: активных пользователей за последние 30 дней
        - active_users_7d: активных пользователей за последние 7 дней
        - premium_active: активных премиум подписок
        - premium_total: всего когда-либо оформленных премиум подписок
        - payments_completed: успешных платежей
        - payments_pending: ожидающих платежей
        - revenue_total: общая выручка (в копейках)
        - subscriptions_1month: подписок на 1 месяц
        - subscriptions_3months: подписок на 3 месяца
    """
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    seven_days_ago = now - timedelta(days=7)
    
    stats = {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        try:
            # Всего уникальных пользователей (из bot_users или из других таблиц)
            try:
                async with db.execute("SELECT COUNT(DISTINCT user_id) as count FROM bot_users") as cursor:
                    row = await cursor.fetchone()
                    stats["total_users"] = row[0] if row and row[0] is not None else 0
            except Exception:
                # Если таблица bot_users не существует, считаем из других таблиц
                async with db.execute("""
                    SELECT COUNT(DISTINCT user_id) as count FROM (
                        SELECT user_id FROM user_premium
                        UNION
                        SELECT user_id FROM child_profiles
                        UNION
                        SELECT user_id FROM dose_events
                    )
                """) as cursor:
                    row = await cursor.fetchone()
                    stats["total_users"] = row[0] if row and row[0] is not None else 0
            
            # Активные пользователи за 30 дней
            try:
                async with db.execute("""
                    SELECT COUNT(DISTINCT user_id) as count 
                    FROM bot_users 
                    WHERE last_seen_at >= ?
                """, (thirty_days_ago.isoformat(),)) as cursor:
                    row = await cursor.fetchone()
                    stats["active_users_30d"] = row[0] if row and row[0] is not None else 0
            except Exception:
                stats["active_users_30d"] = 0
            
            # Активные пользователи за 7 дней
            try:
                async with db.execute("""
                    SELECT COUNT(DISTINCT user_id) as count 
                    FROM bot_users 
                    WHERE last_seen_at >= ?
                """, (seven_days_ago.isoformat(),)) as cursor:
                    row = await cursor.fetchone()
                    stats["active_users_7d"] = row[0] if row and row[0] is not None else 0
            except Exception:
                stats["active_users_7d"] = 0
            
            # Активные премиум подписки
            async with db.execute("""
                SELECT COUNT(*) as count 
                FROM user_premium 
                WHERE is_premium = 1 
                    AND (premium_until IS NULL OR premium_until > ?)
            """, (now.isoformat(),)) as cursor:
                row = await cursor.fetchone()
                stats["premium_active"] = row[0] if row and row[0] is not None else 0
            
            # Всего когда-либо было премиум подписок
            async with db.execute("SELECT COUNT(*) as count FROM user_premium WHERE is_premium = 1 OR premium_until IS NOT NULL") as cursor:
                row = await cursor.fetchone()
                stats["premium_total"] = row[0] if row and row[0] is not None else 0
            
            # Успешные платежи
            try:
                async with db.execute("""
                    SELECT COUNT(*) as count, SUM(amount) as total
                    FROM payments 
                    WHERE status = 'completed'
                """) as cursor:
                    row = await cursor.fetchone()
                    stats["payments_completed"] = row[0] if row and row[0] is not None else 0
                    stats["revenue_total"] = int(row[1]) if row and row[1] is not None else 0
            except Exception:
                stats["payments_completed"] = 0
                stats["revenue_total"] = 0
            
            # Ожидающие платежи
            try:
                async with db.execute("""
                    SELECT COUNT(*) as count 
                    FROM payments 
                    WHERE status = 'pending'
                """) as cursor:
                    row = await cursor.fetchone()
                    stats["payments_pending"] = row[0] if row and row[0] is not None else 0
            except Exception:
                stats["payments_pending"] = 0
            
            # Подписки на 1 месяц
            try:
                async with db.execute("""
                    SELECT COUNT(*) as count 
                    FROM payments 
                    WHERE status = 'completed' AND subscription_type = '1month'
                """) as cursor:
                    row = await cursor.fetchone()
                    stats["subscriptions_1month"] = row[0] if row and row[0] is not None else 0
            except Exception:
                stats["subscriptions_1month"] = 0
            
            # Подписки на 3 месяца
            try:
                async with db.execute("""
                    SELECT COUNT(*) as count 
                    FROM payments 
                    WHERE status = 'completed' AND subscription_type = '3months'
                """) as cursor:
                    row = await cursor.fetchone()
                    stats["subscriptions_3months"] = row[0] if row and row[0] is not None else 0
            except Exception:
                stats["subscriptions_3months"] = 0
                
        except Exception as e:
            import logging
            logging.error(f"Error in get_bot_statistics: {e}", exc_info=True)
            # Возвращаем пустую статистику в случае ошибки
            stats = {
                "total_users": 0,
                "active_users_30d": 0,
                "active_users_7d": 0,
                "premium_active": 0,
                "premium_total": 0,
                "payments_completed": 0,
                "payments_pending": 0,
                "revenue_total": 0,
                "subscriptions_1month": 0,
                "subscriptions_3months": 0
            }
    
    return stats

# ---------- Платежи ----------
async def save_payment(
    user_id: int,
    invoice_payload: str,
    amount: int,
    currency: str,
    subscription_type: str,
    subscription_days: int
) -> None:
    """
    Сохранить информацию о платеже в БД.
    Вызывается при создании инвойса (счета на оплату).
    
    Args:
        user_id: ID пользователя
        invoice_payload: Уникальный идентификатор платежа (payload из инвойса)
        amount: Сумма платежа в копейках
        currency: Валюта (например, "RUB")
        subscription_type: Тип подписки (например, "1month", "3months")
        subscription_days: Количество дней подписки (30 или 90)
    """
    now = datetime.now(timezone.utc)
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO payments
                (user_id, invoice_payload, amount, currency, subscription_type, subscription_days, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                user_id,
                invoice_payload,
                amount,
                currency,
                subscription_type,
                subscription_days,
                now.isoformat()
            ))
            await db.commit()
            import logging
            logging.info(f"✅ Платеж сохранен: user_id={user_id}, payload={invoice_payload}, amount={amount} {currency}")
        except Exception as e:
            import logging
            logging.error(f"❌ Ошибка при сохранении платежа в БД: {e}", exc_info=True)
            raise

async def complete_payment(
    invoice_payload: str,
    provider_payment_charge_id: str
) -> Optional[dict]:
    """
    Отметить платеж как выполненный и активировать премиум-подписку.
    Вызывается после успешной оплаты.
    
    Args:
        invoice_payload: Уникальный идентификатор платежа (payload из инвойса)
        provider_payment_charge_id: ID платежа от провайдера
    
    Returns:
        Словарь с информацией о платеже и подписке, или None если платеж не найден
    """
    now = datetime.now(timezone.utc)
    
    # Используем retry логику для всех операций
    for attempt in range(MAX_RETRIES):
        db = None
        try:
            db = await _db_connect_with_retry()
            db.row_factory = aiosqlite.Row
            
            # Сначала пытаемся найти платеж со статусом 'pending'
            async with db.execute("""
                SELECT user_id, subscription_days, status
                FROM payments
                WHERE invoice_payload = ? AND status = 'pending'
            """, (invoice_payload,)) as cursor:
            row = await cursor.fetchone()
            
            # Если не нашли pending, проверяем, может быть платеж уже обработан
            if not row:
                import logging
                logging.warning(f"⚠️ Платеж с payload '{invoice_payload}' не найден со статусом 'pending'")
                
                # Проверяем, может быть платеж уже был обработан
                async with db.execute("""
                    SELECT user_id, subscription_days, status
                    FROM payments
                    WHERE invoice_payload = ?
                """, (invoice_payload,)) as cursor2:
                    row2 = await cursor2.fetchone()
                    if row2:
                        logging.warning(f"⚠️ Платеж с payload '{invoice_payload}' уже обработан (статус: {row2['status']})")
                        # Если платеж уже обработан, все равно активируем премиум (на случай если активация не прошла)
                        user_id = row2["user_id"]
                        subscription_days = row2["subscription_days"]
                        row = row2  # Используем найденную запись
                    else:
                        logging.error(f"❌ Платеж с payload '{invoice_payload}' не найден в БД вообще!")
                        # Пытаемся извлечь user_id из payload
                        # Форматы: premium_1month_{user_id}_{timestamp} или premium1month{user_id}{timestamp}
                        try:
                            user_id = None
                            subscription_days = None
                            amount = None
                            
                            # Пробуем формат с подчеркиваниями: premium_1month_{user_id}_{timestamp}
                            if '_' in invoice_payload:
                                parts = invoice_payload.split('_')
                                if len(parts) >= 3:
                                    user_id_str = parts[2]
                                    user_id = int(user_id_str)
                            else:
                                # Формат без подчеркиваний: premium1month{user_id}{timestamp}
                                # Ищем паттерн: premium + 1month/3months + число (user_id)
                                match = re.search(r'premium(1month|3months)(\d+)', invoice_payload)
                                if match:
                                    user_id = int(match.group(2))
                            
                            if user_id is None:
                                raise ValueError("Не удалось извлечь user_id")
                            
                            # Определяем тип подписки из payload
                            if '1month' in invoice_payload:
                                subscription_days = 30
                                amount = 99
                            elif '3months' in invoice_payload:
                                subscription_days = 90
                                amount = 270
                            else:
                                logging.error(f"❌ Не удалось определить тип подписки из payload: {invoice_payload}")
                                return None
                                
                                # Создаем запись о платеже вручную
                                logging.warning(f"⚠️ Создаем запись о платеже вручную для user_id={user_id}")
                                await db.execute("""
                                    INSERT INTO payments
                                    (user_id, invoice_payload, amount, currency, subscription_type, subscription_days, status, created_at, completed_at, provider_payment_charge_id)
                                    VALUES (?, ?, ?, 'RUB', ?, ?, 'completed', ?, ?, ?)
                                """, (
                                    user_id,
                                    invoice_payload,
                                    amount * 100,  # в копейках
                                    '1month' if subscription_days == 30 else '3months',
                                    subscription_days,
                                    now.isoformat(),
                                    now.isoformat(),
                                    provider_payment_charge_id
                                ))
                                await db.commit()
                                
                                # Продолжаем обработку
                                row = {"user_id": user_id, "subscription_days": subscription_days, "status": "completed"}
                            else:
                                logging.error(f"❌ Неверный формат payload: {invoice_payload}")
                                return None
                        except (ValueError, IndexError) as e:
                            logging.error(f"❌ Ошибка при извлечении данных из payload '{invoice_payload}': {e}")
                            return None
            
            if not row:
                return None
            
            user_id = row["user_id"]
            subscription_days = row["subscription_days"]
            
                # Обновляем статус платежа
                await db.execute("""
                    UPDATE payments
                    SET status = 'completed',
                        provider_payment_charge_id = ?,
                        completed_at = ?
                    WHERE invoice_payload = ?
                """, (provider_payment_charge_id, now.isoformat(), invoice_payload))
                
                # Активируем премиум-подписку
                # Получаем текущую дату окончания премиума (если есть)
                async with db.execute("""
                    SELECT premium_until FROM user_premium WHERE user_id = ?
                """, (user_id,)) as cursor2:
                    existing_row = await cursor2.fetchone()
                    if existing_row and existing_row["premium_until"]:
                        # Если подписка уже есть, продлеваем её
                        current_until = datetime.fromisoformat(existing_row["premium_until"])
                        if current_until > now:
                            # Подписка еще активна - продлеваем от текущей даты окончания
                            new_until = current_until + timedelta(days=subscription_days)
                        else:
                            # Подписка истекла - начинаем с сегодня
                            new_until = now + timedelta(days=subscription_days)
                    else:
                        # Нет активной подписки - начинаем с сегодня
                        new_until = now + timedelta(days=subscription_days)
                
            # Устанавливаем премиум-статус (эта функция сама управляет подключением)
            await set_user_premium(user_id, True, new_until)
            
            await db.commit()
            
            # Закрываем соединение перед возвратом
            await db.close()
            
            return {
                "user_id": user_id,
                "subscription_days": subscription_days,
                "premium_until": new_until
            }
        except aiosqlite.OperationalError as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                logging.warning(f"⚠️ БД заблокирована при обработке платежа '{invoice_payload}', попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"❌ Ошибка БД при обработке платежа '{invoice_payload}': {e}")
                raise
        except Exception as e:
            if db:
                try:
                    await db.close()
                except:
                    pass
            logging.error(f"❌ Неожиданная ошибка при обработке платежа '{invoice_payload}': {e}", exc_info=True)
            raise
    
    # Если все попытки исчерпаны
    logging.error(f"❌ Не удалось обработать платеж '{invoice_payload}' после {MAX_RETRIES} попыток")
    return None
            except aiosqlite.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY * (2 ** attempt)
                    logging.warning(f"⚠️ БД заблокирована при обработке платежа, попытка {attempt + 1}/{MAX_RETRIES}, ждем {delay:.2f}с...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"❌ Ошибка БД при обработке платежа: {e}")
                    raise
            except Exception as e:
                logging.error(f"❌ Неожиданная ошибка при обработке платежа: {e}", exc_info=True)
                raise
        finally:
            if db:
                await db.close()

# ---------- Уведомления о истечении премиума ----------
async def get_users_with_expiring_premium(min_days: int = 3, max_days: int = 5) -> List[tuple]:
    """
    Получить список пользователей, у которых премиум истекает через min_days-max_days дней.
    
    Args:
        min_days: Минимальное количество дней до истечения (по умолчанию 3)
        max_days: Максимальное количество дней до истечения (по умолчанию 5)
    
    Returns:
        Список кортежей (user_id, premium_until, days_until_expiry)
    """
    now = datetime.now(timezone.utc)
    min_date = now + timedelta(days=min_days)
    max_date = now + timedelta(days=max_days)
    
    users = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT user_id, premium_until
            FROM user_premium
            WHERE is_premium = 1
                AND premium_until IS NOT NULL
                AND premium_until >= ?
                AND premium_until <= ?
        """, (min_date.isoformat(), max_date.isoformat())) as cursor:
            async for row in cursor:
                premium_until = datetime.fromisoformat(row["premium_until"])
                days_until = (premium_until - now).days
                users.append((row["user_id"], row["premium_until"], days_until))
    
    return users

async def get_users_with_expired_premium() -> List[tuple]:
    """
    Получить список пользователей, у которых премиум истек сегодня (в течение последних 24 часов).
    
    Returns:
        Список кортежей (user_id, premium_until)
    """
    now = datetime.now(timezone.utc)
    # Ищем пользователей, у которых премиум истек в течение последних 24 часов
    # но статус еще не обновлен (is_premium = 1)
    yesterday = now - timedelta(days=1)
    
    users = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT user_id, premium_until
            FROM user_premium
            WHERE is_premium = 1
                AND premium_until IS NOT NULL
                AND premium_until >= ?
                AND premium_until <= ?
        """, (yesterday.isoformat(), now.isoformat())) as cursor:
            async for row in cursor:
                premium_until = datetime.fromisoformat(row["premium_until"])
                # Проверяем, что премиум действительно истек
                if premium_until <= now:
                    users.append((row["user_id"], row["premium_until"]))
    
    return users

async def disable_expired_premium_subscriptions() -> int:
    """
    Автоматически отключить все истекшие премиум подписки.
    
    Returns:
        Количество отключенных подписок
    """
    now = datetime.now(timezone.utc)
    disabled_count = 0
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Находим все истекшие подписки
        async with db.execute("""
            SELECT user_id, premium_until
            FROM user_premium
            WHERE is_premium = 1
                AND premium_until IS NOT NULL
                AND premium_until <= ?
        """, (now.isoformat(),)) as cursor:
            expired_subscriptions = await cursor.fetchall()
            
            # Отключаем каждую истекшую подписку
            for row in expired_subscriptions:
                user_id = row[0]
                await db.execute("""
                    UPDATE user_premium
                    SET is_premium = 0,
                        updated_at = ?
                    WHERE user_id = ?
                """, (now.isoformat(), user_id))
                disabled_count += 1
        
        await db.commit()
    
    if disabled_count > 0:
        logging.info(f"✅ Автоматически отключено {disabled_count} истекших премиум подписок")
    
    return disabled_count

async def has_notification_been_sent(user_id: int, premium_until: str) -> bool:
    """
    Проверить, было ли уже отправлено уведомление для данного пользователя и даты окончания премиума.
    
    Args:
        user_id: ID пользователя
        premium_until: Дата окончания премиума (ISO формат)
    
    Returns:
        True если уведомление уже было отправлено, False иначе
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) as count
            FROM premium_expiry_notifications
            WHERE user_id = ? AND premium_until = ?
        """, (user_id, premium_until)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0] and row[0] > 0)

async def mark_notification_sent(user_id: int, premium_until: str, days_until_expiry: int) -> None:
    """
    Отметить, что уведомление было отправлено пользователю.
    
    Args:
        user_id: ID пользователя
        premium_until: Дата окончания премиума (ISO формат)
        days_until_expiry: Количество дней до истечения
    """
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO premium_expiry_notifications
            (user_id, premium_until, notification_sent_at, days_until_expiry)
            VALUES (?, ?, ?, ?)
        """, (user_id, premium_until, now.isoformat(), days_until_expiry))
        await db.commit()
