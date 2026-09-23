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


def test_legacy_unreviewed_material_is_editable_and_carried_forward(records):
    with TestClient(app) as client:
        portal = auth_headers(client, "client@example.com", records["password"])
        initial = upload(client, portal, records["request"], records["required"])
        assert initial.status_code == 202 and process_next_document()
        submitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=portal,
        )
        assert submitted.status_code == 200
        with SessionLocal.begin() as db:
            db.get(CollectionRequest, records["request"]).status = "CHANGES_REQUESTED"
            db.get(Requirement, records["required"]).status = "RECEIVED"
            db.get(Requirement, records["optional"]).status = "NEEDS_ACTION"

        correction = upload(
            client, portal, records["request"], records["optional"],
            b"%PDF-1.7\ncorrected receipt",
        )
        assert correction.status_code == 202 and process_next_document()
        waiting = client.get(
            f"/api/v1/portal/collection-requests/{records['request']}", headers=portal
        ).json()
        original = next(value for value in waiting["requirements"] if value["id"] == str(records["required"]))
        assert original["documents"][0]["editable"] is True

        resubmitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=portal,
        )
        assert resubmitted.status_code == 200, resubmitted.text
        latest_submission = resubmitted.json()["submission"]["id"]
        admin = auth_headers(client, "admin@example.com", records["password"])
        review = client.get(
            f"/api/v1/collection-requests/{records['request']}/review", headers=admin
        ).json()
        carried = next(value for value in review["requirements"] if value["id"] == str(records["required"]))
        assert [value["id"] for value in carried["documents"] if value["submission_id"] == latest_submission] == [initial.json()["document"]["id"]]


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


def test_staged_classification_confirm_cancel_and_manual_fallback(records, monkeypatch):
    from uuid import UUID
    from app.classification_worker import process_classification
    from app.models import AIRun

    monkeypatch.setattr(settings, "agent_classification_provider", "MANUAL")
    base = f"/api/v1/portal/collection-requests/{records['request']}"
    with TestClient(app) as client:
        headers = auth_headers(client, "client@example.com", records["password"])
        run = client.post(base + "/classification-runs", headers=headers).json()
        path = base + "/classification-runs/" + run["id"]
        response = client.post(base + "/documents", headers=headers, data={"classification_run_id": run["id"]}, files={"file": ("statement.pdf", b"%PDF-1.7\nbank", "application/pdf")})
        assert response.status_code == 202, response.text
        doc_id = response.json()["document_id"]
        choice = {"items": [{"document_id": doc_id, "category": "REQUIREMENT", "requirement_id": str(records['required'])}]}
        assert client.get(base, headers=headers).json()["submission"] is None
        assert client.post(path + "/confirm", headers=headers, json=choice).status_code == 409
        assert client.post(path + "/start", headers=headers).json()["status"] == "QUEUED"
        assert not process_classification()  # Not before scanning.
        assert process_next_document()
        with SessionLocal.begin() as db:
            db.get(AIRun, UUID(run["id"])).next_attempt_at = None
        assert process_classification()
        classified = client.get(path, headers=headers).json()
        assert classified["status"] == "SUCCEEDED"
        assert classified["items"][0]["category"] == "OTHER"
        assert "storage_key" not in str(classified) and "input_snapshot" not in classified
        assert not any(req["documents"] for req in client.get(base, headers=headers).json()["requirements"])
        stranger = auth_headers(client, "stranger@example.com", records["password"])
        assert client.get(path, headers=stranger).status_code == 404
        assert client.post(path + "/confirm", headers=stranger, json=choice).status_code == 404
        # A failed provider can use the same safely scanned files without re-uploading.
        assert client.post(path + "/manual", headers=headers).json()["status"] == "FAILED"
        for _ in range(2):
            confirmed = client.post(path + "/confirm", headers=headers, json=choice)
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["confirmed_at"]
        files = [file for req in client.get(base, headers=headers).json()["requirements"] for file in req["documents"]]
        assert len(files) == 1
        assert client.post(path + "/cancel", headers=headers).status_code == 409
        choice["items"][0]["category"], choice["items"][0]["requirement_id"] = "OTHER", None
        assert client.post(path + "/confirm", headers=headers, json=choice).status_code == 409
        second = client.post(base + "/classification-runs", headers=headers).json()
        second_path = base + "/classification-runs/" + second["id"]
        assert client.post(base + "/documents", headers=headers, data={"classification_run_id": second["id"]}, files={"file": ("extra.pdf", b"%PDF-1.7\nextra", "application/pdf")}).status_code == 202
        assert client.post(second_path + "/cancel", headers=headers).json()["status"] == "CANCELLED"
        assert client.post(second_path + "/start", headers=headers).status_code == 409
        assert client.post(second_path + "/confirm", headers=headers, json=choice).status_code == 409
        assert len([file for req in client.get(base, headers=headers).json()["requirements"] for file in req["documents"]]) == 1


