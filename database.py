# --- START OF FILE database.py ---

import logging
import time
import re
import random
import asyncpg
from typing import Dict, Any, List, Optional

# Импортируем конфигурацию и справочники из config.py
from config import DB_CONFIG, BOOKS

logger = logging.getLogger(__name__)

# Глобальная переменная для хранения пула соединений с БД
db_pool: asyncpg.Pool = None

# --- Вспомогательные функции ---

def _extract_hadith_number(text: str) -> int | None:
    """
    Извлекает первый найденный номер хадиса из текста.
    Улучшенная версия для форматов:
    "Номер хадиса: 1", "№ 123", "Hadith number: 55"
    """
    if not text:
        return None
    
    # Флаги re.IGNORECASE | re.MULTILINE нужны для поиска по строкам
    patterns = [
        r'^Номер хадиса\s*:\s*(\d+)',  # Строгое начало строки (как в вашей БД)
        r'Номер хадиса\s*:\s*(\d+)',   # Где-то в тексте
        r'Hadith number\s*:\s*(\d+)',
        r'Number\s*:\s*(\d+)', 
        r'№\s*(\d+)', 
        r'номер\s+(\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                continue
    return None

async def _column_exists(conn, table_name: str, column_name: str) -> bool:
    """Проверяет, существует ли колонка в таблице."""
    return await conn.fetchval(f"""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='{table_name}' AND column_name='{column_name}'
    """)

async def _get_column_type(conn, table_name: str, column_name: str) -> str:
    """Получает тип данных колонки."""
    return await conn.fetchval(f"""
        SELECT data_type FROM information_schema.columns
        WHERE table_name='{table_name}' AND column_name='{column_name}'
    """)

# --- Функции инициализации и подключения ---
async def init_db():
    """
    Инициализирует пул соединений с базой данных и обновляет схему.
    """
    global db_pool
    try:
        # Настройки для Supabase (Transaction Pooler)
        db_pool = await asyncpg.create_pool(
            **DB_CONFIG, 
            statement_cache_size=0, 
            min_size=5,             
            max_size=20
        )

        async with db_pool.acquire() as conn:
            # 1. Создание таблиц
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY, 
                    username TEXT, first_name TEXT, language TEXT DEFAULT 'ru',
                    hadith_mode TEXT DEFAULT 'sequential', selected_book TEXT DEFAULT 'bukhari',
                    registered_at DOUBLE PRECISION, blocked INTEGER DEFAULT 0,
                    auto_progress_sequential BOOLEAN DEFAULT FALSE,
                    reminders_enabled BOOLEAN DEFAULT TRUE,
                    reminder_frequency INTEGER DEFAULT 12,
                    reminder_time TEXT DEFAULT '09:00',
                    last_reminder_sent_at DOUBLE PRECISION DEFAULT 0
                )''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS hadiths (
                    id SERIAL PRIMARY KEY, text TEXT NOT NULL,
                    language TEXT NOT NULL, book TEXT NOT NULL,
                    audio_file_id TEXT DEFAULT NULL,
                    hadith_number INTEGER
                )''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS progress (
                    user_id BIGINT, hadith_id INTEGER, language TEXT,
                    PRIMARY KEY (user_id, hadith_id, language),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (hadith_id) REFERENCES hadiths(id) ON DELETE CASCADE
                )''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    role TEXT DEFAULT 'participant',
                    created_at DOUBLE PRECISION DEFAULT extract(epoch from now()),
                    created_by TEXT DEFAULT NULL
                )''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pending_hadiths (
                    id SERIAL PRIMARY KEY,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    book TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    submitted_at DOUBLE PRECISION DEFAULT extract(epoch from now()),
                    status TEXT DEFAULT 'pending'
                )''')

            # --- МИГРАЦИИ ---

            # Миграция №1: user_id -> BIGINT
            try:
                user_id_type = await _get_column_type(conn, 'users', 'user_id')
                if user_id_type and 'text' in user_id_type.lower():
                    logger.warning("Миграция: Конвертация user_id в BIGINT...")
                    await conn.execute('ALTER TABLE progress DROP CONSTRAINT IF EXISTS progress_user_id_fkey')
                    await conn.execute('ALTER TABLE users ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint')
                    await conn.execute('ALTER TABLE progress ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint')
                    await conn.execute('''
                        ALTER TABLE progress 
                        ADD CONSTRAINT progress_user_id_fkey 
                        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    ''')
            except Exception as e:
                logger.error(f"Ошибка при миграции user_id: {e}")

            # Миграция №2: hadith_number
            if not await _column_exists(conn, 'hadiths', 'hadith_number'):
                await conn.execute('ALTER TABLE hadiths ADD COLUMN hadith_number INTEGER')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_hadith_search ON hadiths (book, language, hadith_number)')

            # Миграция №3: last_reminder_sent_at
            if not await _column_exists(conn, 'users', 'last_reminder_sent_at'):
                await conn.execute('ALTER TABLE users ADD COLUMN last_reminder_sent_at DOUBLE PRECISION DEFAULT 0')

            # --- МИГРАЦИЯ №4: AUDIO_TYPE (ИСПРАВЛЕНИЕ ОШИБКИ №8) ---
            if not await _column_exists(conn, 'hadiths', 'audio_type'):
                # Добавляем колонку для хранения типа файла ('audio' или 'voice')
                # По умолчанию ставим 'audio', так как это более универсально.
                # Если старые файлы - это voice, бот попытается отправить как audio, получит ошибку,
                # и мы научим его обновлять тип (self-healing) в bot_handlers.py
                await conn.execute("ALTER TABLE hadiths ADD COLUMN audio_type TEXT DEFAULT 'audio'")
                logger.info("Миграция: Добавлена колонка 'audio_type'.")

            # --- ОПТИМИЗАЦИЯ ---
            try:
                await conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_hadiths_text_trgm ON hadiths USING gin (text gin_trgm_ops)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_search_trgm ON users USING gin (username gin_trgm_ops, first_name gin_trgm_ops)')
            except Exception:
                pass # Игнорируем, если нет прав суперюзера

            # Главный админ
            from config import WEB_USERNAME, WEB_PASSWORD
            if not await conn.fetchval('SELECT id FROM admins WHERE username = $1', WEB_USERNAME):
                await conn.execute(
                    'INSERT INTO admins (username, password, role) VALUES ($1, $2, $3)',
                    WEB_USERNAME, WEB_PASSWORD, 'main_admin'
                )

    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")
        raise

# --- Функции для работы с пользователями (users) ---

async def save_user(user_id: int, username: str = None, first_name: str = None):
    async with db_pool.acquire() as conn:
        current_time = time.time()
        await conn.execute(
            '''INSERT INTO users (user_id, username, first_name, registered_at, last_reminder_sent_at) 
               VALUES ($1, $2, $3, $4, $5) 
               ON CONFLICT (user_id) DO UPDATE 
               SET username = EXCLUDED.username, 
                   first_name = EXCLUDED.first_name,
                   blocked = 0''',
            user_id, username, first_name, current_time, current_time
        )

async def get_user_language(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval('SELECT language FROM users WHERE user_id = $1', user_id) or 'ru'

async def set_user_language(user_id: int, language: str):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET language = $1 WHERE user_id = $2', language, user_id)

async def get_user_mode(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval('SELECT hadith_mode FROM users WHERE user_id = $1', user_id) or 'sequential'

async def set_user_mode(user_id: int, mode: str):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET hadith_mode = $1 WHERE user_id = $2', mode, user_id)

async def get_user_selected_book(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval('SELECT selected_book FROM users WHERE user_id = $1', user_id) or 'bukhari'

async def set_user_selected_book(user_id: int, book: str):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET selected_book = $1 WHERE user_id = $2', book, user_id)

async def set_user_blocked(user_id: int, blocked: bool):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET blocked = $1 WHERE user_id = $2', 1 if blocked else 0, user_id)

async def get_users_list(page: int, per_page: int, search_query: str = None) -> Dict[str, Any]:
    offset = (page - 1) * per_page
    async with db_pool.acquire() as conn:
        if search_query:
            query_param = f"%{search_query}%"
            try:
                search_id = int(search_query)
            except ValueError:
                search_id = None

            if search_id:
                where_clause = 'WHERE user_id = $1 OR username ILIKE $2 OR first_name ILIKE $2'
                args = [search_id, query_param]
            else:
                where_clause = 'WHERE CAST(user_id AS TEXT) LIKE $1 OR username ILIKE $1 OR first_name ILIKE $1'
                args = [query_param]

            count_query = f'SELECT COUNT(*) FROM users {where_clause}'
            data_query = f'SELECT user_id, username, first_name, language, registered_at, blocked FROM users {where_clause} ORDER BY registered_at DESC LIMIT ${len(args)+1} OFFSET ${len(args)+2}'
            
            total_count = await conn.fetchval(count_query, *args)
            records = await conn.fetch(data_query, *args, per_page, offset)
        else:
            total_count = await conn.fetchval('SELECT COUNT(*) FROM users')
            records = await conn.fetch(
                'SELECT user_id, username, first_name, language, registered_at, blocked FROM users ORDER BY registered_at DESC LIMIT $1 OFFSET $2',
                per_page, offset
            )
        
        return {
            "users": [dict(r) for r in records],
            "total_count": total_count
        }

async def get_user_by_id(user_id: str | int):
    try:
        uid = int(user_id)
    except ValueError:
        return None
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow('SELECT user_id, first_name FROM users WHERE user_id = $1', uid)
        return dict(record) if record else None

# --- Функции для работы с хадисами (hadiths) ---

async def save_hadith(text: str, language: str, book: str, hadith_number: int = None):
    if hadith_number is None:
        hadith_number = _extract_hadith_number(text)
        
    async with db_pool.acquire() as conn:
        # При создании нового хадиса аудио нет, audio_type по умолчанию NULL или 'audio' из схемы
        await conn.execute(
            'INSERT INTO hadiths (text, language, book, hadith_number) VALUES ($1, $2, $3, $4)',
            text, language, book, hadith_number
        )

async def update_hadith(hadith_id: int, text: str, hadith_number: int = None):
    if hadith_number is None:
        hadith_number = _extract_hadith_number(text)
        
    async with db_pool.acquire() as conn:
        await conn.execute(
            'UPDATE hadiths SET text = $1, hadith_number = $2 WHERE id = $3',
            text, hadith_number, hadith_id
        )

async def delete_hadith(hadith_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM hadiths WHERE id = $1', hadith_id)

# ИСПРАВЛЕНИЕ: Добавлен аргумент audio_type
async def update_hadith_audio(hadith_id: int, audio_file_id: str, audio_type: str = 'audio'):
    """
    Обновляет file_id и тип аудиофайла (audio/voice).
    """
    async with db_pool.acquire() as conn:
        # Убедимся, что audio_type обновляется
        await conn.execute(
            'UPDATE hadiths SET audio_file_id = $1, audio_type = $2 WHERE id = $3', 
            audio_file_id, audio_type, hadith_id
        )

async def get_hadith_by_id(hadith_id: int):
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow('SELECT * FROM hadiths WHERE id = $1', hadith_id)
        return dict(record) if record else None

async def get_hadiths_list(page: int, per_page: int, search_query: str = None, book_filter: str = None, lang_filter: str = None) -> Dict[str, Any]:
    offset = (page - 1) * per_page
    params = []
    where_clauses = []

    if search_query:
        params.append(f"%{search_query}%")
        where_clauses.append(f"text ILIKE ${len(params)}")
    if book_filter:
        params.append(book_filter)
        where_clauses.append(f"book = ${len(params)}")
    if lang_filter:
        params.append(lang_filter)
        where_clauses.append(f"language = ${len(params)}")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    async with db_pool.acquire() as conn:
        total_count = await conn.fetchval(f"SELECT COUNT(*) FROM hadiths {where_sql}", *params)
        
        # Запрашиваем audio_type
        data_query = f"""
            SELECT id, text, language, book, audio_file_id, hadith_number, audio_type 
            FROM hadiths {where_sql} 
            ORDER BY book ASC, hadith_number ASC NULLS LAST, id DESC 
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        records = await conn.fetch(data_query, *params, per_page, offset)

        hadiths = [
            {
                "id": r['id'], "text": r['text'], "language": r['language'],
                "book": r['book'], "book_name": BOOKS.get(r['book'], {}).get(r['language'], r['book']),
                "audio_file_id": r['audio_file_id'], "hadith_number": r['hadith_number'],
                "audio_type": r.get('audio_type') or 'audio' # Fallback на всякий случай
            } for r in records
        ]

    return {"hadiths": hadiths, "total_count": total_count}

async def search_hadith_by_number(number_text, language, selected_book):
    try:
        search_number = int(number_text.strip())
    except (ValueError, TypeError):
        return None

    async with db_pool.acquire() as conn:
        record = await conn.fetchrow(
            'SELECT id, text, audio_file_id, audio_type FROM hadiths WHERE language = $1 AND book = $2 AND hadith_number = $3',
            language, selected_book, search_number
        )
        return dict(record) if record else None

# --- Функции для работы с прогрессом (progress) ---

async def load_progress(user_id: int, language: str):
    async with db_pool.acquire() as conn:
        records = await conn.fetch('SELECT hadith_id FROM progress WHERE user_id = $1 AND language = $2', user_id, language)
        return {r['hadith_id'] for r in records}

async def save_progress(user_id: int, hadith_id: int, language: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO progress (user_id, hadith_id, language) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
            user_id, hadith_id, language
        )

async def reset_user_progress(user_id: int, language: str, books_to_reset: List[str]):
    if not books_to_reset:
        return 0
    async with db_pool.acquire() as conn:
        placeholders = ', '.join(f'${i+3}' for i in range(len(books_to_reset)))
        query = f'DELETE FROM progress WHERE user_id = $1 AND language = $2 AND hadith_id IN (SELECT id FROM hadiths WHERE book IN ({placeholders}))'
        result = await conn.execute(query, user_id, language, *books_to_reset)
        deleted_count_match = re.search(r'(\d+)$', result)
        return int(deleted_count_match.group(1)) if deleted_count_match else 0

# --- Функции для работы с напоминаниями (reminders) ---

async def get_reminder_settings(user_id: int):
    async with db_pool.acquire() as conn:
        result = await conn.fetchrow('SELECT reminders_enabled, reminder_frequency, reminder_time FROM users WHERE user_id = $1', user_id)
        if result:
            return {
                'enabled': bool(result['reminders_enabled']),
                'frequency': result['reminder_frequency'] or 12,
                'time': result['reminder_time'] or '09:00'
            }
        return {'enabled': True, 'frequency': 12, 'time': '09:00'}

async def set_reminder_enabled(user_id: int, enabled: bool):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET reminders_enabled = $1 WHERE user_id = $2', enabled, user_id)

async def set_reminder_frequency(user_id: int, frequency: int):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET reminder_frequency = $1 WHERE user_id = $2', frequency, user_id)

async def set_reminder_time(user_id: int, time_str: str):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET reminder_time = $1 WHERE user_id = $2', time_str, user_id)

# --- Функции для статистики и отчетов ---

async def get_user_report():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval('SELECT COUNT(*) FROM users')
        blocked = await conn.fetchval('SELECT COUNT(*) FROM users WHERE blocked = 1')
        return total, blocked

async def get_detailed_user_stats(user_id: int, language: str):
    stats = []
    async with db_pool.acquire() as conn:
        for book_key, book_names in BOOKS.items():
            total_count = await conn.fetchval('SELECT COUNT(*) FROM hadiths WHERE book = $1 AND language = $2', book_key, language)
            studied_count = await conn.fetchval(
                'SELECT COUNT(p.hadith_id) FROM progress p JOIN hadiths h ON p.hadith_id = h.id WHERE p.user_id = $1 AND h.language = $2 AND h.book = $3',
                user_id, language, book_key
            )
            stats.append({'book_name': book_names.get(language, book_key), 'studied': studied_count, 'total': total_count})
    return stats

async def get_user_detailed_stats_by_id(user_id: str | int):
    try:
        uid = int(user_id)
    except ValueError:
        return None, None
        
    async with db_pool.acquire() as conn:
        user_data = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", uid)
        if not user_data:
            return None, None

        language = user_data['language']
        stats = []
        for book_key, book_names in BOOKS.items():
            total_count = await conn.fetchval('SELECT COUNT(*) FROM hadiths WHERE book = $1 AND language = $2', book_key, language)
            studied_count = await conn.fetchval(
                'SELECT COUNT(p.hadith_id) FROM progress p JOIN hadiths h ON p.hadith_id = h.id WHERE p.user_id = $1 AND h.language = $2 AND h.book = $3',
                uid, language, book_key
            )
            percentage = (studied_count / total_count * 100) if total_count > 0 else 0
            stats.append({
                'book_name': book_names.get(language, book_key),
                'studied': studied_count, 'total': total_count, 'percentage': percentage
            })
        return dict(user_data), stats

# --- Функции для работы с аудио в админке ---

async def get_hadiths_without_audio(page: int, per_page: int, search_query: str = None) -> Dict[str, Any]:
    offset = (page - 1) * per_page
    params = []
    where_clauses = ["audio_file_id IS NULL"]

    if search_query:
        params.append(f"%{search_query}%")
        where_clauses.append(f"text ILIKE ${len(params)}")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    async with db_pool.acquire() as conn:
        total_count = await conn.fetchval(f"SELECT COUNT(*) FROM hadiths {where_sql}", *params)
        data_query = f"SELECT id, text, language, book, audio_file_id FROM hadiths {where_sql} ORDER BY id ASC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        records = await conn.fetch(data_query, *params, per_page, offset)
        
        hadiths = [
            {
                "id": r['id'], "text": r['text'], "language": r['language'],
                "book": r['book'], "book_name": BOOKS.get(r['book'], {}).get(r['language'], r['book']),
                "audio_file_id": r['audio_file_id']
            } for r in records
        ]

    return {"hadiths": hadiths, "total_count": total_count}

async def get_hadiths_without_audio_count() -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM hadiths WHERE audio_file_id IS NULL')

# --- Функции для работы с администраторами ---

async def get_admin_by_username(username: str):
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow('SELECT * FROM admins WHERE username = $1', username)
        return dict(record) if record else None

async def create_admin(username: str, password: str, role: str, created_by: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO admins (username, password, role, created_by) VALUES ($1, $2, $3, $4)',
            username, password, role, created_by
        )

async def get_all_admins():
    async with db_pool.acquire() as conn:
        records = await conn.fetch("SELECT * FROM admins WHERE role != 'main_admin' ORDER BY created_at DESC")
        return [dict(r) for r in records]

async def delete_admin(admin_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE id = $1', admin_id)


# --- Функции для работы с предложенными хадисами ---

async def create_pending_hadith(text: str, language: str, book: str, submitted_by: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO pending_hadiths (text, language, book, submitted_by) VALUES ($1, $2, $3, $4)',
            text, language, book, submitted_by
        )

async def get_pending_hadiths():
    async with db_pool.acquire() as conn:
        records = await conn.fetch("SELECT * FROM pending_hadiths WHERE status = 'pending' ORDER BY submitted_at DESC")
        result = []
        for r in records:
            hadith_dict = dict(r)
            hadith_dict['book_name'] = BOOKS.get(r['book'], {}).get(r['language'], r['book'])
            result.append(hadith_dict)
        return result

async def get_pending_hadith_by_id(hadith_id: int):
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow('SELECT * FROM pending_hadiths WHERE id = $1', hadith_id)
        return dict(record) if record else None

async def approve_pending_hadith(hadith_id: int, edited_text: str = None):
    async with db_pool.acquire() as conn:
        pending = await conn.fetchrow('SELECT * FROM pending_hadiths WHERE id = $1', hadith_id)
        if not pending:
            return False
        
        final_text = edited_text if edited_text else pending['text']
        hadith_number = _extract_hadith_number(final_text)
        
        await conn.execute(
            'INSERT INTO hadiths (text, language, book, hadith_number) VALUES ($1, $2, $3, $4)',
            final_text, pending['language'], pending['book'], hadith_number
        )
        
        await conn.execute("UPDATE pending_hadiths SET status = 'approved' WHERE id = $1", hadith_id)
        return True

async def reject_pending_hadith(hadith_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE pending_hadiths SET status = 'rejected' WHERE id = $1", hadith_id)

async def update_pending_hadith(hadith_id: int, text: str):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE pending_hadiths SET text = $1 WHERE id = $2', text, hadith_id)

async def get_pending_hadiths_count():
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM pending_hadiths WHERE status = 'pending'")

async def get_last_hadith_by_book_and_language(book: str, language: str):
    async with db_pool.acquire() as conn:
        # Запрашиваем также audio_type для полноты данных
        record = await conn.fetchrow(
            'SELECT id, text, hadith_number, audio_file_id, audio_type FROM hadiths WHERE book = $1 AND language = $2 ORDER BY id DESC LIMIT 1',
            book, language
        )
        return dict(record) if record else None

async def get_next_unstudied_hadith(user_id: int, language: str, mode: str):
    """
    Получает следующий хадис. Теперь возвращает и audio_type.
    """
    async with db_pool.acquire() as conn:
        selected_book = await get_user_selected_book(user_id)
        
        # Выбираем все поля (h.*), включая audio_type
        base_query = """
            FROM hadiths h
            LEFT JOIN progress p ON h.id = p.hadith_id AND p.user_id = $1
            WHERE h.language = $2 
              AND h.book = $3 
              AND p.hadith_id IS NULL
        """
        
        if mode == 'random':
            count_query = f"SELECT COUNT(*) {base_query}"
            count = await conn.fetchval(count_query, user_id, language, selected_book)
            if count == 0:
                return None
            random_offset = random.randint(0, count - 1)
            fetch_query = f"""
                SELECT h.* {base_query} ORDER BY h.id ASC LIMIT 1 OFFSET $4
            """
            hadith = await conn.fetchrow(fetch_query, user_id, language, selected_book, random_offset)
        else:
            fetch_query = f"""
                SELECT h.* {base_query} ORDER BY h.id ASC LIMIT 1
            """
            hadith = await conn.fetchrow(fetch_query, user_id, language, selected_book)
            
        return dict(hadith) if hadith else None

async def iterate_users(batch_size: int = 1000):
    last_user_id = -1
    while True:
        async with db_pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT user_id, blocked, language 
                FROM users 
                WHERE user_id > $1
                ORDER BY user_id ASC 
                LIMIT $2
                """,
                last_user_id, batch_size
            )
            
        if not records:
            break
            
        for record in records:
            user_data = dict(record)
            user_data['user_id'] = str(user_data['user_id']) 
            last_user_id = record['user_id']
            yield user_data

# --- END OF FILE database.py ---