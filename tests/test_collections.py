from datetime import UTC, datetime
from decimal import Decimal
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
    AIRun,
    NotificationOutbox,
    ReviewDecision,
    Submission,
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
        recreated = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(headers, "create-after-cancel-1"),
            json=payload(records["first"], records["accountant"]),
        )
        assert recreated.status_code == 201, recreated.text
        assert recreated.json()["id"] != request_id
        duplicate = client.post(
            "/api/v1/collection-requests",
            headers=idempotent(headers, "create-after-cancel-2"),
            json=payload(records["first"], records["accountant"]),
        )
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["id"] != recreated.json()["id"]
        replay = client.post("/api/v1/collection-requests", headers=idempotent(headers, "create-after-cancel-2"), json=payload(records["first"], records["accountant"]))
        assert replay.json()["id"] == duplicate.json()["id"]
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
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["id"] != copy_body["id"]

        filtered = client.get("/api/v1/collection-requests", headers=headers, params={
            "client_id": str(records["first"]),
            "period": "2026-10-01",
            "status": "DRAFT",
            "assignee_id": str(records["accountant"]),
            "due_from": "2026-10-01T00:00:00Z",
            "due_to": "2026-10-31T23:59:59Z",
        })
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 2
        assert {row["id"] for row in filtered.json()["items"]} == {copy_body["id"], duplicate.json()["id"]}

        with SessionLocal.begin() as db:
            db.get(CollectionRequest, UUID(source["id"])).updated_at = datetime(2026, 11, 2, tzinfo=UTC)
            db.get(CollectionRequest, UUID(copy_body["id"])).updated_at = datetime(2026, 11, 1, tzinfo=UTC)
        recently_updated = client.get(
            "/api/v1/collection-requests", headers=headers,
            params={"sort": "updated_at", "order": "desc"},
        )
        assert recently_updated.status_code == 200
        assert [item["id"] for item in recently_updated.json()["items"][:2]] == [
            source["id"], copy_body["id"],
        ]
        assert recently_updated.json()["items"][0]["updated_at"].startswith("2026-11-02")

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
        ).json()["total"] == 2


def test_ai_policy_defaults_validation_copy_and_draft_guard(records):
    with TestClient(app) as client:
        headers = auth_headers(client, "admin@example.com", records["password"])
        body = payload(records["first"], records["admin"])
        created = client.post("/api/v1/collection-requests", headers=idempotent(headers, "ai-default"), json=body)
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["ai_mode"] == "AUTO_REVIEW"
        assert item["review_preference"] == "STANDARD"
        assert Decimal(item["ai_satisfy_threshold"]) == Decimal("0.980")
        assert Decimal(item["ai_request_action_threshold"]) == Decimal("0.980")
        path = f"/api/v1/collection-requests/{item['id']}"
        for changes in ({"ai_mode": "UNKNOWN"}, {"ai_mode": None}, {"review_preference": "UNKNOWN"}, {"review_preference": None}, {"ai_satisfy_threshold": "0.499"}, {"ai_request_action_threshold": "1.001"}, {"ai_satisfy_threshold": "NaN"}, {"ai_satisfy_threshold": "0.9999"}):
            assert client.patch(path, headers=headers, json={"version": item["version"], **changes}).status_code == 422
        updated = client.patch(path, headers=headers, json={"version": item["version"], "ai_mode": "SUGGEST", "review_preference": "CAUTIOUS", "ai_satisfy_threshold": "0.995"}).json()
        copied = client.post(path + "/copy", params={"period": "2026-10-01"}, headers=idempotent(headers, "ai-copy-1"))
        assert copied.status_code == 201, copied.text
        assert copied.json()["ai_mode"] == "SUGGEST"
        assert copied.json()["review_preference"] == "CAUTIOUS"
        assert Decimal(copied.json()["ai_satisfy_threshold"]) == Decimal("0.995")
        published = client.post(path + "/publish", headers=idempotent(headers, "ai-publish"), json={"version": updated["version"]})
        assert published.status_code == 200, published.text
        assert client.patch(path, headers=headers, json={"version": published.json()["version"], "ai_mode": "OFF"}).status_code == 409
        # Analysis is internal: a bank statement needs no manually entered transaction.
        body["period"] = "2026-11-01"
        body["requirements"][0]["type"] = "BANK_STATEMENT"
        result = client.post("/api/v1/collection-requests", headers=idempotent(headers, "automatic-analysis"), json=body)
        assert result.status_code == 201, result.text
        bank = result.json()["requirements"][0]
        assert bank["analysis_type"] == "BANK_TRANSACTION_RECONCILIATION"
        assert "target_transaction" not in bank["criteria"]
        draft_path = f"/api/v1/collection-requests/{result.json()['id']}"
        edited = client.patch(f"/api/v1/requirements/{bank['id']}", headers=headers, json={"version": bank["version"], "type": "RECEIPT", "title": "Receipts", "required": True, "criteria": {}})
        assert edited.status_code == 200, edited.text
        assert edited.json()["analysis_type"] == "DOCUMENT_REQUIREMENT_VALIDATION"
        added = client.post(draft_path + "/requirements", headers=headers, json={"type": "BANK_STATEMENT", "title": "Bank statement"})
        assert added.status_code == 201, added.text
        assert added.json()["analysis_type"] == "BANK_TRANSACTION_RECONCILIATION"
        legacy_target = {"date": "2026-11-02", "description": "Legacy payment", "amount": "-2828.80", "currency": "SGD"}
        with SessionLocal.begin() as db:
            legacy = db.get(Requirement, UUID(added.json()["id"]))
            legacy.analysis_type = "DOCUMENT_REQUIREMENT_VALIDATION"
            legacy.criteria = {"target_transaction": legacy_target, "period_note": "Keep this instruction"}
        copied_draft = client.post(draft_path + "/copy", params={"period": "2026-12-01"}, headers=idempotent(headers, "copy-internal-analysis"))
        assert copied_draft.status_code == 201, copied_draft.text
        copied_bank = next(req for req in copied_draft.json()["requirements"] if req["title"] == "Bank statement")
        assert copied_bank["analysis_type"] == "BANK_TRANSACTION_RECONCILIATION"
        assert copied_bank["criteria"] == {"period_note": "Keep this instruction"}
        with SessionLocal() as db:
            assert db.get(Requirement, UUID(added.json()["id"])).criteria["target_transaction"] == legacy_target
        body["period"] = "2026-12-01"
        body["requirements"][0]["analysis_type"] = "DOCUMENT_REQUIREMENT_VALIDATION"
        assert client.post("/api/v1/collection-requests", headers=idempotent(headers, "client-analysis-override"), json=body).status_code == 422
        with SessionLocal() as db:
            assert db.scalar(select(func.count(AIRun.id))) == 0
            assert db.scalar(select(func.count(NotificationOutbox.id))) == 0


