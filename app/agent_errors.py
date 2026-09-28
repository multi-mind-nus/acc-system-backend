import json
from urllib.error import HTTPError


def agent_http_error(exc: HTTPError) -> str:
    # The agent already retries malformed model output; another worker retry
    # repeats the same validation failure rather than recovering an outage.
    try:
        payload = json.loads(exc.read(64 * 1024))
        if isinstance(payload, dict) and payload.get("code") == "MODEL_INVALID_RESPONSE":
            return "AGENT_INVALID_RESPONSE"
    except (ValueError, OSError):
        pass
    return "AGENT_UNAVAILABLE" if exc.code >= 500 else "AGENT_INVALID_RESPONSE"
