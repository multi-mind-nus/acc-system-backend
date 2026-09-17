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
from app.models import (
    Base, Client, ClientBankAccount, Firm, FirmMember,
    PasswordResetToken, User, UserInvite,
)


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


def test_f3_staff_filters_client_pagination_and_validation(admin: User) -> None:
    with SessionLocal.begin() as db:
        for number in range(3):
            staff = User(
                firm_id=admin.firm_id, email=f"staff{number}@example.com",
                name=f"Staff {number}", password_hash=admin.password_hash,
                status="DISABLED" if number == 2 else "ACTIVE",
            )
            db.add(staff)
            db.flush()
            db.add(FirmMember(
                firm_id=admin.firm_id, user_id=staff.id,
                role="FIRM_ADMIN" if number == 2 else "ACCOUNTANT",
            ))
        db.add(User(firm_id=admin.firm_id, email="contact@example.com", name="Contact",
                    password_hash=admin.password_hash))
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        staff = client.get("/api/v1/users", headers=headers, params={
            "staff_only": True, "role": "ACCOUNTANT", "status": "ACTIVE",
            "search": "STAFF", "page_size": 1,
        }).json()
        assert staff["total"] == 2 and staff["page_size"] == 1
        assert staff["items"][0]["email"] == "staff0@example.com"
        assert client.get("/api/v1/users", headers=headers, params={
            "staff_only": True, "role": "ACCOUNTANT", "status": "ACTIVE",
            "search": "STAFF", "page_size": 1, "page": 2,
        }).json()["items"][0]["email"] == "staff1@example.com"
        assert client.get("/api/v1/users", headers=headers).json()["total"] == 5
        # A disabled admin cannot replace the last active admin, but its own role
        # can still be changed without affecting the active-admin invariant.
        assert client.patch(f"/api/v1/users/{admin.id}", headers=headers, json={
            "role": "ACCOUNTANT",
        }).json()["code"] == "LAST_ADMIN_REQUIRED"
        disabled = client.get("/api/v1/users?role=FIRM_ADMIN&status=DISABLED", headers=headers).json()["items"][0]
        assert client.patch(f"/api/v1/users/{disabled['id']}", headers=headers, json={
            "role": "ACCOUNTANT",
        }).status_code == 200
        for code in ("ZULU", "ALPHA"):
            response = client.post("/api/v1/clients", headers=headers, json={
                "code": code, "legal_name": code,
            })
            assert response.status_code == 201
        page = client.get("/api/v1/clients?page_size=1", headers=headers).json()
        assert page["total"] == 2 and page["items"][0]["code"] == "ALPHA"
        client_id = page["items"][0]["id"]
        assert client.patch(f"/api/v1/clients/{client_id}", headers=headers, json={
            "status": "DISABLED", "features": {"has_loan": True},
        }).json()["features"]["has_loan"] is True
        assert client.get("/api/v1/clients?status=DISABLED&search=alpha", headers=headers).json()["total"] == 1
        duplicate = client.post("/api/v1/clients", headers=headers, json={"code": "alpha", "legal_name": "Other"})
        assert duplicate.status_code == 409 and duplicate.json()["code"] == "CLIENT_CODE_EXISTS"
        for payload in ({"legal_name": None}, {"legal_name": "   "}, {"base_currency": "sgd"}):
            invalid = client.patch(f"/api/v1/clients/{client_id}", headers=headers, json=payload)
            assert invalid.status_code == 422 and invalid.json()["request_id"]
        assert client.get("/api/v1/users?status=INVALID", headers=headers).status_code == 422
        assert client.get("/api/v1/clients?page=0", headers=headers).status_code == 422


