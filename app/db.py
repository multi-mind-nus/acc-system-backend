import logging
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

metadata = MetaData()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


def dependency_status() -> dict[str, str]:
    status: dict[str, str] = {}

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        status["postgres"] = "ok"
    except SQLAlchemyError:
        logger.warning("PostgreSQL readiness check failed", exc_info=True)
        status["postgres"] = "error"

    try:
        redis_client.ping()
        status["redis"] = "ok"
    except RedisError:
        logger.warning("Redis readiness check failed", exc_info=True)
        status["redis"] = "error"

    return status
