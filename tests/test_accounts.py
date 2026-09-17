from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.auth import decode_token, hash_password, hash_token
from app.config import settings
from app.db import SessionLocal, redis_client
from app.main import app
from app.models import Base, Client, Firm, FirmMember, PasswordResetToken, User, UserInvite


@pytest.fixture(autouse=True)
def clean_state():
    # These tests delete every table and flush Redis. Refuse the deployment defaults.
    if (
        settings.environment != "test"
        or make_url(settings.database_url).database != "acc_test"
        or redis_client.connection_pool.connection_kwargs.get("db") != 15
    ):
        pytest.fail("Account tests require ENVIRONMENT=test, database acc_test and Redis DB 15")
    with SessionLocal.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
    redis_client.flushdb()
    yield
    redis_client.flushdb()


@pytest.fixture
def admin() -> User:
    with SessionLocal.begin() as db:
        firm = Firm(name="Test Accounting")
        db.add(firm)
        db.flush()
        user = User(
            firm_id=firm.id,
            email="admin@example.com",
            name="Admin",
            password_hash=hash_password("correct horse battery staple"),
        )
        db.add(user)
        db.flush()
        db.add(FirmMember(firm_id=firm.id, user_id=user.id, role="FIRM_ADMIN"))
    return user


def login(client: TestClient, email: str, password: str) -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def bearer(auth: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def test_refresh_rotation_replay_and_logout(admin: User) -> None:
    with TestClient(app) as client:
        auth = login(client, admin.email, "correct horse battery staple")
        first_refresh = client.cookies.get("refresh_token")
        payload = decode_token(auth["access_token"], "access")
        payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
        expired_access = jwt.encode(
            payload, settings.jwt_secret.get_secret_value(), algorithm="HS256"
        )
        assert client.get(
            "/api/v1/me", headers=bearer({"access_token": expired_access})
        ).status_code == 401
        # A sliding refresh lifetime must also renew the all-sessions index.
        index_key = f"auth:user:{admin.id}:sessions"
        redis_client.expire(index_key, 1)
        response = client.post(
            "/api/v1/auth/refresh", headers={"Origin": "http://localhost"}
        )
        assert response.status_code == 200
        next_auth = response.json()
        assert client.cookies.get("refresh_token") != first_refresh
        assert redis_client.ttl(index_key) > 60

        with TestClient(app) as replay:
            replay.cookies.set("refresh_token", first_refresh)
            response = replay.post(
                "/api/v1/auth/refresh", headers={"Origin": "http://localhost"}
            )
            assert response.status_code == 401
            assert response.json()["code"] == "REFRESH_REUSED"

        assert client.get("/api/v1/me", headers=bearer(next_auth)).status_code == 401

        auth = login(client, admin.email, "correct horse battery staple")
        logout_refresh = client.cookies.get("refresh_token")
        assert client.post(
            "/api/v1/auth/logout", headers={"Origin": "http://localhost"}
        ).status_code == 200
        assert client.get("/api/v1/me", headers=bearer(auth)).status_code == 401
        client.cookies.set("refresh_token", logout_refresh)
        assert client.post("/api/v1/auth/refresh").status_code == 401


def test_invitations_roles_assignments_and_tenant_boundary(admin: User) -> None:
    with TestClient(app) as client:
        admin_auth = login(client, admin.email, "correct horse battery staple")
        admin_headers = bearer(admin_auth)
        response = client.post(
            "/api/v1/clients",
            headers=admin_headers,
            json={"code": "ACME", "legalName": "ACME Pte Ltd"},
        )
        # The API boundary is snake_case; camelCase conversion is a frontend concern.
        assert response.status_code == 422
        response = client.post(
            "/api/v1/clients",
            headers=admin_headers,
            json={"code": "ACME", "legal_name": "ACME Pte Ltd"},
        )
        assert response.status_code == 201, response.text
        client_id = response.json()["id"]

        invite = client.post(
            "/api/v1/users/invitations",
            headers=admin_headers,
            json={"email": "accountant@example.com", "role": "ACCOUNTANT"},
        ).json()
        assert client.post(
            "/api/v1/auth/invitations/accept",
            json={
                "token": invite["token"],
                "name": "Accountant",
                "password": "accountant-password",
            },
        ).status_code == 200
        accountant_auth = login(
            client, "accountant@example.com", "accountant-password"
        )
        accountant_id = accountant_auth["user"]["id"]
        assert client.get(
            f"/api/v1/clients/{client_id}", headers=bearer(accountant_auth)
        ).status_code == 404
        assert client.get(
            "/api/v1/clients", headers=bearer(accountant_auth)
        ).json()["items"] == []
        assert client.patch(
            f"/api/v1/clients/{client_id}", headers=bearer(accountant_auth),
            json={"legal_name": "Unauthorized edit"},
        ).status_code == 403
        accountant_sessions = [(accountant_auth, client.cookies.get("refresh_token"))]
        second_auth = login(client, "accountant@example.com", "accountant-password")
        accountant_sessions.append((second_auth, client.cookies.get("refresh_token")))
        assert client.put(
            f"/api/v1/clients/{client_id}/assignments",
            headers=admin_headers,
            json={"user_ids": [accountant_id]},
        ).status_code == 200
        assert client.get(
            f"/api/v1/clients/{client_id}", headers=bearer(accountant_auth)
        ).status_code == 200
        assert client.post(
            f"/api/v1/clients/{client_id}/invitations",
            headers=bearer(accountant_auth),
            json={"email": "forbidden@example.com", "role": "CLIENT_SUBMITTER"},
        ).status_code == 404
        assert client.patch(
            f"/api/v1/users/{accountant_id}",
            headers=admin_headers,
            json={"status": "DISABLED"},
        ).status_code == 200
        for session_auth, refresh_token in accountant_sessions:
            assert client.get(
                "/api/v1/me", headers=bearer(session_auth)
            ).status_code == 401
            assert client.post(
                "/api/v1/auth/refresh",
                headers={"Cookie": f"refresh_token={refresh_token}"},
            ).status_code == 401

        client_invite = client.post(
            f"/api/v1/clients/{client_id}/invitations",
            headers=admin_headers,
            json={"email": "client@example.com", "role": "CLIENT_ADMIN"},
        ).json()
        assert client.post(
            "/api/v1/auth/invitations/accept",
            json={
                "token": client_invite["token"],
                "name": "Client Admin",
                "password": "client-admin-password",
            },
        ).status_code == 200
        client_auth = login(client, "client@example.com", "client-admin-password")
        submitter_invite = client.post(
            f"/api/v1/clients/{client_id}/invitations",
            headers=bearer(client_auth),
            json={"email": "submitter@example.com", "role": "CLIENT_SUBMITTER"},
        )
        assert submitter_invite.status_code == 201
        assert client.post(
            "/api/v1/auth/invitations/accept",
            json={"token": submitter_invite.json()["token"], "name": "Submitter",
                  "password": "submitter-password"},
        ).status_code == 200
        submitter_auth = login(client, "submitter@example.com", "submitter-password")
        assert client.get(
            f"/api/v1/clients/{client_id}", headers=bearer(submitter_auth)
        ).status_code == 200
        assert client.post(
            f"/api/v1/clients/{client_id}/invitations", headers=bearer(submitter_auth),
            json={"email": "forbidden@example.com", "role": "CLIENT_ADMIN"},
        ).status_code == 404
        assert client.get("/api/v1/users", headers=bearer(submitter_auth)).status_code == 403
        same_firm_client = client.post(
            "/api/v1/clients", headers=admin_headers,
            json={"code": "SECOND", "legal_name": "Another Client"},
        ).json()["id"]
        assert client.get(
            f"/api/v1/clients/{same_firm_client}", headers=bearer(submitter_auth)
        ).status_code == 404

        with SessionLocal.begin() as db:
            other_firm = Firm(name="Other Firm")
            db.add(other_firm)
            db.flush()
            other_client = Client(
                firm_id=other_firm.id,
                code="OTHER",
                legal_name="Other Client",
                features={},
            )
            db.add(other_client)
            db.flush()
            other_client_id = other_client.id
        assert client.get(
            f"/api/v1/clients/{other_client_id}", headers=admin_headers
        ).status_code == 404
        assert client.patch(
            f"/api/v1/clients/{other_client_id}", headers=admin_headers,
            json={"legal_name": "Cross-tenant edit"},
        ).status_code == 404
        assert client.put(
            f"/api/v1/clients/{other_client_id}/assignments", headers=admin_headers,
            json={"user_ids": []},
        ).status_code == 404
        assert client.post(
            f"/api/v1/clients/{other_client_id}/invitations", headers=admin_headers,
            json={"email": "forbidden@example.com", "role": "CLIENT_ADMIN"},
        ).status_code == 404
        with SessionLocal() as db:
            assert db.get(Client, other_client_id).legal_name == "Other Client"


def test_password_change_revokes_every_session(admin: User) -> None:
    with TestClient(app) as client:
        admin_auth = login(client, admin.email, "correct horse battery staple")
        sessions = [(admin_auth, client.cookies.get("refresh_token"))]
        other_auth = login(client, admin.email, "correct horse battery staple")
        sessions.append((other_auth, client.cookies.get("refresh_token")))
        assert client.patch(
            "/api/v1/me/password",
            headers=bearer(admin_auth),
            json={
                "current_password": "correct horse battery staple",
                "new_password": "new correct horse battery staple",
            },
        ).status_code == 200
        for session_auth, refresh_token in sessions:
            assert client.get("/api/v1/me", headers=bearer(session_auth)).status_code == 401
            assert client.post(
                "/api/v1/auth/refresh",
                headers={"Cookie": f"refresh_token={refresh_token}"},
            ).status_code == 401
        assert client.post(
            "/api/v1/auth/login",
            json={"email": admin.email, "password": "correct horse battery staple"},
        ).status_code == 401
        login(client, admin.email, "new correct horse battery staple")


def test_invitation_single_use_expiry_and_replacement(admin: User) -> None:
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        for state in ("accepted", "expired", "replaced"):
            invitation = {"email": f"{state}@example.com", "role": "ACCOUNTANT"}
            invite = client.post(
                "/api/v1/users/invitations", headers=headers, json=invitation,
            ).json()
            payload = {"token": invite["token"], "name": "Accountant",
                       "password": "accountant-password"}
            if state == "accepted":
                assert client.post(
                    "/api/v1/auth/invitations/accept", json=payload,
                ).status_code == 200
            elif state == "expired":
                with SessionLocal.begin() as db:
                    db.get(UserInvite, UUID(invite["id"])).expires_at = (
                        datetime.now(UTC) - timedelta(seconds=1)
                    )
            else:
                assert client.post(
                    "/api/v1/users/invitations", headers=headers, json=invitation,
                ).status_code == 201
            response = client.post("/api/v1/auth/invitations/accept", json=payload)
            assert response.status_code == 400
            assert response.json()["code"] == "INVITATION_INVALID"


def test_reset_single_use_expiry_and_session_revocation(admin: User) -> None:
    with TestClient(app) as client:
        sessions = []
        for _ in range(2):
            auth = login(client, admin.email, "correct horse battery staple")
            sessions.append((auth, client.cookies.get("refresh_token")))
        tokens = [client.post(
            "/api/v1/auth/password-reset/request", json={"email": admin.email},
        ).json()["development_token"] for _ in range(3)]
        with SessionLocal.begin() as db:
            token = db.scalar(select(PasswordResetToken).where(
                PasswordResetToken.token_hash == hash_token(tokens[-1])
            ))
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        for token in tokens:
            rejected = client.post("/api/v1/auth/password-reset/confirm", json={
                "token": token, "new_password": "reset-password-new",
            })
            assert rejected.status_code == 400
            assert rejected.json()["code"] == "RESET_INVALID"
        valid_token = client.post(
            "/api/v1/auth/password-reset/request", json={"email": admin.email},
        ).json()["development_token"]
        payload = {"token": valid_token, "new_password": "reset-password-new"}
        assert client.post("/api/v1/auth/password-reset/confirm", json=payload).status_code == 200
        assert client.post("/api/v1/auth/password-reset/confirm", json=payload).status_code == 400
        for session_auth, refresh_token in sessions:
            assert client.get("/api/v1/me", headers=bearer(session_auth)).status_code == 401
            assert client.post(
                "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={refresh_token}"},
            ).status_code == 401
        assert client.post("/api/v1/auth/login", json={
            "email": admin.email, "password": "correct horse battery staple",
        }).status_code == 401
        login(client, admin.email, "reset-password-new")


