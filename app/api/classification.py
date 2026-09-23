from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.portal import PortalUser, _draft_submission, _load_request, _requirement_editable
from app.classification_schemas import ClassificationRunOut, ConfirmClassification
from app.config import settings
from app.db import DbSession
from app.errors import APIError
from app.models import AIRun, Document, Requirement, RequirementDocument

router = APIRouter(prefix="/api/v1/portal/collection-requests")


def _load(db, principal, request_id, run_id, *, editable=False):
    item = _load_request(db, principal, request_id, lock=True)
    run = db.scalar(select(AIRun).where(
        AIRun.id == run_id, AIRun.request_id == item.id,
        AIRun.firm_id == item.firm_id, AIRun.requested_by == principal.user.id,
        AIRun.purpose == "CLASSIFY",
    ).with_for_update())
    if run is None:
        raise APIError(404, "CLASSIFICATION_NOT_FOUND", "Classification session not found")
    if editable and (item.status not in ("OPEN", "CHANGES_REQUESTED") or run.confirmed_at or run.status == "CANCELLED"):
        raise APIError(409, "CLASSIFICATION_NOT_EDITABLE", "Classification session cannot be changed")
    return item, run


def _documents(db, run):
    ids = [UUID(value["document_id"]) for value in run.input_snapshot.get("documents", [])]
    documents = {doc.id: doc for doc in db.scalars(select(Document).where(Document.id.in_(ids), Document.firm_id == run.firm_id))}
    return [documents[id] for id in ids if id in documents]


def _out(db, run):
    return ClassificationRunOut(
        id=run.id, status=run.status, provider=run.input_snapshot.get("provider", "DISABLED"),
        confirmed_at=run.confirmed_at, error=run.error,
        documents=[{"document_id": doc.id, "name": doc.original_name, "status": doc.status, "failure_code": doc.failure_code} for doc in _documents(db, run)],
        items=(run.output or {}).get("classifications", []),
    )


@router.post("/{request_id}/classification-runs", response_model=ClassificationRunOut, status_code=201)
def create_run(request_id: UUID, db: DbSession, principal: PortalUser):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "This request cannot be changed")
    run = AIRun(firm_id=item.firm_id, request_id=item.id, requested_by=principal.user.id, purpose="CLASSIFY", input_snapshot={
        "documents": [], "provider": "MANUAL" if item.ai_mode == "OFF" else settings.agent_classification_provider,
    })
    db.add(run)
    db.flush()
    result = _out(db, run)
    db.commit()
    return result


@router.get("/{request_id}/classification-runs/{run_id}", response_model=ClassificationRunOut)
def get_run(request_id: UUID, run_id: UUID, db: DbSession, principal: PortalUser):
    _, run = _load(db, principal, request_id, run_id)
    return _out(db, run)


@router.post("/{request_id}/classification-runs/{run_id}/start", response_model=ClassificationRunOut)
def start_run(request_id: UUID, run_id: UUID, db: DbSession, principal: PortalUser):
    item, run = _load(db, principal, request_id, run_id, editable=True)
    if run.status in ("QUEUED", "PROCESSING", "SUCCEEDED"):
        return _out(db, run)
    if run.status != "DRAFT" or not run.input_snapshot.get("documents"):
        raise APIError(409, "CLASSIFICATION_NOT_EDITABLE", "Upload files before starting classification")
    requirements = list(db.scalars(select(Requirement).where(Requirement.request_id == item.id).order_by(Requirement.position)))
    run.input_snapshot = {**run.input_snapshot, "requirements": [
        {"id": str(req.id), "document_type": req.type, "title": req.title}
        for req in requirements if _requirement_editable(item, req)
    ]}
    run.status = "QUEUED"
    run.next_attempt_at = datetime.now(UTC)
    result = _out(db, run)
    db.commit()
    return result


