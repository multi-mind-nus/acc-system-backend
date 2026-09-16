import logging

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings

logger = logging.getLogger(__name__)

metadata = MetaData()
engine = create_engine(settings.database_url, pool_pre_ping=True)
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


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