def test_production_reset_does_not_reveal_account_existence(admin: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    with TestClient(app) as client:
        known = client.post("/api/v1/auth/password-reset/request", json={"email": admin.email})
        unknown = client.post("/api/v1/auth/password-reset/request", json={"email": "missing@example.com"})
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()
        assert known.json()["development_token"] is None


@pytest.mark.parametrize(("path", "payload"), [
    ("login", {"email": "missing@example.com", "password": "wrong-password"}),
    ("invitations/accept", {"token": "x" * 32, "name": "Name", "password": "accountant-password"}),
    ("password-reset/request", {"email": "missing@example.com"}),
    ("password-reset/confirm", {"token": "x" * 32, "new_password": "reset-password-new"}),
])
def test_auth_rate_limits_include_request_id(path: str, payload: dict, monkeypatch) -> None:
    monkeypatch.setattr(settings, "login_rate_limit", 2)
    monkeypatch.setattr(settings, "token_rate_limit", 2)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post(f"/api/v1/auth/{path}", json=payload).status_code != 429
        response = client.post(f"/api/v1/auth/{path}", json=payload)
        assert response.status_code == 429
        assert response.json()["code"] == "RATE_LIMITED"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_logout_does_not_report_success_when_revocation_fails(admin: User, monkeypatch) -> None:
    with TestClient(app) as client:
        login(client, admin.email, "correct horse battery staple")
        def unavailable():
            raise RedisError("Redis unavailable")
        monkeypatch.setattr(redis_client, "pipeline", unavailable)
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 503
        assert response.json()["code"] == "AUTH_UNAVAILABLE"
        assert "set-cookie" not in response.headers
