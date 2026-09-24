from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select, update

from app.api.collections import (
    _complete_idempotent,
    _events,
    _load_request,
    _load_requirement,
    _start_idempotent,
    IdempotencyKey,
)
from app.auth import Principal, ensure_client_access, require_firm_role
from app.db import DbSession
from app.errors import APIError
from app.models import (
    AIRun,
    Client,
    CollectionRequest,
    Document,
    Requirement,
    RequirementDocument,
    ReviewDecision,
    ReviewDecisionDocument,
    Submission,
    User,
    WorkflowEvent,
)
from app.review_schemas import (
    ReviewRunOut,
    ApprovalInput,
    EvidenceInput,
    RequirementReviewInput,
    ReviewCollectionOut,
    ReviewDecisionOut,
    ReviewDocumentOut,
    ReviewRequirementOut,
    ReviewSubmissionOut,
    TransitionInput,
)

router = APIRouter(prefix="/api/v1")
Staff = Annotated[
    Principal, Depends(require_firm_role("FIRM_ADMIN", "ACCOUNTANT"))
]


def _run_out(db, item, run):
    from app.review_analysis import authorized_documents
    refs = run.input_snapshot.get("request", {}).get("documents", [])
    documents = authorized_documents(db, item, [UUID(ref["document_id"]) for ref in refs])
    return ReviewRunOut(
        id=run.id, submission_id=run.submission_id, status=run.status, model_version=run.model_version,
        error=run.error, created_at=run.created_at, finished_at=run.finished_at, output=run.output,
        documents=[{"id": str(doc.id), "name": doc.original_name, "content_type": doc.content_type, "scope": ref["scope"]} for ref in refs if (doc := documents.get(UUID(ref["document_id"])))],
        searches=run.input_snapshot.get("search_results", []),
    )


@router.get("/collection-requests/{request_id}/review-runs", response_model=list[ReviewRunOut])
def get_review_runs(request_id: UUID, db: DbSession, principal: Staff):
    item = _load_request(db, principal, request_id)
    runs = db.scalars(select(AIRun).where(AIRun.request_id == item.id, AIRun.firm_id == item.firm_id, AIRun.purpose == "REVIEW").order_by(AIRun.created_at.desc(), AIRun.id))
    return [_run_out(db, item, run) for run in runs]


@router.post("/collection-requests/{request_id}/review-runs/{run_id}/retry", response_model=ReviewRunOut)
def retry_review(request_id: UUID, run_id: UUID, db: DbSession, principal: Staff):
    from app.review_analysis import enqueue_review
    item = _load_request(db, principal, request_id, lock=True)
    run = db.scalar(select(AIRun).where(AIRun.id == run_id, AIRun.request_id == item.id, AIRun.purpose == "REVIEW"))
    if run is None:
        raise APIError(404, "AI_RUN_NOT_FOUND", "Review not found")
    submission = db.scalar(select(Submission).where(Submission.request_id == item.id, Submission.status == "SUBMITTED").order_by(Submission.round_no.desc()).limit(1))
    if item.status != "IN_REVIEW" or item.ai_mode == "OFF" or submission.id != run.submission_id:
        raise APIError(409, "INVALID_TRANSITION", "Only the current submission can be analyzed")
    latest = db.scalar(select(AIRun).where(AIRun.submission_id == submission.id, AIRun.purpose == "REVIEW").order_by(AIRun.created_at.desc()).limit(1))
    if latest.status in ("QUEUED", "PROCESSING") or latest.id != run.id:
        return _run_out(db, item, latest)
    if run.status != "FAILED":
        raise APIError(409, "INVALID_TRANSITION", "Only failed reviews can be retried")
    new_run = enqueue_review(db, item, submission, principal.user.id)
    db.flush()
    result = _run_out(db, item, new_run)
    db.commit()
    return result


