# --- START OF FILE hadith_bot.py ---

import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
import uvicorn

# Импортируем компоненты из наших модулей
import config
from config import REDIS_HOST, REDIS_PORT 
import database
from web_app import app
from bot_handlers import router as main_router

# ИСПРАВЛЕНИЕ: Добавляем broadcast_worker в список импорта
from utils import (
    send_hadiths_periodically,
    scan_blocked_users_periodically,
    ensure_upload_dir_exists,
    broadcast_worker
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Основная асинхронная функция для запуска всего ---
async def main():
    """
    Главная функция, которая инициализирует и запускает все компоненты.
    """
    # 1. Создаем объекты бота и хранилища FSM
    # ИСПРАВЛЕНИЕ: Используем правильные переменные из config
    # Это позволяет Docker подставить 'redis', а локально использовать 'localhost'
    storage = RedisStorage.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}/0")
    
    bot = Bot(
        token=config.TOKEN,
        default=DefaultBotProperties(parse_mode="HTML")
    )
    
    # 2. Создаем диспетчер
    dp = Dispatcher(storage=storage)

    # Включаем роутер с обработчиками сообщений
    dp.include_router(main_router)

    # 3. Убедимся, что папка для загрузок существует
    ensure_upload_dir_exists()

    # 4. Инициализируем соединение с базой данных
    await database.init_db()

    # 5. Передаем экземпляр бота в веб-приложение
    app.state.bot = bot

    # 6. Запускаем фоновые задачи
    asyncio.create_task(send_hadiths_periodically(bot))
    asyncio.create_task(scan_blocked_users_periodically(bot))
    
    # ИСПРАВЛЕНИЕ: Запускаем воркер рассылки (вызываем функцию напрямую)
    asyncio.create_task(broadcast_worker(bot))

    # 7. Настраиваем и готовим к запуску веб-сервер (админ-панель)
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(server_config)

    # Запускаем веб-сервер в виде фоновой задачи
    server_task = asyncio.create_task(server.serve())

    logger.info("Бот запущен! Веб-панель доступна по адресу: http://<ваш_ip>:8000")

    try:
        # 8. Запускаем бота
        # Удаляем любые вебхуки перед запуском в режиме поллинга
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Остановка бота и сервисов...")

        # Корректно останавливаем веб-сервер
        if not server_task.done():
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                logger.info("Веб-сервер остановлен.")

        # Закрываем пул соединений с базой данных
        if database.db_pool:
            await database.db_pool.close()
            logger.info("Соединение с базой данных закрыто.")
        
        # Закрываем хранилище Redis
        await storage.close()
        logger.info("Соединение с Redis закрыто.")

        # Закрываем сессию бота
        await bot.session.close()
        logger.info("Сессия бота закрыта.")


# --- Точка входа в программу ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")

# --- END OF FILE hadith_bot.py ---