def test_ai_audit_constraints_and_system_events(records):
    from sqlalchemy.exc import IntegrityError
    from uuid import uuid4

    with TestClient(app) as client:
        headers = auth_headers(client, "admin@example.com", records["password"])
        created = client.post("/api/v1/collection-requests", headers=idempotent(headers, "ai-audit-create"), json=payload(records["first"], records["admin"])).json()
        request_id = UUID(created["id"])
        requirement_id = UUID(created["requirements"][0]["id"])
        with SessionLocal.begin() as db:
            submission = Submission(firm_id=records["firm_id"], request_id=request_id, round_no=1, status="SUBMITTED", created_by=records["admin"])
            db.add(submission)
            db.flush()
            run = AIRun(firm_id=records["firm_id"], request_id=request_id, submission_id=submission.id, purpose="REVIEW", requested_by=records["admin"])
            db.add(run)
            db.flush()
            values = dict(firm_id=records["firm_id"], requirement_id=requirement_id, submission_id=submission.id, source="AI", ai_run_id=run.id, decision="SATISFY")
            db.add(ReviewDecision(**values))
            db.add(WorkflowEvent(firm_id=records["firm_id"], request_id=request_id, actor_type="SYSTEM", event_type="AI_REVIEWED", payload={}))
            db.flush()
            for invalid in (ReviewDecision(**values), ReviewDecision(**{**values, "decision": "WAIVE"}), AIRun(firm_id=uuid4(), request_id=request_id, purpose="CLASSIFY"), AIRun(firm_id=records["firm_id"], request_id=request_id, purpose="REVIEW"), WorkflowEvent(firm_id=records["firm_id"], request_id=request_id, actor_type="USER", event_type="INVALID")):
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.add(invalid)
                    db.flush()
            outbox = NotificationOutbox(firm_id=records["firm_id"], request_id=request_id, recipient="client@example.com", template="CHANGES_REQUESTED", dedupe_key="review:1:client")
            db.add(outbox)
            db.flush()
            assert outbox.status == "SUPPRESSED" and outbox.last_error == "PROVIDER_DISABLED"
            with pytest.raises(IntegrityError), db.begin_nested():
                db.add(NotificationOutbox(firm_id=records["firm_id"], request_id=request_id, recipient="client@example.com", template="CHANGES_REQUESTED", dedupe_key="review:1:client"))
                db.flush()
        events = client.get(f"/api/v1/collection-requests/{request_id}/events", headers=headers).json()
        system = next(event for event in events if event["actor_type"] == "SYSTEM")
        assert system["actor_id"] is None and system["actor_name"] == "System"
        review = client.get(f"/api/v1/collection-requests/{request_id}/review", headers=headers)
        assert review.status_code == 200, review.text
        decision = review.json()["requirements"][0]["decisions"][0]
        assert decision["source"] == "AI" and decision["created_by"] is None


def test_ai_migration_preserves_existing_requests_as_suggest(records):
    from alembic import command
    from alembic.config import Config

    with TestClient(app) as client:
        headers = auth_headers(client, "admin@example.com", records["password"])
        created = client.post("/api/v1/collection-requests", headers=idempotent(headers, "migration-old-request"), json=payload(records["first"], records["admin"])).json()
        config = Config("alembic.ini")
        try:
            command.downgrade(config, "0008_submission_notes")
        finally:
            command.upgrade(config, "head")
        migrated = client.get(f"/api/v1/collection-requests/{created['id']}", headers=headers).json()
        assert migrated["ai_mode"] == "SUGGEST"
        assert migrated["status"] == created["status"]
        assert migrated["version"] == created["version"]
        assert len(migrated["requirements"]) == len(created["requirements"])
        assert migrated["events"] == created["events"]
        fresh = client.post("/api/v1/collection-requests", headers=idempotent(headers, "migration-new-request"), json=payload(records["first"], records["admin"], "2026-10-01"))
        assert fresh.status_code == 201, fresh.text
        assert fresh.json()["ai_mode"] == "AUTO_REVIEW"
