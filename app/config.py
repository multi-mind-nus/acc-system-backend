from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "acc-system-backend"
    app_version: str = "dev"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://acc:acc@localhost:5432/acc"
    redis_url: str = "redis://localhost:6379/0"
    worker_poll_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
