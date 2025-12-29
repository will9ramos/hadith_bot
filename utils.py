# --- START OF FILE utils.py ---

import asyncio
import logging
import os
import time
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import aiofiles
import redis.asyncio as redis

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile, FSInputFile, 
    InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
)
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
import database
from config import AUDIO_GROUP_ID, UPLOAD_DIR, REDIS_HOST, REDIS_PORT
from translations import TRANSLATIONS

logger = logging.getLogger(__name__)

# Настройки Redis
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
BROADCAST_QUEUE_KEY = "hadith_broadcast_queue"

redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client

def ensure_upload_dir_exists():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- Функции для обработки и загрузки аудио ---

async def convert_audio_to_ogg(input_path: str) -> str | None:
    """Конвертирует аудио в OGG Opus для голосовых сообщений."""
    temp_output_path = None
    try:
        file_name = os.path.basename(input_path)
        name_without_ext = os.path.splitext(file_name)[0]
        temp_output_path = str(UPLOAD_DIR / f"{name_without_ext}_{int(time.time())}.ogg")

        cmd = [
            'ffmpeg', '-i', input_path,
            '-c:a', 'libopus', '-b:a', '32k',
            '-vbr', 'on', '-y', temp_output_path
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        
        try:
            await asyncio.wait_for(process.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            process.kill()
            return None

        if process.returncode == 0 and os.path.exists(temp_output_path):
            return temp_output_path
        return None
    except Exception as e:
        logger.error(f"Ошибка конвертации: {e}")
        return None

async def upload_audio_to_telegram(bot: Bot, file_path: str, original_filename: str) -> tuple[str, str] | None:
    """
    Загружает файл и возвращает (file_id, type).
    Type может быть 'audio' или 'voice'.
    """
    path_to_delete = None
    try:
        # Если файл маленький (< 1MB) и это OGG, пробуем как Voice для красоты
        file_size = os.path.getsize(file_path)
        
        if original_filename.lower().endswith('.ogg') and file_size < 1024 * 1024:
            try:
                msg = await bot.send_voice(AUDIO_GROUP_ID, FSInputFile(file_path))
                return msg.voice.file_id, "voice"
            except Exception: pass

        # В остальных случаях как Audio
        audio_file = FSInputFile(file_path, filename=original_filename)
        msg = await bot.send_audio(AUDIO_GROUP_ID, audio=audio_file)
        return msg.audio.file_id, "audio"

    except Exception as e:
        logger.error(f"Критическая ошибка загрузки в TG: {e}")
        return None
    finally:
        if path_to_delete and os.path.exists(path_to_delete):
            os.unlink(path_to_delete)

async def process_hadith_audio(bot: Bot, hadith_id: int, uploaded_file) -> bool:
    """Обрабатывает загрузку аудио из веб-панели."""
    temp_path = None
    try:
        ext = Path(uploaded_file.filename).suffix
        temp_path = UPLOAD_DIR / f"web_upload_{hadith_id}_{uuid.uuid4()}{ext}"
        
        async with aiofiles.open(temp_path, 'wb') as out_file:
            while content := await uploaded_file.read(1024 * 1024):
                await out_file.write(content)
            
        result = await upload_audio_to_telegram(bot, str(temp_path), uploaded_file.filename)

        if result:
            file_id, file_type = result
            await database.update_hadith_audio(hadith_id, file_id, file_type)
            return True
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

# --- НОВАЯ ЛОГИКА РАССЫЛКИ (REDIS WORKER) ---

async def schedule_broadcast(task_data: dict):
    r = await get_redis()
    await r.rpush(BROADCAST_QUEUE_KEY, json.dumps(task_data))

async def broadcast_worker(bot: Bot):
    """Слушает очередь и выполняет рассылку с учетом всех лимитов API."""
    logger.info("Broadcast Worker запущен.")
    r = await get_redis()
    
    while True:
        try:
            _, task_json = await r.blpop(BROADCAST_QUEUE_KEY, timeout=0)
            task = json.loads(task_json)
            await _execute_broadcast(bot, task)
        except Exception as e:
            logger.error(f"Ошибка воркера рассылки: {e}")
            await asyncio.sleep(5)

async def _execute_broadcast(bot: Bot, task: dict):
    successful_sends = 0
    failed_sends = 0
    
    msg_type = task.get('type') # 'native', 'copy', 'forward'
    message_text = task.get('text', '')
    
    # Подготовка медиа для Native
    cached_media_group = None
    file_id = task.get('file_id')
    file_type = task.get('file_type')
    media_paths = task.get('media_paths', [])

    # === ИСПРАВЛЕНИЕ: Предзагрузка для Native (Альбомы И Одиночные файлы) ===
    if msg_type == 'native' and media_paths:
        
        # Сценарий 1: Альбом (> 1 файла)
        if len(media_paths) > 1:
            if len(media_paths) > 10: media_paths = media_paths[:10] # Ошибка №3
            
            temp_media = []
            for i, path in enumerate(media_paths):
                caption = message_text if i == 0 else None
                # Ошибка №13: явно задаем parse_mode для каждого элемента
                if any(path.lower().endswith(e) for e in ['.jpg', '.jpeg', '.png']):
                    temp_media.append(InputMediaPhoto(media=FSInputFile(path), caption=caption, parse_mode="HTML"))
                else:
                    temp_media.append(InputMediaVideo(media=FSInputFile(path), caption=caption, parse_mode="HTML"))
            
            try:
                # Отправляем в технический чат один раз, чтобы закэшировать ID
                msgs = await bot.send_media_group(AUDIO_GROUP_ID, media=temp_media)
                cached_media_group = []
                for i, m in enumerate(msgs):
                    cap = message_text if i == 0 else None
                    if m.photo: cached_media_group.append(InputMediaPhoto(media=m.photo[-1].file_id, caption=cap, parse_mode="HTML"))
                    elif m.video: cached_media_group.append(InputMediaVideo(media=m.video.file_id, caption=cap, parse_mode="HTML"))
            except Exception as e:
                logger.error(f"Ошибка предзагрузки альбома: {e}")
                return

        # Сценарий 2: Одиночный файл (ИСПРАВЛЕНИЕ БАГА)
        elif len(media_paths) == 1 and not file_id:
            path = media_paths[0]
            try:
                # Загружаем файл в тех. чат, чтобы получить ID
                if any(path.lower().endswith(e) for e in ['.jpg', '.jpeg', '.png']):
                    msg = await bot.send_photo(AUDIO_GROUP_ID, photo=FSInputFile(path))
                    file_id = msg.photo[-1].file_id
                    file_type = "photo"
                else:
                    msg = await bot.send_video(AUDIO_GROUP_ID, video=FSInputFile(path))
                    file_id = msg.video.file_id
                    file_type = "video"
            except Exception as e:
                logger.error(f"Ошибка предзагрузки одиночного файла: {e}")
                # Fallback: Если не удалось загрузить, попробуем отправить только текст в цикле
    # ========================================================================

    # МАССОВАЯ РАССЫЛКА
    async for user_data in database.iterate_users():
        user_id = int(user_data['user_id'])
        if user_data.get('blocked'): continue

        try:
            # Ошибка №2: Используем copy_messages для альбомов
            if msg_type == 'copy':
                msg_ids = task.get('message_ids', [])
                from_chat = task.get('from_chat_id')
                if len(msg_ids) > 1:
                    # copy_messages сохраняет группировку и лимиты
                    await bot.copy_messages(chat_id=user_id, from_chat_id=from_chat, message_ids=msg_ids)
                else:
                    await bot.copy_message(chat_id=user_id, from_chat_id=from_chat, message_id=msg_ids[0])

            elif msg_type == 'forward':
                for mid in task.get('message_ids', []):
                    await bot.forward_message(chat_id=user_id, from_chat_id=task.get('from_chat_id'), message_id=mid)

            elif msg_type == 'native':
                if cached_media_group:
                    await bot.send_media_group(user_id, media=cached_media_group)
                elif file_id:
                    # Ошибка №1: Проверка длины подписи (1024)
                    if len(message_text) > 1024:
                        if file_type == "photo": await bot.send_photo(user_id, photo=file_id)
                        elif file_type == "video": await bot.send_video(user_id, video=file_id)
                        elif file_type == "audio": await bot.send_audio(user_id, audio=file_id)
                        await bot.send_message(user_id, message_text)
                    else:
                        if file_type == "photo": await bot.send_photo(user_id, photo=file_id, caption=message_text)
                        elif file_type == "video": await bot.send_video(user_id, video=file_id, caption=message_text)
                        elif file_type == "audio": await bot.send_audio(user_id, audio=file_id, caption=message_text)
                else:
                    await bot.send_message(user_id, message_text)

            successful_sends += 1
            # Ошибка №6: Лимит 1 сек на один чат. 
            # Для надежности при массовой рассылке держим 0.05с (20 сообщений/сек всего)
            await asyncio.sleep(0.05) 

        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            await database.set_user_blocked(user_id, True)
        except Exception:
            failed_sends += 1

    # Удаление временных файлов
    for path in media_paths:
        try: 
            if os.path.exists(path): os.remove(path)
        except: pass
    
    logger.info(f"Рассылка завершена. Успешно: {successful_sends}")

# --- Обертки для совместимости ---

async def send_broadcast_message(bot: Bot, message_text: str, media_paths: list = None, file_id: str = None, file_type: str = None):
    # Проверка лимита 10МБ для фото (Ошибка №9)
    if media_paths and not file_id:
        for p in media_paths:
            if any(p.lower().endswith(ex) for ex in ['.jpg', '.png', '.jpeg']):
                if os.path.getsize(p) > 10 * 1024 * 1024:
                    logger.error(f"Фото {p} слишком большое (>10MB)")
                    return 0, 1

    await schedule_broadcast({
        'type': 'native', 'text': message_text, 'media_paths': media_paths,
        'file_id': file_id, 'file_type': file_type
    })
    return 0, 0

async def send_personal_message(bot: Bot, user_id: str, text: str):
    try:
        await bot.send_message(int(user_id), text)
        return True
    except: return False

async def perform_user_scan(bot: Bot):
    """Сканирование. Теперь без typing, чтобы не шуметь (Ошибка №12)."""
    blocked_count = 0
    async for user_data in database.iterate_users():
        if user_data.get('blocked'): continue
        try:
            # Используем максимально легкий запрос
            await bot.get_chat(int(user_data['user_id']))
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            await database.set_user_blocked(int(user_data['user_id']), True)
            blocked_count += 1
        except Exception: pass
    return blocked_count

async def send_hadiths_periodically(bot: Bot):
    while True:
        try:
            await asyncio.sleep(60 * 5)
            current_timestamp = time.time()
            async with database.db_pool.acquire() as conn:
                users_to_remind = await conn.fetch("""
                    SELECT user_id, language, hadith_mode, selected_book, reminder_time, reminder_frequency, last_reminder_sent_at
                    FROM users
                    WHERE blocked = 0 AND reminders_enabled = TRUE
                    AND (extract(epoch from now()) - last_reminder_sent_at) >= (reminder_frequency * 3600)
                """)

                for user_data in users_to_remind:
                    user_id = user_data['user_id']
                    
                    # Проверка часа для частот >= 24ч
                    if (user_data['reminder_frequency'] or 12) >= 24:
                        try:
                            r_hour = int(user_data['reminder_time'].split(':')[0])
                            if datetime.now(ZoneInfo("Asia/Tashkent")).hour != r_hour: continue
                        except: pass

                    hadith = await database.get_next_unstudied_hadith(user_id, user_data['language'], user_data['hadith_mode'])
                    if not hadith:
                        await conn.execute("UPDATE users SET last_reminder_sent_at = $1 WHERE user_id = $2", current_timestamp, user_id)
                        continue

                    # Сборка сообщения
                    from keyboards import HadithAction, InlineKeyboardMarkup, InlineKeyboardButton
                    inline_kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text=TRANSLATIONS[user_data['language']]['hadith_studied'],
                            callback_data=HadithAction(action="studied", hadith_id=hadith['id'], language=user_data['language']).pack()
                        )
                    ]])
                    
                    text_to_send = TRANSLATIONS[user_data['language']]['periodic_prompt'].format(text=hadith['text'])
                    
                    try:
                        if hadith.get('audio_file_id'):
                            a_id = hadith['audio_file_id']
                            a_type = hadith.get('audio_type', 'audio') # Ошибка №8
                            
                            # Ошибка №1 и №14: Лимит подписи 1024
                            if len(text_to_send) > 1024:
                                if a_type == 'voice': await bot.send_voice(user_id, a_id)
                                else: await bot.send_audio(user_id, a_id)
                                await bot.send_message(user_id, text_to_send, reply_markup=inline_kb)
                            else:
                                if a_type == 'voice': await bot.send_voice(user_id, a_id, caption=text_to_send, reply_markup=inline_kb)
                                else: await bot.send_audio(user_id, a_id, caption=text_to_send, reply_markup=inline_kb)
                        else:
                            await bot.send_message(user_id, text_to_send, reply_markup=inline_kb)
                            
                        await conn.execute("UPDATE users SET last_reminder_sent_at = $1 WHERE user_id = $2", current_timestamp, user_id)
                    except TelegramForbiddenError:
                        await database.set_user_blocked(user_id, True)
                    except Exception as e:
                        logger.error(f"Ошибка отправки напоминания: {e}")
                    
                    await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Ошибка в цикле напоминаний: {e}")
            await asyncio.sleep(60)

async def scan_blocked_users_periodically(bot: Bot):
    """
    Фоновая задача, которая раз в 24 часа проверяет базу пользователей
    на предмет тех, кто заблокировал бота.
    """
    while True:
        try:
            # Ждем 24 часа (86400 секунд)
            await asyncio.sleep(86400)
            
            logger.info("Начало планового сканирования заблокированных пользователей...")
            blocked_count = await perform_user_scan(bot)
            logger.info(f"Сканирование завершено. Выявлено заблокированных: {blocked_count}")
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле сканирования: {e}")
            await asyncio.sleep(300) # Пауза 5 минут при ошибке перед повтором

# --- END OF FILE utils.py ---