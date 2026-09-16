import logging
import signal
from threading import Event

from app.config import settings
from app.db import dependency_status
from app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)
stop_event = Event()


def request_stop(signum, _frame) -> None:
    logger.info("Worker stop requested by signal %s", signum)
    stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Worker started")

    while not stop_event.is_set():
        status = dependency_status()
        log = logger.info if all(value == "ok" for value in status.values()) else logger.error
        log("Worker dependency check", extra={"dependencies": status})
        stop_event.wait(settings.worker_poll_seconds)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
