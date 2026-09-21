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
    ClientMember,
    CollectionRequest,
    Firm,
    FirmMember,
    Requirement,
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
        client_user = User(
            firm_id=firm.id, email="client@example.com", name="Casey Client",
            password_hash=hash_password(password),
        )
        stranger = User(
            firm_id=firm.id, email="stranger@example.com", name="Other Client",
            password_hash=hash_password(password),
        )
        db.add_all([admin, client_user, stranger])
        db.flush()
        db.add(FirmMember(firm_id=firm.id, user_id=admin.id, role="FIRM_ADMIN"))
        first = Client(firm_id=firm.id, code="FIRST", legal_name="First Client")
        second = Client(firm_id=firm.id, code="SECOND", legal_name="Second Client")
        db.add_all([first, second])
        db.flush()
        db.add_all([
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
            "optional": optional.id,
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

        uploaded = upload(client, headers, records["request"], records["required"])
        assert uploaded.status_code == 202, uploaded.text
        document_id = uploaded.json()["document"]["id"]
        link_id = uploaded.json()["document"]["link_id"]
        assert uploaded.json()["document"]["status"] == "QUARANTINED"
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
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["status"] == "IN_REVIEW"
        assert submitted.json()["submission"]["status"] == "SUBMITTED"
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
