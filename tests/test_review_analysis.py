from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analysis_schemas import AmountRelation
from app.db import SessionLocal
from app.main import app
from app.models import AIRun, Client, CollectionRequest, Document, Firm, FirmMember, Requirement, RequirementDocument, ReviewDecision, Submission, User
from app.review_analysis import amount_valid, authorized_documents, process_review
from app.worker import process_next_document
from test_portal import clean_state, records, auth_headers, upload  # noqa: F401


def response(body):
    return {"schema_version": "1", "run_id": str(body.run_id), "model_version": "test-review-v1", "extractions": [{"document_id": str(d.document_id), "entity_name": "First Client", "period": "2026-09-01", "amount": "10.00", "currency": "SGD"} for d in body.documents],
        "findings": [{"requirement_id": str(r.id), "action": "ESCALATE", "suggested_decision": None, "issue_code": None, "confidence": 0.5, "entity_check": "UNKNOWN", "period_check": "UNKNOWN", "explanation": "Manual verification required", "client_message": None, "evidence": [{"document_id": str(d.document_id), "relation": "REFERENCE", "reason": "Check source"} for d in body.documents], "amounts": []} for r in body.requirements]}


def submitted(client, records):
    portal = auth_headers(client, "client@example.com", records["password"])
    staff = auth_headers(client, "admin@example.com", records["password"])
    result = upload(client, portal, records["request"], records["required"])
    assert result.status_code == 202
    assert process_next_document()
    with SessionLocal() as db:
        assert not db.scalar(select(AIRun).where(AIRun.purpose == "REVIEW"))
    result = client.post(f"/api/v1/portal/collection-requests/{records['request']}/submit", headers=portal)
    assert result.status_code == 200, result.text
    assert result.json()["review_status"] == "PROCESSING"
    return portal, staff


def test_submit_analysis_is_advisory_private_and_idempotent(records, monkeypatch):
    monkeypatch.setattr("app.review_analysis.call_agent", response)
    with TestClient(app) as client:
        portal, staff = submitted(client, records)
        assert process_review()
        assert not process_review()
        path = f"/api/v1/collection-requests/{records['request']}/review-runs"
        result = client.get(path, headers=staff).json()[0]
        assert result["status"] == "SUCCEEDED"
        assert result["output"]["findings"][0]["manual_reasons"] == ["ESCALATED", "LOW_CONFIDENCE", "MANUAL_REVIEW_REQUIRED"]
        assert "storage_key" not in str(result)
        assert client.get(path, headers=portal).status_code == 403
        public = client.get(f"/api/v1/portal/collection-requests/{records['request']}", headers=portal).json()
        assert public["review_status"] == "AWAITING_ACCOUNTANT"
        assert "extractions" not in str(public) and "confidence" not in str(public)
        with SessionLocal() as db:
            assert db.get(CollectionRequest, records["request"]).status == "IN_REVIEW"
            assert db.get(Requirement, records["required"]).status == "RECEIVED"
            assert not db.scalar(select(ReviewDecision))
            assert db.scalar(select(Document)).extracted_data["amount"] == "10.00"


@pytest.mark.parametrize("failure", ["timeout", "evidence", "run_id", "schema", "search_limit", "stale"])
def test_failures_retry_and_stale_results(records, monkeypatch, failure):
    def agent(body):
        if failure == "timeout":
            raise TimeoutError()
        result = response(body)
        if failure == "evidence":
            result["findings"][0]["evidence"][0]["document_id"] = str(uuid4())
        if failure == "run_id":
            result["run_id"] = str(uuid4())
        if failure == "schema":
            result["findings"][0]["action"] = "APPROVE"
        if failure == "search_limit":
            result.update(findings=[], search={"action": "SEARCH_HISTORY", "requirement_id": str(body.requirements[0].id)})
        if failure == "stale":
            with SessionLocal.begin() as db:
                db.get(CollectionRequest, records["request"]).status = "CANCELLED"
        return result
    monkeypatch.setattr("app.review_analysis.call_agent", agent)
    with TestClient(app) as client:
        _, staff = submitted(client, records)
        iterations = 3 if failure == "timeout" else 4 if failure == "search_limit" else 1
        for _ in range(iterations):
            with SessionLocal.begin() as db:
                run = db.scalar(select(AIRun).where(AIRun.purpose == "REVIEW"))
                run.next_attempt_at = None
            assert process_review()
        path = f"/api/v1/collection-requests/{records['request']}/review-runs"
        result = client.get(path, headers=staff).json()[0]
        assert result["status"] == ("CANCELLED" if failure == "stale" else "FAILED")
        with SessionLocal() as db:
            assert not db.scalar(select(ReviewDecision))
            assert db.scalar(select(Document)).extracted_data is None
        if failure != "stale":
            monkeypatch.setattr("app.review_analysis.call_agent", response)
            first = client.post(path + f"/{result['id']}/retry", headers=staff)
            assert first.status_code == 200, first.text
            second = client.post(path + f"/{result['id']}/retry", headers=staff)
            assert second.json()["id"] == first.json()["id"]
            assert process_review()