def test_f3_banks_assignments_and_tenant_boundaries(admin: User) -> None:
    from sqlalchemy.exc import IntegrityError

    with SessionLocal.begin() as db:
        accountant = User(firm_id=admin.firm_id, email="accountant@example.com", name="Accountant",
                          password_hash=admin.password_hash)
        db.add(accountant)
        db.flush()
        db.add(FirmMember(firm_id=admin.firm_id, user_id=accountant.id, role="ACCOUNTANT"))
        other_firm = Firm(name="Other firm")
        db.add(other_firm)
        db.flush()
        foreign_client = Client(firm_id=other_firm.id, code="FOREIGN", legal_name="Foreign")
        db.add(foreign_client)
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        client_id = client.post("/api/v1/clients", headers=headers, json={
            "code": "BANKS", "legal_name": "Bank client",
        }).json()["id"]
        other_id = client.post("/api/v1/clients", headers=headers, json={
            "code": "OTHER", "legal_name": "Other client",
        }).json()["id"]
        staff_headers = bearer(login(client, accountant.email, "correct horse battery staple"))
        bank_url = f"/api/v1/clients/{client_id}/bank-accounts"
        assignment_url = f"/api/v1/clients/{client_id}/assignments"
        assert client.get(bank_url, headers=staff_headers).status_code == 404
        assert client.get(assignment_url, headers=staff_headers).status_code == 404
        assert client.put(assignment_url, headers=headers, json={"user_ids": [str(accountant.id)]}).status_code == 200
        assert client.get(assignment_url, headers=staff_headers).json() == {"user_ids": [str(accountant.id)]}
        bank_payload = {"bank": "DBS", "account_last4": "0042", "currency": "SGD"}
        bank = client.post(bank_url, headers=headers, json=bank_payload)
        assert bank.status_code == 201 and bank.json()["account_last4"] == "0042"
        bank_id = bank.json()["id"]
        assert client.get(bank_url, headers=staff_headers).json() == [bank.json()]
        assert client.post(bank_url, headers=staff_headers, json=bank_payload).status_code == 403
        assert client.patch(f"{bank_url}/{bank_id}", headers=staff_headers, json={"bank": "UOB"}).status_code == 403
        assert client.patch(f"{bank_url}/{bank_id}", headers=headers, json={"status": "DISABLED"}).json()["status"] == "DISABLED"
        for field, value in (("bank", " "), ("account_last4", "12345"), ("currency", "sgd")):
            assert client.post(bank_url, headers=headers, json={**bank_payload, field: value}).status_code == 422
        assert client.patch(f"{bank_url}/{bank_id}", headers=headers, json={"bank": None}).status_code == 422
        assert client.patch(f"/api/v1/clients/{other_id}/bank-accounts/{bank_id}", headers=headers,
                            json={"bank": "Wrong client"}).status_code == 404
        for method in ("get", "post"):
            response = getattr(client, method)(
                f"/api/v1/clients/{foreign_client.id}/bank-accounts", headers=headers,
                **({"json": bank_payload} if method == "post" else {}),
            )
            assert response.status_code == 404
        assert client.put(assignment_url, headers=headers, json={"user_ids": [str(admin.id)]}).status_code == 422
        assert client.put(assignment_url, headers=headers, json={"user_ids": []}).status_code == 200
        assert client.get(bank_url, headers=staff_headers).status_code == 404
        # Tenant isolation is also enforced by the composite foreign key.
        with pytest.raises(IntegrityError), SessionLocal.begin() as db:
            db.add(ClientBankAccount(firm_id=admin.firm_id, client_id=foreign_client.id, **bank_payload))
            db.flush()