@router.post("/{request_id}/classification-runs/{run_id}/cancel", response_model=ClassificationRunOut)
def cancel_run(request_id: UUID, run_id: UUID, db: DbSession, principal: PortalUser):
    _, run = _load(db, principal, request_id, run_id)
    if run.confirmed_at:
        raise APIError(409, "CLASSIFICATION_ALREADY_CONFIRMED", "Files have already been added")
    run.status = "CANCELLED"
    run.finished_at = datetime.now(UTC)
    run.locked_by = None
    run.locked_until = None
    result = _out(db, run)
    db.commit()
    return result


@router.post("/{request_id}/classification-runs/{run_id}/confirm", response_model=ClassificationRunOut)
def confirm_run(request_id: UUID, run_id: UUID, body: ConfirmClassification, db: DbSession, principal: PortalUser):
    item, run = _load(db, principal, request_id, run_id)
    choices = sorted([choice.model_dump(mode="json") for choice in body.items], key=lambda choice: choice["document_id"])
    if run.confirmed_at:
        if choices != run.confirmation:
            raise APIError(409, "CLASSIFICATION_ALREADY_CONFIRMED", "Files have already been added")
        return _out(db, run)
    if run.status not in ("SUCCEEDED", "FAILED") or item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "CLASSIFICATION_NOT_EDITABLE", "Classification is not ready for confirmation")
    documents = {str(doc.id): doc for doc in _documents(db, run)}
    if set(documents) != {choice["document_id"] for choice in choices}:
        raise APIError(422, "INVALID_CLASSIFICATION", "Choose a category for every uploaded file")
    requirements = {req.id: req for req in db.scalars(select(Requirement).where(Requirement.request_id == item.id))}
    for choice in body.items:
        document = documents[str(choice.document_id)]
        if document.client_id != item.client_id:
            raise APIError(422, "INVALID_CLASSIFICATION", "Document is not available for this client")
        if choice.category == "INVALID":
            continue
        if document.status != "AVAILABLE":
            raise APIError(409, "DOCUMENT_NOT_READY", "Only safely scanned files can be added")
        requirement = requirements.get(choice.requirement_id) if choice.requirement_id else None
        if choice.category == "REQUIREMENT" and (not requirement or not _requirement_editable(item, requirement)):
            raise APIError(409, "SUBMISSION_READ_ONLY", "The selected requirement cannot be changed")
        if choice.category == "OTHER" and not _requirement_editable(item, None):
            raise APIError(409, "SUBMISSION_READ_ONLY", "Other documents cannot be changed in this round")
        draft = _draft_submission(db, item, principal)
        existing = db.scalar(select(RequirementDocument.id).where(
            RequirementDocument.submission_id == draft.id,
            RequirementDocument.document_id == document.id,
            RequirementDocument.requirement_id == choice.requirement_id,
            RequirementDocument.excluded_at.is_(None),
        ))
        if not existing:
            db.add(RequirementDocument(firm_id=item.firm_id, request_id=item.id, submission_id=draft.id, document_id=document.id, requirement_id=choice.requirement_id, document_type=requirement.type if requirement else "OTHER"))
        document.document_type = requirement.type if requirement else "OTHER"
        if requirement and requirement.status == "PENDING":
            requirement.status = "RECEIVED"
    run.confirmation = choices
    run.confirmed_at = datetime.now(UTC)
    result = _out(db, run)
    db.commit()
    return result


@router.post("/{request_id}/classification-runs/{run_id}/manual", response_model=ClassificationRunOut)
def manual_run(request_id: UUID, run_id: UUID, db: DbSession, principal: PortalUser):
    _, run = _load(db, principal, request_id, run_id, editable=True)
    if run.status == "DRAFT" or any(doc.status == "QUARANTINED" for doc in _documents(db, run)):
        raise APIError(409, "DOCUMENT_NOT_READY", "Wait for file scanning before choosing categories")
    run.status = "FAILED"
    run.error = "MANUAL_SELECTED"
    run.output = None
    run.locked_by = run.locked_until = None
    run.finished_at = datetime.now(UTC)
    result = _out(db, run)
    db.commit()
    return result
