from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import SessionLocal, redis_client
from app.main import app
from app.models import (
    Base,
    Client,
    ClientAssignment,
    CollectionRequest,
    Firm,
    FirmMember,
    Requirement,
    User,
    WorkflowEvent,
)


@pytest.fixture(autouse=True)
def clean_state():
    if (
        settings.environment != "test"
        or make_url(settings.database_url).database != "acc_test"
        or redis_client.connection_pool.connection_kwargs.get("db") != 15
    ):
        pytest.fail("Collection tests require the disposable test services")
    with SessionLocal.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
    redis_client.flushdb()
    yield
    redis_client.flushdb()


@pytest.fixture
def records() -> dict[str, object]:
    password = "correct horse battery staple"
    with SessionLocal.begin() as db:
        firm = Firm(name="Collection Test Firm")
        db.add(firm)
        db.flush()
        users = {}
        for key, role in (
            ("admin", "FIRM_ADMIN"),
            ("accountant", "ACCOUNTANT"),
            ("unassigned", "ACCOUNTANT"),
        ):
            user = User(
                firm_id=firm.id,
                email=f"{key}@example.com",
                name=key.title(),
                password_hash=hash_password(password),
            )
            db.add(user)
            db.flush()
            db.add(FirmMember(firm_id=firm.id, user_id=user.id, role=role))
            users[key] = user
        first = Client(firm_id=firm.id, code="FIRST", legal_name="First Client")
        second = Client(firm_id=firm.id, code="SECOND", legal_name="Second Client")
        db.add_all([first, second])
        db.flush()
        db.add(ClientAssignment(
            firm_id=firm.id, client_id=first.id, user_id=users["accountant"].id
        ))
        result = {
            "password": password,
            "firm_id": firm.id,
            "first": first.id,
            "second": second.id,
            **{key: value.id for key, value in users.items()},
        }
    return result


def auth_headers(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={
        "email": email, "password": password,
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def payload(client_id: object, assignee_id: object, period: str = "2026-09-01"):
    return {
        "client_id": str(client_id),
        "period": period,
        "due_at": f"{period[:7]}-25T23:59:00+08:00",
        "scope_note": "Monthly bookkeeping documents",
        "assignee_id": str(assignee_id),
        "requirements": [
            {"type": "BANK_STATEMENT", "title": "Bank statement", "required": True},
            {"type": "SALES_INVOICE", "title": "Sales invoices", "required": True},
            {"type": "RECEIPT", "title": "Receipts", "required": False},
        ],
    }


def idempotent(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def test_create_is_atomic_and_assignment_scoped(records) -> None:
    with TestClient(app) as client:
        accountant = auth_headers(client, "accountant@example.com", records["password"])
        created = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(accountant, "create-assigned-1"),
            json=payload(records["first"], records["accountant"]),
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "DRAFT"
        assert len(created.json()["requirements"]) == 3

        denied = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(accountant, "create-unassigned-1"),
            json=payload(records["second"], records["accountant"]),
        )
        assert denied.status_code == 404

        admin = auth_headers(client, "admin@example.com", records["password"])
        allowed = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(admin, "create-admin-001"),
            json=payload(records["second"], records["admin"]),
        )
        assert allowed.status_code == 201
        invalid_payload = payload(records["first"], records["accountant"], "2026-10-01")
        invalid_payload["requirements"][1]["title"] = " "
        invalid = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(admin, "create-invalid-1"),
            json=invalid_payload,
        )
        assert invalid.status_code == 422
        with SessionLocal() as db:
            assert db.scalar(select(func.count(CollectionRequest.id))) == 2
            assert db.scalar(select(func.count(Requirement.id))) == 6