def test_decimal_and_bad_amounts_remain_manual(records, monkeypatch):
    def agent(body):
        result = response(body)
        result["findings"][0]["amounts"] = [{"currency": "SGD", "operation": "SUM", "operands": [{"document_id": str(body.documents[0].document_id), "amount": "0.1", "label": "One"}, {"document_id": str(body.documents[0].document_id), "amount": "0.2", "label": "Two"}], "expected_amount": "0.30", "actual_amount": "0.4", "difference": "0.00"}]
        return result
    monkeypatch.setattr("app.review_analysis.call_agent", agent)
    with TestClient(app) as client:
        _, staff = submitted(client, records)
        assert process_review()
        result = client.get(f"/api/v1/collection-requests/{records['request']}/review-runs", headers=staff).json()[0]
        finding = result["output"]["findings"][0]
        assert not finding["amounts_valid"] and "AMOUNT_MISMATCH" in finding["manual_reasons"]
        relation = deepcopy(finding["amounts"][0])
        relation["actual_amount"] = "0.3"
        assert amount_valid(AmountRelation.model_validate(relation))


def test_history_search_is_scoped_and_lease_can_be_reclaimed(records, monkeypatch):
    with TestClient(app) as client:
        _, staff = submitted(client, records)
        with SessionLocal.begin() as db:
            current = db.get(CollectionRequest, records["request"])
            source = db.scalar(select(Document))
            previous = CollectionRequest(firm_id=current.firm_id, client_id=current.client_id, period=date(2026, 8, 1), due_at=current.due_at, status="CLOSED", created_by=current.created_by, assignee_id=current.assignee_id)
            db.add(previous)
            db.flush()
            sub = Submission(firm_id=current.firm_id, request_id=previous.id, round_no=1, status="SUBMITTED", created_by=current.created_by)
            db.add(sub)
            db.flush()
            historical = Document(firm_id=current.firm_id, client_id=current.client_id, uploader_id=source.uploader_id, original_name="B03 evidence.pdf", content_type=source.content_type, size_bytes=source.size_bytes, sha256=source.sha256, storage_key=source.storage_key, status="AVAILABLE")
            db.add(historical)
            db.flush()
            db.add(RequirementDocument(firm_id=current.firm_id, request_id=previous.id, submission_id=sub.id, document_id=historical.id, document_type="OTHER"))
            history_id = str(historical.id)
            other_client = db.scalar(select(Client).where(Client.id != current.client_id))
            other_firm = Firm(name="Isolated firm")
            db.add(other_firm)
            db.flush()
            other_user = User(firm_id=other_firm.id, email="isolated@example.com", name="Isolated", password_hash="test-only")
            foreign_client = Client(firm_id=other_firm.id, code="ISOLATED", legal_name="Isolated client")
            db.add_all([other_user, foreign_client])
            db.flush()
            db.add(FirmMember(firm_id=other_firm.id, user_id=other_user.id, role="ACCOUNTANT"))
            db.flush()
            excluded_ids = []
            for customer, owner in ((other_client, current.created_by), (foreign_client, other_user.id)):
                foreign_request = CollectionRequest(firm_id=customer.firm_id, client_id=customer.id, period=date(2026, 8, 1), due_at=current.due_at, status="CLOSED", created_by=owner, assignee_id=owner)
                db.add(foreign_request)
                db.flush()
                foreign_submission = Submission(firm_id=customer.firm_id, request_id=foreign_request.id, round_no=1, status="SUBMITTED", created_by=owner)
                foreign_doc = Document(firm_id=customer.firm_id, client_id=customer.id, uploader_id=owner, original_name="B03 private.pdf", content_type=source.content_type, size_bytes=source.size_bytes, sha256=source.sha256, storage_key=source.storage_key, status="AVAILABLE")
                db.add_all([foreign_submission, foreign_doc])
                db.flush()
                db.add(RequirementDocument(firm_id=customer.firm_id, request_id=foreign_request.id, submission_id=foreign_submission.id, document_id=foreign_doc.id, document_type="OTHER"))
                excluded_ids.append(foreign_doc.id)
            db.flush()
            assert not authorized_documents(db, current, excluded_ids)
            run = db.scalar(select(AIRun))
            run.status, run.locked_by, run.locked_until = "PROCESSING", "dead-worker", datetime.now(UTC) - timedelta(seconds=1)
        def agent(body):
            result = response(body)
            if not body.turn:
                result.update(findings=[], search={"action": "SEARCH_HISTORY", "requirement_id": str(body.requirements[0].id), "query": "B03"})
            else:
                assert [str(d.document_id) for d in body.documents if d.scope == "HISTORY"] == [history_id]
            return result
        monkeypatch.setattr("app.review_analysis.call_agent", agent)
        assert process_review() and process_review()
        result = client.get(f"/api/v1/collection-requests/{records['request']}/review-runs", headers=staff).json()[0]
        assert result["status"] == "SUCCEEDED" and result["searches"] == [{"action": "SEARCH_HISTORY", "count": 1}]
        assert next(d for d in result["documents"] if d["id"] == history_id)["scope"] == "HISTORY"


def test_off_mode_never_queues_review(records):
    with SessionLocal.begin() as db:
        db.get(CollectionRequest, records["request"]).ai_mode = "OFF"
    with TestClient(app) as client:
        portal = auth_headers(client, "client@example.com", records["password"])
        upload(client, portal, records["request"], records["required"])
        process_next_document()
        result = client.post(f"/api/v1/portal/collection-requests/{records['request']}/submit", headers=portal)
        assert result.status_code == 200 and result.json()["review_status"] == "AWAITING_ACCOUNTANT"
        assert not process_review()
