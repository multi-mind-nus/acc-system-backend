from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import SessionLocal, redis_client
from app.main import app
from app.models import (
    Base,
    Client,
    ClientAssignment,
    ClientMember,
    CollectionRequest,
    Firm,
    FirmMember,
    Requirement,
    ReviewDecision,
    User,
)
from app.worker import _scan_file, process_next_document


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    if (
        settings.environment != "test"
        or make_url(settings.database_url).database != "acc_test"
        or redis_client.connection_pool.connection_kwargs.get("db") != 15
    ):
        pytest.fail("Portal tests require the disposable test services")
    with SessionLocal.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
    redis_client.flushdb()
    monkeypatch.setattr(settings, "quarantine_path", str(tmp_path / "quarantine"))
    monkeypatch.setattr(settings, "document_path", str(tmp_path / "documents"))
    monkeypatch.setattr(settings, "clamav_host", None)
    yield
    redis_client.flushdb()


@pytest.fixture
def records():
    password = "client-password"
    with SessionLocal.begin() as db:
        firm = Firm(name="Portal Test Firm")
        db.add(firm)
        db.flush()
        admin = User(
            firm_id=firm.id, email="admin@example.com", name="Avery Accountant",
            password_hash=hash_password(password),
        )
        accountant = User(
            firm_id=firm.id, email="accountant@example.com", name="Alex Accountant",
            password_hash=hash_password(password),
        )
        client_user = User(
            firm_id=firm.id, email="client@example.com", name="Casey Client",
            password_hash=hash_password(password),
        )
        stranger = User(
            firm_id=firm.id, email="stranger@example.com", name="Other Client",
            password_hash=hash_password(password),
        )
        db.add_all([admin, accountant, client_user, stranger])
        db.flush()
        db.add(FirmMember(firm_id=firm.id, user_id=admin.id, role="FIRM_ADMIN"))
        db.add(FirmMember(
            firm_id=firm.id, user_id=accountant.id, role="ACCOUNTANT"
        ))
        first = Client(firm_id=firm.id, code="FIRST", legal_name="First Client")
        second = Client(firm_id=firm.id, code="SECOND", legal_name="Second Client")
        db.add_all([first, second])
        db.flush()
        db.add_all([
            ClientAssignment(
                firm_id=firm.id, client_id=first.id, user_id=accountant.id,
            ),
            ClientMember(
                firm_id=firm.id, client_id=first.id, user_id=client_user.id,
                role="CLIENT_SUBMITTER",
            ),
            ClientMember(
                firm_id=firm.id, client_id=second.id, user_id=stranger.id,
                role="CLIENT_SUBMITTER",
            ),
        ])
        request = CollectionRequest(
            firm_id=firm.id, client_id=first.id, period=date(2026, 9, 1),
            due_at=datetime(2026, 9, 25, tzinfo=UTC), status="OPEN",
            scope_note="Upload September records", created_by=admin.id,
            assignee_id=admin.id,
        )
        db.add(request)
        db.flush()
        required = Requirement(
            firm_id=firm.id, request_id=request.id, position=0,
            type="BANK_STATEMENT", title="Bank statement", required=True,
        )
        optional = Requirement(
            firm_id=firm.id, request_id=request.id, position=1,
            type="RECEIPT", title="Receipts", required=False,
        )
        db.add_all([required, optional])
        db.flush()
        return {
            "password": password, "request": request.id, "required": required.id,
            "optional": optional.id, "admin": admin.id, "first": first.id,
        }


def auth_headers(client: TestClient, email: str, password: str):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def upload(client, headers, request_id, requirement_id, body=b"%PDF-1.7\nhello"):
    return client.post(
        f"/api/v1/portal/collection-requests/{request_id}/documents",
        headers=headers,
        data={"requirement_id": str(requirement_id)},
        files={"file": ("statement.pdf", body, "application/pdf")},
    )