def test_versions_publish_idempotency_and_immutable_requirements(records) -> None:
    with TestClient(app) as client:
        headers = auth_headers(client, "admin@example.com", records["password"])
        created = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(headers, "create-version-1"),
            json=payload(records["first"], records["accountant"]),
        ).json()
        request_id = created["id"]
        updated = client.patch(
            f"/api/v1/collection-requests/{request_id}",
            headers=headers,
            json={"version": created["version"], "scope_note": "Updated scope"},
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == created["version"] + 1
        conflict = client.patch(
            f"/api/v1/collection-requests/{request_id}",
            headers=headers,
            json={"version": created["version"], "scope_note": "Stale edit"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "VERSION_CONFLICT"
        assert conflict.json()["details"]["scope_note"] == "Updated scope"

        requirement = updated.json()["requirements"][0]
        changed = client.patch(
            f"/api/v1/requirements/{requirement['id']}", headers=headers,
            json={
                "version": requirement["version"], "type": requirement["type"],
                "title": "All bank statements", "required": True, "criteria": {},
            },
        )
        assert changed.status_code == 200
        publish_headers = idempotent(headers, "publish-request-1")
        version = updated.json()["version"]
        published = client.post(
            f"/api/v1/collection-requests/{request_id}/publish",
            headers=publish_headers, json={"version": version},
        )
        assert published.status_code == 200
        assert published.json()["status"] == "OPEN"
        replay = client.post(
            f"/api/v1/collection-requests/{request_id}/publish",
            headers=publish_headers, json={"version": version},
        )
        assert replay.status_code == 200 and replay.json() == published.json()
        reused = client.post(
            f"/api/v1/collection-requests/{request_id}/publish",
            headers=publish_headers, json={"version": published.json()["version"]},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert client.patch(
            f"/api/v1/requirements/{requirement['id']}", headers=headers,
            json={
                "version": changed.json()["version"], "type": requirement["type"],
                "title": "Forbidden", "required": True, "criteria": {},
            },
        ).status_code == 409
        assert client.delete(
            f"/api/v1/requirements/{requirement['id']}", headers=headers,
            params={"version": changed.json()["version"]},
        ).status_code == 409
        cancelled = client.post(
            f"/api/v1/collection-requests/{request_id}/cancel",
            headers=idempotent(headers, "cancel-request-1"),
            json={"version": published.json()["version"], "reason": "Client engagement ended"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
        assert cancelled.json()["events"][0]["payload"]["reason"] == "Client engagement ended"
        with SessionLocal() as db:
            assert db.scalar(select(func.count(WorkflowEvent.id)).where(
                WorkflowEvent.request_id == UUID(request_id),
                WorkflowEvent.event_type == "PUBLISHED",
            )) == 1


def test_copy_and_combined_filters_do_not_leak_tenants(records) -> None:
    with TestClient(app) as client:
        headers = auth_headers(client, "admin@example.com", records["password"])
        source = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(headers, "create-copy-src"),
            json=payload(records["first"], records["accountant"]),
        ).json()
        copied = client.post(
            f"/api/v1/collection-requests/{source['id']}/copy",
            headers=idempotent(headers, "copy-next-period"),
            params={"period": "2026-10-01"},
        )
        assert copied.status_code == 201, copied.text
        copy_body = copied.json()
        assert copy_body["status"] == "DRAFT"
        assert [item["title"] for item in copy_body["requirements"]] == [
            item["title"] for item in source["requirements"]
        ]
        assert all(item["status"] == "PENDING" for item in copy_body["requirements"])
        assert [event["event_type"] for event in copy_body["events"]] == ["COPIED"]
        duplicate = client.post(
            f"/api/v1/collection-requests/{source['id']}/copy",
            headers=idempotent(headers, "copy-next-other-key"),
            params={"period": "2026-10-01"},
        )
        assert duplicate.status_code == 409

        filtered = client.get("/api/v1/collection-requests", headers=headers, params={
            "client_id": str(records["first"]),
            "period": "2026-10-01",
            "status": "DRAFT",
            "assignee_id": str(records["accountant"]),
            "due_from": "2026-10-01T00:00:00Z",
            "due_to": "2026-10-31T23:59:59Z",
        })
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["id"] == copy_body["id"]

        with SessionLocal.begin() as db:
            other_firm = Firm(name="Other Firm")
            db.add(other_firm)
            db.flush()
            other_user = User(
                firm_id=other_firm.id,
                email="foreign@example.com",
                name="Foreign",
                password_hash=hash_password(str(records["password"])),
            )
            db.add(other_user)
            db.flush()
            db.add(FirmMember(
                firm_id=other_firm.id, user_id=other_user.id, role="FIRM_ADMIN"
            ))
            other_client = Client(
                firm_id=other_firm.id, code="FOREIGN", legal_name="Foreign Client"
            )
            db.add(other_client)
            db.flush()
            db.add(CollectionRequest(
                firm_id=other_firm.id,
                client_id=other_client.id,
                period=datetime(2026, 10, 1, tzinfo=UTC).date(),
                due_at=datetime(2026, 10, 25, tzinfo=UTC),
                created_by=other_user.id,
                assignee_id=other_user.id,
            ))
        assert client.get(
            "/api/v1/collection-requests", headers=headers,
            params={"period": "2026-10-01"},
        ).json()["total"] == 1