@pytest.mark.parametrize("result_kind", ["valid", "wrong_id", "finding", "cancel", "unavailable"])
def test_classification_worker_validates_and_discards_late_results(records, monkeypatch, result_kind):
    import io
    import json
    from urllib.error import URLError
    from uuid import UUID, uuid4
    from app import classification_worker
    from app.models import AIRun

    monkeypatch.setattr(settings, "agent_classification_provider", "REMOTE")
    base = f"/api/v1/portal/collection-requests/{records['request']}"
    with TestClient(app) as client:
        headers = auth_headers(client, "client@example.com", records["password"])
        run = client.post(base + "/classification-runs", headers=headers).json()
        path = base + "/classification-runs/" + run["id"]
        doc = client.post(base + "/documents", headers=headers, data={"classification_run_id": run["id"]}, files={"file": ("bank.pdf", b"%PDF-1.7\nbank statement", "application/pdf")}).json()["document_id"]
        assert process_next_document()
        assert client.post(path + "/start", headers=headers).status_code == 200

        def respond(request, **kwargs):
            body = json.loads(request.data)
            assert body["purpose"] == "CLASSIFY" and body["run_id"] == run["id"]
            assert set(body["documents"][0]) == {"document_id", "storage_key", "content_type", "sha256", "original_name"}
            if result_kind == "unavailable": raise URLError("secret error")
            if result_kind == "cancel": assert client.post(path + "/cancel", headers=headers).status_code == 200
            result = {"schema_version": "1", "run_id": str(uuid4()) if result_kind == "wrong_id" else run["id"], "model_version": "test-v1", "classifications": [{"document_id": doc, "category": "REQUIREMENT", "requirement_id": str(records["required"]), "document_type": "BANK_STATEMENT", "confidence": 0.99}]}
            if result_kind == "finding": result["classifications"][0]["finding"] = "APPROVE"
            return io.BytesIO(json.dumps(result).encode())

        monkeypatch.setattr(classification_worker, "urlopen", respond)
        assert classification_worker.process_classification()
        if result_kind == "unavailable":
            for _ in range(2):
                with SessionLocal.begin() as db: db.get(AIRun, UUID(run["id"])).next_attempt_at = None
                assert classification_worker.process_classification()
        result = client.get(path, headers=headers).json()
        assert result["status"] == ("SUCCEEDED" if result_kind == "valid" else "CANCELLED" if result_kind == "cancel" else "FAILED")
        assert "secret" not in str(result)
        assert client.get(base, headers=headers).json()["submission"] is None
        assert not classification_worker.process_classification()
        if result_kind == "valid":
            with SessionLocal.begin() as db:
                db.get(CollectionRequest, records["request"]).status = "CHANGES_REQUESTED"
                db.get(Requirement, records["required"]).status = "SATISFIED"
            choice = {"items": [{"document_id": doc, "category": "REQUIREMENT", "requirement_id": str(records["required"])}]}
            assert client.post(path + "/confirm", headers=headers, json=choice).status_code == 409


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
        incomplete_return = client.post(
            f"/api/v1/collection-requests/{records['request']}/request-changes",
            headers={**admin, "Idempotency-Key": "request-changes-incomplete"},
            json={"version": reviewed.json()["version"], "reason": "Wrong period"},
        )
        assert incomplete_return.status_code == 422
        assert incomplete_return.json()["code"] == "REVIEW_INCOMPLETE"
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
        returned = client.post(
            f"/api/v1/collection-requests/{records['request']}/request-changes",
            headers={**admin, "Idempotency-Key": "request-changes-1"},
            json={"version": accepted_review.json()["version"], "reason": "Wrong period"},
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