def test_f3_invitation_management_lifecycle(admin: User) -> None:
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        url = "/api/v1/users/invitations"
        original = client.post(url, headers=headers, json={
            "email": "new-staff@example.com", "role": "ACCOUNTANT",
        }).json()
        listing = client.get(url, headers=headers).json()
        assert listing["total"] == 1 and listing["items"][0]["status"] == "PENDING"
        assert set(listing["items"][0]) == {"id", "email", "role", "expires_at", "created_at", "status"}
        resent = client.post(f"{url}/{original['id']}/resend", headers=headers)
        assert resent.status_code == 200
        assert resent.json()["id"] != original["id"]
        accept = {"name": "New staff", "password": "a sufficiently long password", "token": original["token"]}
        assert client.post("/api/v1/auth/invitations/accept", json=accept).status_code == 400
        statuses = {item["id"]: item["status"] for item in client.get(url, headers=headers).json()["items"]}
        assert statuses == {original["id"]: "REVOKED", resent.json()["id"]: "PENDING"}
        assert client.post("/api/v1/auth/invitations/accept", json={**accept, "token": resent.json()["token"]}).status_code == 200
        for action in ("revoke", "resend"):
            conflict = client.post(f"{url}/{resent.json()['id']}/{action}", headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "INVITATION_ALREADY_ACCEPTED"
        expiring = client.post(url, headers=headers, json={"email": "expired-new@example.com", "role": "ACCOUNTANT"}).json()
        with SessionLocal.begin() as db:
            db.get(UserInvite, UUID(expiring["id"])).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        statuses = {item["id"]: item["status"] for item in client.get(url, headers=headers).json()["items"]}
        assert statuses[expiring["id"]] == "EXPIRED" and statuses[resent.json()["id"]] == "ACCEPTED"
        assert client.get(f"{url}?page=2&page_size=1", headers=headers).json()["total"] == 3
        replacement = client.post(f"{url}/{expiring['id']}/resend", headers=headers).json()
        assert client.post(f"{url}/{replacement['id']}/revoke", headers=headers).status_code == 200
        assert client.post(f"{url}/{replacement['id']}/revoke", headers=headers).status_code == 200
        assert client.post("/api/v1/auth/invitations/accept", json={**accept, "token": replacement["token"]}).status_code == 400
        staff_headers = bearer(login(client, "new-staff@example.com", accept["password"]))
        assert client.get(url, headers=staff_headers).status_code == 403
        assert client.post(f"{url}/{original['id']}/resend", headers=staff_headers).status_code == 403


def test_f3_client_contact_permissions_last_admin_and_invitation_scope(admin: User) -> None:
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        client_id = client.post("/api/v1/clients", headers=headers, json={
            "code": "CONTACTS", "legal_name": "Contacts client",
        }).json()["id"]
        other_id = client.post("/api/v1/clients", headers=headers, json={
            "code": "OTHER", "legal_name": "Other client",
        }).json()["id"]
        prefix = f"/api/v1/clients/{client_id}"
        contacts = []
        for name, role in (("manager", "CLIENT_ADMIN"), ("submitter", "CLIENT_SUBMITTER")):
            invite = client.post(f"{prefix}/invitations", headers=headers, json={
                "email": f"{name}@example.com", "role": role,
            }).json()
            assert client.post("/api/v1/auth/invitations/accept", json={
                "token": invite["token"], "name": name, "password": "contact secure password",
            }).status_code == 200
            contacts.append(login(client, f"{name}@example.com", "contact secure password"))
        manager, submitter = contacts
        manager_headers, submitter_headers = bearer(manager), bearer(submitter)
        for method, suffix, kwargs in (
            ("get", "/invitations", {}),
            ("post", "/invitations", {"json": {"email": "blocked@example.com", "role": "CLIENT_ADMIN"}}),
            ("patch", f"/members/{submitter['user']['id']}", {"json": {"role": "CLIENT_ADMIN"}}),
        ):
            assert getattr(client, method)(f"{prefix}{suffix}", headers=submitter_headers, **kwargs).status_code == 404
        assert client.get(f"{prefix}/assignments", headers=manager_headers).status_code == 403
        for payload in ({"role": "CLIENT_SUBMITTER"}, {"role": "CLIENT_ADMIN", "active": False}):
            denied = client.patch(f"{prefix}/members/{manager['user']['id']}", headers=manager_headers, json=payload)
            assert denied.status_code == 409 and denied.json()["code"] == "LAST_CLIENT_ADMIN_REQUIRED"
        invite = client.post(f"{prefix}/invitations", headers=manager_headers, json={
            "email": "pending-contact@example.com", "role": "CLIENT_SUBMITTER",
        }).json()
        assert client.get(f"{prefix}/invitations", headers=manager_headers).json()["total"] == 3
        assert client.post(f"/api/v1/users/invitations/{invite['id']}/revoke", headers=headers).status_code == 404
        assert client.post(f"/api/v1/clients/{other_id}/invitations/{invite['id']}/revoke", headers=headers).status_code == 404
        assert client.post(f"{prefix}/invitations/{invite['id']}/resend", headers=submitter_headers).status_code == 404
        assert client.post(f"{prefix}/invitations/{invite['id']}/revoke", headers=manager_headers).status_code == 200
        assert client.patch(f"{prefix}/members/{submitter['user']['id']}", headers=manager_headers,
                            json={"role": "CLIENT_ADMIN"}).status_code == 200
        assert client.patch(f"{prefix}/members/{manager['user']['id']}", headers=manager_headers,
                            json={"role": "CLIENT_ADMIN", "active": False}).status_code == 200
        assert client.get(f"{prefix}/members", headers=manager_headers).status_code == 403


@pytest.mark.parametrize("disabled", ["firm", "client", "user"])
def test_f3_invitation_rejects_disabled_entities(admin: User, disabled: str) -> None:
    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        client_id = client.post("/api/v1/clients", headers=headers, json={
            "code": "DISABLED", "legal_name": "Disabled client",
        }).json()["id"]
        invite = client.post(f"/api/v1/clients/{client_id}/invitations", headers=headers, json={
            "email": "disabled@example.com", "role": "CLIENT_ADMIN",
        }).json()
        with SessionLocal.begin() as db:
            if disabled == "firm":
                db.get(Firm, admin.firm_id).status = "DISABLED"
            elif disabled == "client":
                db.get(Client, UUID(client_id)).status = "DISABLED"
            else:
                db.add(User(firm_id=admin.firm_id, email="disabled@example.com", name="Disabled",
                            password_hash=admin.password_hash, status="DISABLED"))
        assert client.post("/api/v1/auth/invitations/accept", json={
            "token": invite["token"], "name": "Disabled", "password": "correct horse battery staple",
        }).status_code == 400
        if disabled == "client":
            assert client.post(f"/api/v1/clients/{client_id}/invitations/{invite['id']}/resend", headers=headers).status_code == 409
        elif disabled == "user":
            assert client.post(f"/api/v1/clients/{client_id}/invitations", headers=headers, json={
                "email": "disabled@example.com", "role": "CLIENT_ADMIN",
            }).status_code == 409


@pytest.mark.parametrize("action", ["revoke", "resend"])
def test_f3_invitation_accept_and_management_are_atomic(admin: User, action: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with TestClient(app) as client:
        headers = bearer(login(client, admin.email, "correct horse battery staple"))
        invite = client.post("/api/v1/users/invitations", headers=headers, json={
            "email": "race@example.com", "role": "ACCOUNTANT",
        }).json()
    barrier = Barrier(2)

    def accept():
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            return client.post("/api/v1/auth/invitations/accept", json={
                "token": invite["token"], "name": "Race", "password": "race secure password",
            })

    def manage():
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            return client.post(f"/api/v1/users/invitations/{invite['id']}/{action}", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        acceptance = pool.submit(accept)
        management = pool.submit(manage)
        accepted, managed = acceptance.result(timeout=10), management.result(timeout=10)
    assert (accepted.status_code, managed.status_code) in ((200, 409), (400, 200))
    with SessionLocal() as db:
        stored = db.get(UserInvite, UUID(invite["id"]))
        assert bool(stored.accepted_at) != bool(stored.revoked_at)