def _event(db, principal: Principal, item: CollectionRequest, kind: str, payload=None):
    now = datetime.now(UTC)
    db.execute(update(CollectionRequest).where(
        CollectionRequest.id == item.id,
    ).values(updated_at=now))
    db.add(WorkflowEvent(
        firm_id=item.firm_id,
        request_id=item.id,
        actor_id=principal.user.id,
        event_type=kind,
        payload=payload or {},
        created_at=now,
    ))


def _document_out(link, document, submission) -> ReviewDocumentOut:
    return ReviewDocumentOut(
        id=document.id,
        link_id=link.id,
        submission_id=submission.id,
        round_no=submission.round_no,
        name=document.original_name,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        status=document.status,
        document_type=link.document_type,
        relation=link.relation,
        created_at=document.created_at,
    )


def _review_detail(db, item: CollectionRequest) -> ReviewCollectionOut:
    requirements = list(db.scalars(
        select(Requirement)
        .where(Requirement.request_id == item.id)
        .order_by(Requirement.position)
    ))
    submissions = list(db.scalars(
        select(Submission)
        .where(Submission.request_id == item.id, Submission.status == "SUBMITTED")
        .order_by(Submission.round_no)
    ))
    document_rows = db.execute(
        select(RequirementDocument, Document, Submission)
        .join(Document, Document.id == RequirementDocument.document_id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .where(
            RequirementDocument.request_id == item.id,
            RequirementDocument.excluded_at.is_(None),
        )
        .order_by(Submission.round_no, RequirementDocument.created_at)
    ).all()
    documents: dict[UUID | None, list[ReviewDocumentOut]] = {}
    for link, document, submission in document_rows:
        documents.setdefault(link.requirement_id, []).append(
            _document_out(link, document, submission)
        )

    decisions: dict[UUID, list[ReviewDecisionOut]] = {}
    decision_rows = db.execute(
        select(ReviewDecision, User.name)
        .outerjoin(User, User.id == ReviewDecision.created_by)
        .where(ReviewDecision.requirement_id.in_([value.id for value in requirements]))
        .order_by(ReviewDecision.created_at.desc())
    ).all() if requirements else []
    evidence: dict[UUID, list[EvidenceInput]] = {}
    if decision_rows:
        ids = [decision.id for decision, _ in decision_rows]
        for link in db.scalars(select(ReviewDecisionDocument).where(
            ReviewDecisionDocument.decision_id.in_(ids)
        )):
            evidence.setdefault(link.decision_id, []).append(EvidenceInput(
                document_id=link.document_id,
                relation=link.relation,
            ))
    for decision, creator_name in decision_rows:
        decisions.setdefault(decision.requirement_id, []).append(ReviewDecisionOut(
            id=decision.id,
            submission_id=decision.submission_id,
            decision=decision.decision,
            issue_code=decision.issue_code,
            client_message=decision.client_message,
            internal_note=decision.internal_note,
            created_by=decision.created_by,
            source=decision.source,
            ai_run_id=decision.ai_run_id,
            created_by_name=creator_name or "System",
            created_at=decision.created_at,
            evidence=evidence.get(decision.id, []),
        ))

    return ReviewCollectionOut(
        id=item.id,
        client_id=item.client_id,
        client_name=db.get(Client, item.client_id).legal_name,
        period=item.period,
        due_at=item.due_at,
        status=item.status,
        version=item.version,
        assignee_name=db.get(User, item.assignee_id).name,
        requirements=[ReviewRequirementOut(
            id=requirement.id,
            type=requirement.type,
            title=requirement.title,
            required=requirement.required,
            status=requirement.status,
            version=requirement.version,
            issue_code=requirement.issue_code,
            client_message=requirement.client_message,
            internal_note=requirement.internal_note,
            documents=documents.get(requirement.id, []),
            decisions=decisions.get(requirement.id, []),
        ) for requirement in requirements],
        other_documents=documents.get(None, []),
        submissions=[ReviewSubmissionOut.model_validate(
            submission, from_attributes=True
        ) for submission in submissions],
        events=_events(db, item.id),
    )


def _check_request_version(item: CollectionRequest, version: int, db) -> None:
    if item.version != version:
        raise APIError(409, "VERSION_CONFLICT", "Request has changed", _review_detail(db, item))


def _unreviewed_requirements(db, item: CollectionRequest) -> list[UUID]:
    return list(db.scalars(select(Requirement.id).where(
        Requirement.request_id == item.id,
        Requirement.status.not_in(("NEEDS_ACTION", "SATISFIED", "WAIVED")),
    )))


def _finish(db, record, item: CollectionRequest) -> ReviewCollectionOut:
    db.flush()
    result = _review_detail(db, item)
    _complete_idempotent(record, result)
    db.commit()
    return result


@router.get(
    "/collection-requests/{request_id}/review",
    response_model=ReviewCollectionOut,
)
def get_review(request_id: UUID, db: DbSession, principal: Staff):
    return _review_detail(db, _load_request(db, principal, request_id))


@router.post(
    "/requirements/{requirement_id}/review",
    response_model=ReviewCollectionOut,
)
def review_requirement(
    requirement_id: UUID,
    body: RequirementReviewInput,
    db: DbSession,
    principal: Staff,
):
    requirement, item = _load_requirement(db, principal, requirement_id)
    if item.status != "IN_REVIEW":
        raise APIError(409, "INVALID_TRANSITION", "This request is not in review")
    if requirement.version != body.version:
        raise APIError(409, "VERSION_CONFLICT", "Requirement has changed", _review_detail(db, item))
    submission = db.scalar(select(Submission).where(
        Submission.id == body.submission_id,
        Submission.request_id == item.id,
        Submission.status == "SUBMITTED",
    ))
    if submission is None:
        raise APIError(422, "INVALID_SUBMISSION", "Submission does not belong to this request")

    from app.review_analysis import authorized_documents
    evidence = authorized_documents(db, item, [value.document_id for value in body.evidence])
    if len(evidence) != len(body.evidence):
        raise APIError(422, "INVALID_EVIDENCE", "Evidence does not belong to this client")
    now = datetime.now(UTC)
    requirement.status = {
        "SATISFY": "SATISFIED",
        "REQUEST_ACTION": "NEEDS_ACTION",
        "WAIVE": "WAIVED",
    }[body.decision]
    requirement.issue_code = body.issue_code if body.decision == "REQUEST_ACTION" else None
    requirement.client_message = body.client_message
    requirement.internal_note = body.internal_note
    requirement.reviewed_by = principal.user.id
    requirement.reviewed_at = now
    decision = ReviewDecision(
        firm_id=item.firm_id,
        requirement_id=requirement.id,
        submission_id=submission.id,
        decision=body.decision,
        issue_code=requirement.issue_code,
        client_message=body.client_message,
        internal_note=body.internal_note,
        created_by=principal.user.id,
    )
    db.add(decision)
    db.flush()
    for value in body.evidence:
        db.add(ReviewDecisionDocument(
            decision_id=decision.id,
            document_id=value.document_id,
            relation=value.relation,
        ))
    _event(db, principal, item, "REQUIREMENT_REVIEWED", {
        "requirement_id": str(requirement.id),
        "decision": body.decision,
    })
    db.commit()
    return _review_detail(db, item)


def _start_transition(db, principal, key, path, body, request_id):
    record, replay = _start_idempotent(
        db, principal, key, "POST", path, body.model_dump()
    )
    if replay:
        return record, replay, None
    return record, None, _load_request(db, principal, request_id, lock=True)


@router.post(
    "/collection-requests/{request_id}/request-changes",
    response_model=ReviewCollectionOut,
)
def request_changes(
    request_id: UUID, body: TransitionInput, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    path = f"/collection-requests/{request_id}/request-changes"
    record, replay, item = _start_transition(
        db, principal, idempotency_key, path, body, request_id
    )
    if replay:
        return replay
    _check_request_version(item, body.version, db)
    if item.status != "IN_REVIEW":
        raise APIError(409, "INVALID_TRANSITION", "Only a request in review can be returned")
    unreviewed = _unreviewed_requirements(db, item)
    if unreviewed:
        raise APIError(422, "REVIEW_INCOMPLETE", "Review every requirement before continuing", {
            "requirement_ids": [str(value) for value in unreviewed]
        })
    if not db.scalar(select(Requirement.id).where(
        Requirement.request_id == item.id,
        Requirement.status == "NEEDS_ACTION",
    )):
        raise APIError(422, "ACTION_REQUIRED", "Mark at least one requirement as needing action")
    item.status = "CHANGES_REQUESTED"
    _event(db, principal, item, "CHANGES_REQUESTED", {"reason": body.reason})
    return _finish(db, record, item)


@router.post(
    "/collection-requests/{request_id}/approve",
    response_model=ReviewCollectionOut,
)
def approve(
    request_id: UUID, body: ApprovalInput, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    path = f"/collection-requests/{request_id}/approve"
    record, replay, item = _start_transition(
        db, principal, idempotency_key, path, body, request_id
    )
    if replay:
        return replay
    _check_request_version(item, body.version, db)
    if item.status != "IN_REVIEW":
        raise APIError(409, "INVALID_TRANSITION", "Only a request in review can be approved")
    incomplete = list(db.scalars(select(Requirement.id).where(
        Requirement.request_id == item.id,
        Requirement.status.not_in(("SATISFIED", "WAIVED")),
    )))
    if incomplete:
        raise APIError(422, "REQUIREMENTS_INCOMPLETE", "Required items are incomplete", {
            "requirement_ids": [str(value) for value in incomplete]
        })
    item.status = "READY_FOR_BOOKKEEPING"
    item.approved_by = principal.user.id
    item.approved_at = datetime.now(UTC)
    _event(db, principal, item, "APPROVED")
    return _finish(db, record, item)


@router.post(
    "/collection-requests/{request_id}/reopen",
    response_model=ReviewCollectionOut,
)
def reopen(
    request_id: UUID, body: TransitionInput, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    if principal.firm_role != "FIRM_ADMIN":
        raise APIError(403, "FORBIDDEN", "Only a firm administrator can withdraw approval")
    path = f"/collection-requests/{request_id}/reopen"
    record, replay, item = _start_transition(
        db, principal, idempotency_key, path, body, request_id
    )
    if replay:
        return replay
    _check_request_version(item, body.version, db)
    if item.status != "READY_FOR_BOOKKEEPING":
        raise APIError(409, "INVALID_TRANSITION", "Only an approved request can be reopened")
    item.status = "IN_REVIEW"
    item.approved_by = None
    item.approved_at = None
    _event(db, principal, item, "APPROVAL_WITHDRAWN", {"reason": body.reason})
    return _finish(db, record, item)


@router.post(
    "/collection-requests/{request_id}/close",
    response_model=ReviewCollectionOut,
)
def close(
    request_id: UUID, body: TransitionInput, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    path = f"/collection-requests/{request_id}/close"
    record, replay, item = _start_transition(
        db, principal, idempotency_key, path, body, request_id
    )
    if replay:
        return replay
    _check_request_version(item, body.version, db)
    if item.status != "READY_FOR_BOOKKEEPING":
        raise APIError(409, "INVALID_TRANSITION", "Only an approved request can be closed")
    item.status = "CLOSED"
    _event(db, principal, item, "CLOSED", {"reason": body.reason})
    return _finish(db, record, item)


@router.get("/documents/{document_id}/download")
def download_document(document_id: UUID, db: DbSession, principal: Staff):
    document = db.scalar(select(Document).where(
        Document.id == document_id,
        Document.firm_id == principal.firm.id,
    ))
    if document is None:
        raise APIError(404, "DOCUMENT_NOT_FOUND", "Document not found")
    ensure_client_access(db, principal, document.client_id)
    if document.status != "AVAILABLE":
        raise APIError(409, "DOCUMENT_UNAVAILABLE", "Document is not available for download")
    return Response(headers={
        "X-Accel-Redirect": f"/__protected_documents/{document.storage_key}",
        "Content-Type": document.content_type,
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.original_name)}",
        "X-Content-Type-Options": "nosniff",
    })
