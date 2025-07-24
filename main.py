from fastapi import FastAPI
from contextlib import asynccontextmanager
from pathlib import Path
import logging
from redis.asyncio import Redis
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from datetime import timedelta
from fastapi_cache.decorator import cache

from config.settings import settings
from config.database import engine, Base
from auth.router import router as auth_router
from files.router import router as files_router

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Обработчик событий жизненного цикла приложения"""
    # Startup логика
    try:
        # Создаем папку для файлов
        storage_path = Path(settings.STORAGE_DIR)
        storage_path.mkdir(exist_ok=True, parents=True)
        logger.info(f"Storage directory ready at: {storage_path.absolute()}")

        # Инициализация БД
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created successfully")

        # Инициализация кеша Valkey (Redis-совместимый)
        redis = Redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5
        )
        
        try:
            if await redis.ping():
                logger.info("Successfully connected to Valkey server")
                FastAPICache.init(RedisBackend(redis), prefix="fastapi-cache")
        except Exception as e:
            logger.error(f"Failed to connect to Valkey: {str(e)}")
            raise

        yield  # Приложение работает

        # Shutdown логика
        await redis.close()
        
    except Exception as e:
        logger.error(f"Startup error: {str(e)}")
        raise

app = FastAPI(
    title="File Manager API",
    version="1.0.0",
    lifespan=lifespan
)

# Подключение роутеров
app.include_router(auth_router)
app.include_router(files_router)

# # Тестовый эндпоинт для проверки кеша
# @app.get("/test-cache")
# @cache(expire=timedelta(minutes=1))
# async def test_cache():
#     from datetime import datetime
#     return {"timestamp": datetime.now().isoformat(), "message": "This response is cached for 1 minute"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL
    )