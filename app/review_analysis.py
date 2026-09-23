import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import or_, select

from app.analysis_schemas import ReviewRequest, validate_review
from app.config import settings
from app.db import SessionLocal
from app.models import AIRun, Client, CollectionRequest, Document, Requirement, RequirementDocument, Submission


def file_reference(doc, requirement_ids=(), scope="CURRENT"):
    return {"document_id": str(doc.id), "storage_key": doc.storage_key, "content_type": doc.content_type, "sha256": doc.sha256, "original_name": doc.original_name, "requirement_ids": [str(id) for id in requirement_ids], "scope": scope}


def enqueue_review(db, item, submission, user_id):
    if item.ai_mode == "OFF":
        return None
    db.flush()
    requirements = list(db.scalars(select(Requirement).where(Requirement.request_id == item.id).order_by(Requirement.position)))
    rows = db.execute(select(RequirementDocument, Document, Submission)
        .join(Document, Document.id == RequirementDocument.document_id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .where(RequirementDocument.request_id == item.id, Submission.status == "SUBMITTED", Submission.round_no <= submission.round_no)
        .order_by(Submission.round_no, RequirementDocument.created_at)).all()
    effective = {(link.requirement_id, doc.id): (link, doc) for link, doc, _ in rows}
    files = {}
    sizes = {}
    for link, doc in effective.values():
        if link.excluded_at or doc.status != "AVAILABLE" or doc.firm_id != item.firm_id or doc.client_id != item.client_id:
            continue
        ref = files.setdefault(doc.id, file_reference(doc))
        sizes[doc.id] = doc.size_bytes
        if link.requirement_id:
            ref["requirement_ids"].append(str(link.requirement_id))
    run = AIRun(id=uuid4(), firm_id=item.firm_id, request_id=item.id, submission_id=submission.id, purpose="REVIEW", status="QUEUED", requested_by=user_id)
    payload = {"schema_version": "1", "run_id": str(run.id), "purpose": "REVIEW", "turn": 0,
        "context": {"entity_name": db.get(Client, item.client_id).legal_name, "period": item.period.isoformat(), "submission_id": str(submission.id)},
        "documents": list(files.values()), "requirements": [{"id": str(req.id), "document_type": req.type, "title": req.title, "analysis_type": req.analysis_type, "required": req.required, "instructions": str(req.criteria.get("description", ""))[:4000]} for req in requirements], "search_history": []}
    run.input_snapshot = {"request": payload, "turn_attempts": 0, "search_results": []}
    if len(files) > 100 or sum(sizes.values()) > 100 * 1024 * 1024:
        run.status, run.error, run.finished_at = "FAILED", "REVIEW_TOO_LARGE", datetime.now(UTC)
    db.add(run)
    return run


def authorized_documents(db, item, ids):
    # Only previously submitted, active links. Staged/unsubmitted documents are not evidence.
    return {doc.id: doc for doc in db.scalars(select(Document).where(
        Document.id.in_(ids), Document.firm_id == item.firm_id, Document.client_id == item.client_id, Document.status == "AVAILABLE",
        select(RequirementDocument.id).join(Submission, Submission.id == RequirementDocument.submission_id)
        .join(CollectionRequest, CollectionRequest.id == RequirementDocument.request_id)
        .where(RequirementDocument.document_id == Document.id, RequirementDocument.excluded_at.is_(None), Submission.status == "SUBMITTED", CollectionRequest.status != "CANCELLED").exists(),
    ))}


def search_documents(db, item, search, known_ids):
    statement = (select(Document).join(RequirementDocument, RequirementDocument.document_id == Document.id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .join(CollectionRequest, CollectionRequest.id == RequirementDocument.request_id)
        .where(Document.firm_id == item.firm_id, Document.client_id == item.client_id, Document.status == "AVAILABLE",
            RequirementDocument.excluded_at.is_(None), Submission.status == "SUBMITTED", CollectionRequest.status != "CANCELLED",
            Document.id.not_in(known_ids)))
    if search.action == "SEARCH_HISTORY":
        statement = statement.where(CollectionRequest.period < item.period)
    else:
        statement = statement.where(CollectionRequest.id == item.id)
    if search.period:
        statement = statement.where(CollectionRequest.period == search.period)
    if search.document_type:
        statement = statement.where(or_(Document.document_type == search.document_type, RequirementDocument.document_type == search.document_type))
    if search.query:
        statement = statement.where(or_(Document.original_name.icontains(search.query, autoescape=True), Document.entity_name.icontains(search.query, autoescape=True), Document.extracted_data["invoice_number"].astext.icontains(search.query, autoescape=True), Document.extracted_data["counterparty"].astext.icontains(search.query, autoescape=True)))
    if search.amount:
        statement = statement.where(Document.extracted_data["amount"].astext == search.amount)
    if search.currency:
        statement = statement.where(Document.extracted_data["currency"].astext == search.currency)
    return list(db.scalars(statement.distinct().order_by(Document.created_at.desc(), Document.id).limit(min(20, 100-len(known_ids)))))


def amount_valid(relation):
    with localcontext() as context:
        context.prec = 80
        amounts = [Decimal(row.amount) for row in relation.operands]
        actual = sum(amounts) if relation.operation == "SUM" else amounts[0]
        if relation.operation == "SUBTRACT":
            actual -= sum(amounts[1:])
        elif relation.operation == "MULTIPLY":
            for value in amounts[1:]:
                actual *= value
        return actual == Decimal(relation.actual_amount) and actual - Decimal(relation.expected_amount) == Decimal(relation.difference)


def call_agent(body):
    request = Request(settings.agent_url + "/v1/analyze", data=body.model_dump_json().encode(), headers={"Content-Type": "application/json", "Idempotency-Key": f"{body.run_id}:{body.turn}"})
    with urlopen(request, timeout=180) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Response too large")
        return json.loads(raw)


def process_review():
    now, lease = datetime.now(UTC), str(uuid4())
    with SessionLocal.begin() as db:
        run = db.scalar(select(AIRun).where(AIRun.purpose == "REVIEW", AIRun.status.in_(("QUEUED", "PROCESSING")),
            or_(AIRun.next_attempt_at.is_(None), AIRun.next_attempt_at <= now), or_(AIRun.locked_until.is_(None), AIRun.locked_until <= now))
            .order_by(AIRun.created_at).with_for_update(skip_locked=True).limit(1))
        if run is None:
            return False
        if run.input_snapshot["turn_attempts"] >= 3:
            run.status, run.error, run.finished_at = "FAILED", "AGENT_UNAVAILABLE", now
            run.locked_by = run.locked_until = None
            return True
        run.status, run.locked_by, run.locked_until = "PROCESSING", lease, now + timedelta(seconds=210)
        run.attempts += 1
        snapshot = dict(run.input_snapshot)
        snapshot["turn_attempts"] += 1
        run.input_snapshot = snapshot
        run_id, request_id = run.id, run.request_id
    result, error = None, None
    try:
        body = ReviewRequest.model_validate(snapshot["request"])
        result = validate_review(body, call_agent(body))
    except HTTPError as exc:
        error = "AGENT_UNAVAILABLE" if exc.code >= 500 else "AGENT_INVALID_RESPONSE"
    except (URLError, TimeoutError, OSError):
        error = "AGENT_UNAVAILABLE"
    except ValueError:
        error = "AGENT_INVALID_RESPONSE"
    with SessionLocal.begin() as db:
        item = db.scalar(select(CollectionRequest).where(CollectionRequest.id == request_id).with_for_update())
        run = db.scalar(select(AIRun).where(AIRun.id == run_id).with_for_update())
        if run.status != "PROCESSING" or run.locked_by != lease:
            return True
        run.locked_by = run.locked_until = None
        latest = db.scalar(select(Submission.id).where(Submission.request_id == item.id, Submission.status == "SUBMITTED").order_by(Submission.round_no.desc()).limit(1))
        if item.status != "IN_REVIEW" or latest != run.submission_id:
            run.status, run.error, run.finished_at = "CANCELLED", "REVIEW_SUPERSEDED", datetime.now(UTC)
            return True
        if not error:
            docs = authorized_documents(db, item, [d.document_id for d in body.documents])
            if set(docs) != {d.document_id for d in body.documents}:
                error = "INVALID_EVIDENCE"
        if not error and result.search:
            if body.turn >= 3:
                error = "SEARCH_LIMIT_REACHED"
            else:
                found = search_documents(db, item, result.search, set(docs))
                request = body.model_dump(mode="json")
                request["turn"] += 1
                request["search_history"].append(result.search.model_dump(mode="json"))
                request["documents"].extend(file_reference(doc, scope="HISTORY" if result.search.action == "SEARCH_HISTORY" else "CURRENT") for doc in found)
                run.input_snapshot = {"request": request, "turn_attempts": 0, "search_results": snapshot["search_results"] + [{"action": result.search.action, "count": len(found)}]}
                run.status, run.next_attempt_at = "QUEUED", None
                run.model_version = result.model_version
                return True
        if error:
            run.error = error
            run.status = "QUEUED" if error == "AGENT_UNAVAILABLE" and snapshot["turn_attempts"] < 3 else "FAILED"
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2 ** snapshot["turn_attempts"])
        else:
            output = result.model_dump(mode="json")
            for finding, row in zip(result.findings, output["findings"], strict=True):
                threshold = item.ai_satisfy_threshold if finding.suggested_decision == "SATISFY" else item.ai_request_action_threshold
                row["manual_reasons"] = []
                if finding.action == "ESCALATE":
                    row["manual_reasons"].append("ESCALATED")
                if Decimal(str(finding.confidence)) < threshold:
                    row["manual_reasons"].append("LOW_CONFIDENCE")
                row["amounts_valid"] = all(amount_valid(relation) for relation in finding.amounts)
                if not row["amounts_valid"]:
                    row["manual_reasons"].append("AMOUNT_MISMATCH")
                # B6.3 records suggestions only. B6.4 adds guarded automatic decisions.
                row["manual_reasons"].append("MANUAL_REVIEW_REQUIRED")
            run.output, run.model_version, run.status, run.error = output, result.model_version, "SUCCEEDED", None
            for extraction in result.extractions:
                doc = docs[extraction.document_id]
                doc.entity_name, doc.period = extraction.entity_name, extraction.period
                # Do not overwrite confirmed upload classification with an unknown type.
                if extraction.document_type:
                    doc.document_type = extraction.document_type
                doc.extracted_data = extraction.model_dump(mode="json")
        if run.status in ("FAILED", "SUCCEEDED"):
            run.finished_at = datetime.now(UTC)
    return True
