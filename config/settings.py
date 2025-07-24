from pydantic_settings import BaseSettings
from pydantic import PostgresDsn

class Settings(BaseSettings):
    # Основные настройки приложения
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    STORAGE_DIR: str = "storage"
    LOG_LEVEL: str = "info"
    
    # Настройки аутентификации
    SECRET_KEY: str = "your-secret-key-here"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # Настройки PostgreSQL (для Docker)
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "2746"
    POSTGRES_HOST: str = "postgres"  # Изменили с localhost на имя сервиса
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = "file_storage"
    
    # Настройки Redis (для Docker)
    REDIS_URL: str = "redis://redis:6379/0"  # Изменили localhost на redis
    
    @property
    def DATABASE_URL(self) -> PostgresDsn:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

settings = Settings()