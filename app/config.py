from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "acc-system-backend"
    app_version: str = "dev"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://acc:acc@localhost:5432/acc"
    redis_url: str = "redis://localhost:6379/0"
    worker_poll_seconds: float = 10.0
    max_upload_bytes: int = 25 * 1024 * 1024
    quarantine_path: str = "/data/quarantine"
    document_path: str = "/data/documents"
    clamav_host: str | None = None
    clamav_port: int = 3310
    agent_url: str = "http://agent:8000"
    agent_classification_provider: Literal["DISABLED", "MOCK", "REMOTE"] = "DISABLED"
    jwt_secret: SecretStr = SecretStr("local-development-only-change-me")
    jwt_issuer: str = "acc-system"
    jwt_audience: str = "acc-system-web"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cookie_secure: bool = False
    frontend_origin: str = "http://localhost"
    login_rate_limit: int = 10
    token_rate_limit: int = 10
    rate_limit_window_seconds: int = 60

    @model_validator(mode="after")
    def require_production_secrets(self):
        if (
            self.environment == "production"
            and self.jwt_secret.get_secret_value() == "local-development-only-change-me"
        ):
            raise ValueError("JWT_SECRET must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