def test_upload_scan_duplicate_exclude_and_submit(records, monkeypatch):
    real_replace = __import__("os").replace

    def reject_cross_volume_replace(source, target):
        assert source.parent == target.parent
        real_replace(source, target)

    monkeypatch.setattr("app.worker.os.replace", reject_cross_volume_replace)
    with TestClient(app) as client:
        headers = auth_headers(client, "client@example.com", records["password"])
        listed = client.get("/api/v1/portal/collection-requests", headers=headers)
        assert listed.status_code == 200 and listed.json()["total"] == 1
        assert "updated_at" in listed.json()["items"][0]

        with SessionLocal.begin() as db:
            original = db.get(CollectionRequest, records["request"])
            original.updated_at = datetime(2026, 11, 2, tzinfo=UTC)
            older = CollectionRequest(
                firm_id=original.firm_id,
                client_id=records["first"],
                period=date(2026, 10, 1),
                due_at=datetime(2026, 10, 25, tzinfo=UTC),
                status="CLOSED",
                created_by=records["admin"],
                assignee_id=records["admin"],
                updated_at=datetime(2026, 11, 1, tzinfo=UTC),
            )
            db.add(older)
            db.flush()
            older_id = str(older.id)
        filtered = client.get(
            "/api/v1/portal/collection-requests",
            headers=headers,
            params={
                "client_id": str(records["first"]),
                "period": "2026-10-01",
                "status": "CLOSED",
                "sort": "period",
                "order": "desc",
            },
        )
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["items"]] == [older_id]
        ordered = client.get("/api/v1/portal/collection-requests", headers=headers)
        assert ordered.json()["items"][0]["id"] == str(records["request"])

        uploaded = upload(client, headers, records["request"], records["required"])
        assert uploaded.status_code == 202, uploaded.text
        document_id = uploaded.json()["document"]["id"]
        link_id = uploaded.json()["document"]["link_id"]
        assert uploaded.json()["document"]["status"] == "QUARANTINED"
        assert uploaded.json()["document"]["editable"] is True
        assert client.get(
            f"/api/v1/portal/document-links/{link_id}/download", headers=headers
        ).status_code == 409
        assert process_next_document()

        available = client.get(f"/api/v1/portal/documents/{document_id}", headers=headers)
        assert available.json()["status"] == "AVAILABLE"
        download = client.get(
            f"/api/v1/portal/document-links/{link_id}/download", headers=headers
        )
        assert download.status_code == 200
        assert download.headers["x-accel-redirect"].startswith("/__protected_documents/")

        duplicate = upload(client, headers, records["request"], records["optional"])
        assert duplicate.status_code == 202
        assert duplicate.json()["document"]["id"] == document_id
        assert duplicate.json()["document"]["duplicate"] is True
        optional_link_id = duplicate.json()["document"]["link_id"]
        excluded = client.delete(f"/api/v1/portal/document-links/{optional_link_id}", headers=headers)
        assert excluded.status_code == 200
        # The latest link is excluded; the required link remains valid.
        submitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=headers,
            json={"note": "Bank statement uploaded; receipts will follow if needed."},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["status"] == "IN_REVIEW"
        assert submitted.json()["submission"]["status"] == "SUBMITTED"
        assert submitted.json()["submission"]["note"] == (
            "Bank statement uploaded; receipts will follow if needed."
        )
        assert submitted.json()["requirements"][0]["documents"][0]["editable"] is False
        assert client.delete(
            f"/api/v1/portal/document-links/{optional_link_id}", headers=headers
        ).status_code == 409


def test_validation_malware_and_tenant_hiding(records):
    with TestClient(app) as client:
        headers = auth_headers(client, "client@example.com", records["password"])
        mismatch = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/documents",
            headers=headers,
            data={"requirement_id": str(records["required"])},
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        )
        assert mismatch.status_code == 415

        infected = upload(
            client, headers, records["request"], records["required"],
            b"%PDF-1.7\nEICAR-STANDARD-ANTIVIRUS-TEST-FILE",
        )
        assert infected.status_code == 202
        infected_id = infected.json()["document"]["id"]
        assert process_next_document()
        assert client.get(
            f"/api/v1/portal/documents/{infected_id}", headers=headers
        ).json()["status"] == "FAILED"
        assert client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=headers,
        ).status_code == 422

        stranger = auth_headers(client, "stranger@example.com", records["password"])
        assert client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=stranger
        ).status_code == 404
        assert client.get(
            f"/api/v1/portal/documents/{infected_id}", headers=stranger
        ).status_code == 404


