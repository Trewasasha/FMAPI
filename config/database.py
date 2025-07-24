from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from .settings import settings

# Базовый класс для моделей
Base = declarative_base()

# Движок подключения
engine = create_async_engine(settings.DATABASE_URL)

# Фабрика сессий
async_session = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db():
    async with async_session() as session:
        yield session