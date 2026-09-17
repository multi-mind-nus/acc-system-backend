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


def test_validation_errors_do_not_echo_credentials() -> None:
    for path, payload in (
        ("login", {"email": "admin@example.com", "password": "secret-password-" * 10}),
        ("password-reset/confirm", {"token": "secret-reset-token", "new_password": "short-secret"}),
    ):
        response = client.post(f"/api/v1/auth/{path}", json=payload)
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["details"]
        assert all("input" not in error for error in body["details"])
        for field in ("password", "token", "new_password"):
            if field in payload:
                assert payload[field] not in response.text
        assert body["request_id"] == response.headers["X-Request-ID"]