def test_fake_classification_matches_filename_to_requirement(records):
    with TestClient(app) as client:
        headers = auth_headers(client, "client@example.com", records["password"])
        payload = {"files": [
            {"name": "september-bank-statement.pdf", "content_type": "application/pdf", "size_bytes": 42},
            {"name": "misc.png", "content_type": "image/png", "size_bytes": 12},
            {"name": "notes.txt", "content_type": "text/plain", "size_bytes": 8},
        ]}
        response = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/classify",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "provider": "FAKE",
            "items": [
                {"index": 0, "category": "REQUIREMENT", "requirement_id": str(records["required"]), "confidence": 0.9},
                {"index": 1, "category": "OTHER", "requirement_id": None, "confidence": 0.55},
                {"index": 2, "category": "INVALID", "requirement_id": None, "confidence": 0.99},
            ],
        }
        stranger = auth_headers(client, "stranger@example.com", records["password"])
        assert client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/classify",
            headers=stranger,
            json=payload,
        ).status_code == 404


def test_production_never_releases_a_file_without_clamav(tmp_path, monkeypatch):
    document = tmp_path / "safe.pdf"
    document.write_bytes(b"%PDF-1.7\nsafe")
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "clamav_host", None)
    with pytest.raises(RuntimeError, match="ClamAV is required"):
        _scan_file(document)


