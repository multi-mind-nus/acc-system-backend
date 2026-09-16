from fastapi.testclient import TestClient

from app import db
from app.main import app


client = TestClient(app)


def test_live() -> None:
    response = client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "nginx-request-123"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "nginx-request-123"


def test_rejects_invalid_request_id() -> None:
    invalid_request_id = "x" * 65

    response = client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": invalid_request_id},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != invalid_request_id


def test_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        db,
        "dependency_status",
        lambda: {"postgres": "ok", "redis": "ok"},
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"] == {"postgres": "ok", "redis": "ok"}


def test_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        db,
        "dependency_status",
        lambda: {"postgres": "ok", "redis": "error"},
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_NOT_READY"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
