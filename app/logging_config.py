import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    _extra_fields = (
        "request_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "dependencies",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            (field, getattr(record, field))
            for field in self._extra_fields
            if hasattr(record, field)
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