def test_review_changes_resubmit_approve_reopen_and_close(records):
    with TestClient(app) as client:
        portal = auth_headers(client, "client@example.com", records["password"])
        first = upload(client, portal, records["request"], records["required"])
        assert first.status_code == 202
        first_document = first.json()["document"]["id"]
        accepted = upload(
            client, portal, records["request"], records["optional"],
            b"%PDF-1.7\nreceipt",
        )
        assert accepted.status_code == 202
        accepted_link = accepted.json()["document"]["link_id"]
        assert process_next_document()
        assert process_next_document()
        submitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=portal,
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["requirements"][0]["status"] == "RECEIVED"

        admin = auth_headers(client, "admin@example.com", records["password"])
        review = client.get(
            f"/api/v1/collection-requests/{records['request']}/review",
            headers=admin,
        ).json()
        requirement = next(value for value in review["requirements"] if value["required"])
        optional = next(value for value in review["requirements"] if not value["required"])
        submission_id = review["submissions"][-1]["id"]
        accepted_review = client.post(
            f"/api/v1/requirements/{records['optional']}/review",
            headers=admin,
            json={
                "version": optional["version"],
                "submission_id": submission_id,
                "decision": "SATISFY",
            },
        )
        assert accepted_review.status_code == 200, accepted_review.text
        invalid = client.post(
            f"/api/v1/requirements/{records['required']}/review",
            headers=admin,
            json={
                "version": requirement["version"],
                "submission_id": submission_id,
                "decision": "REQUEST_ACTION",
            },
        )
        assert invalid.status_code == 422
        reviewed = client.post(
            f"/api/v1/requirements/{records['required']}/review",
            headers=admin,
            json={
                "version": requirement["version"],
                "submission_id": submission_id,
                "decision": "REQUEST_ACTION",
                "issue_code": "WRONG_PERIOD",
                "client_message": "Please upload the September statement.",
                "internal_note": "The file is for August.",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["requirements"][0]["status"] == "NEEDS_ACTION"
        returned = client.post(
            f"/api/v1/collection-requests/{records['request']}/request-changes",
            headers={**admin, "Idempotency-Key": "request-changes-1"},
            json={"version": reviewed.json()["version"], "reason": "Wrong period"},
        )
        assert returned.status_code == 200, returned.text
        assert returned.json()["status"] == "CHANGES_REQUESTED"
        waiting_detail = client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=portal
        ).json()
        original_link = waiting_detail["requirements"][0]["documents"][0]["link_id"]
        assert waiting_detail["requirements"][0]["documents"][0]["editable"] is True
        assert waiting_detail["requirements"][0]["documents"][0]["counts_for_submission"] is False
        accepted_requirement = next(
            value for value in waiting_detail["requirements"]
            if value["id"] == str(records["optional"])
        )
        assert accepted_requirement["documents"][0]["editable"] is False
        locked_upload = upload(
            client, portal, records["request"], records["optional"],
            b"%PDF-1.7\nreplacement receipt",
        )
        assert locked_upload.status_code == 409
        assert locked_upload.json()["code"] == "SUBMISSION_READ_ONLY"
        locked_remove = client.delete(
            f"/api/v1/portal/document-links/{accepted_link}", headers=portal
        )
        assert locked_remove.status_code == 409
        assert locked_remove.json()["code"] == "SUBMISSION_READ_ONLY"

        replacement = upload(
            client, portal, records["request"], records["required"],
            b"%PDF-1.7\nseptember",
        )
        replacement_document = replacement.json()["document"]["id"]
        assert replacement.json()["document"]["editable"] is True
        assert process_next_document()
        supplemented = client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=portal
        ).json()
        supplemented_documents = supplemented["requirements"][0]["documents"]
        assert [value["id"] for value in supplemented_documents] == [
            str(first_document), str(replacement_document),
        ]
        assert supplemented_documents[0]["editable"] is True
        assert supplemented_documents[0]["counts_for_submission"] is False
        assert supplemented_documents[1]["counts_for_submission"] is True

        removed = client.delete(
            f"/api/v1/portal/document-links/{original_link}", headers=portal
        )
        assert removed.status_code == 200, removed.text
        after_remove = client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=portal
        ).json()
        assert [
            value["id"] for value in after_remove["requirements"][0]["documents"]
        ] == [str(replacement_document)]
        resubmitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=portal,
            json={"note": "Correct September statement attached."},
        )
        assert resubmitted.status_code == 200, resubmitted.text
        assert resubmitted.json()["submission"]["note"] == (
            "Correct September statement attached."
        )

        review = client.get(
            f"/api/v1/collection-requests/{records['request']}/review",
            headers=admin,
        ).json()
        requirement = review["requirements"][0]
        satisfied = client.post(
            f"/api/v1/requirements/{records['required']}/review",
            headers=admin,
            json={
                "version": requirement["version"],
                "submission_id": review["submissions"][-1]["id"],
                "decision": "SATISFY",
                "client_message": "September statement received.",
            },
        )
        assert satisfied.status_code == 200, satisfied.text
        latest_decision = satisfied.json()["requirements"][0]["decisions"][0]
        assert {
            (value["document_id"], value["relation"])
            for value in latest_decision["evidence"]
        } == {
            (str(replacement_document), "SUPPORTS"),
        }
        assert len(satisfied.json()["requirements"][0]["documents"]) == 2
        approved = client.post(
            f"/api/v1/collection-requests/{records['request']}/approve",
            headers={**admin, "Idempotency-Key": "approve-review-1"},
            json={"version": satisfied.json()["version"]},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "READY_FOR_BOOKKEEPING"

        accountant = auth_headers(client, "accountant@example.com", records["password"])
        assert client.post(
            f"/api/v1/collection-requests/{records['request']}/reopen",
            headers={**accountant, "Idempotency-Key": "reopen-forbidden-1"},
            json={"version": approved.json()["version"], "reason": "Check again"},
        ).status_code == 403
        reopened = client.post(
            f"/api/v1/collection-requests/{records['request']}/reopen",
            headers={**admin, "Idempotency-Key": "reopen-review-1"},
            json={"version": approved.json()["version"], "reason": "Check again"},
        )
        assert reopened.status_code == 200, reopened.text
        approved_again = client.post(
            f"/api/v1/collection-requests/{records['request']}/approve",
            headers={**admin, "Idempotency-Key": "approve-review-2"},
            json={"version": reopened.json()["version"]},
        )
        closed = client.post(
            f"/api/v1/collection-requests/{records['request']}/close",
            headers={**admin, "Idempotency-Key": "close-review-1"},
            json={"version": approved_again.json()["version"], "reason": "Books completed"},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "CLOSED"

        portal_detail = client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=portal
        )
        assert portal_detail.status_code == 200
        assert "internal_note" not in portal_detail.text
        assert portal_detail.json()["requirements"][0]["client_message"] == (
            "September statement received."
        )
        with SessionLocal() as db:
            assert db.query(ReviewDecision).count() == 3
