import os
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app.auth import Principal, current_principal, ensure_client_access
from app.collection_schemas import CollectionFilterStatus
from app.classification_schemas import StagedUploadOut
from app.config import settings
from app.db import DbSession
from app.errors import APIError
from app.models import (
    Client,
    AIRun,
    CollectionRequest,
    Document,
    Requirement,
    RequirementDocument,
    Submission,
    User,
)
from app.notifications import add_workflow_event
from app.portal_schemas import (
    PortalCollectionDetailOut,
    PortalCollectionListOut,
    PortalCollectionSummaryOut,
    PortalDocumentOut,
    PortalRequirementOut,
    PortalSubmissionOut,
    PortalSubmitInput,
    PortalUploadOut,
)
from app.review_analysis import collection_review_status

router = APIRouter(prefix="/api/v1/portal")
PortalUser = Annotated[Principal, Depends(current_principal)]

ALLOWED_FILES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}


def _load_request(
    db, principal: Principal, request_id: UUID, *, lock: bool = False
) -> CollectionRequest:
    statement = select(CollectionRequest).where(
        CollectionRequest.id == request_id,
        CollectionRequest.firm_id == principal.firm.id,
        CollectionRequest.status != "DRAFT",
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(statement)
    if item is None:
        raise APIError(404, "COLLECTION_NOT_FOUND", "Collection request not found")
    ensure_client_access(
        db, principal, item.client_id,
        client_roles=("CLIENT_ADMIN", "CLIENT_SUBMITTER"),
    )
    return item


def _submission(db, request_id: UUID, status: str | None = None) -> Submission | None:
    statement = select(Submission).where(Submission.request_id == request_id)
    if status:
        statement = statement.where(Submission.status == status)
    return db.scalar(statement.order_by(Submission.round_no.desc()).limit(1))


def _draft_submission(db, item: CollectionRequest, principal: Principal) -> Submission:
    draft = _submission(db, item.id, "DRAFT")
    if draft:
        return draft
    round_no = (db.scalar(select(func.max(Submission.round_no)).where(
        Submission.request_id == item.id
    )) or 0) + 1
    draft = Submission(
        firm_id=item.firm_id,
        request_id=item.id,
        round_no=round_no,
        created_by=principal.user.id,
    )
    db.add(draft)
    db.flush()
    return draft


def _document_out(
    document: Document,
    link: RequirementDocument | None = None,
    *,
    duplicate=False,
    editable=False,
    counts_for_submission=False,
) -> PortalDocumentOut:
    if link is None:
        raise ValueError("A document response requires its submission link")
    return PortalDocumentOut(
        id=document.id,
        link_id=link.id,
        name=document.original_name,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        status="EXCLUDED" if link and link.excluded_at else document.status,
        failure_code=document.failure_code,
        duplicate=duplicate,
        editable=editable,
        counts_for_submission=counts_for_submission,
        created_at=document.created_at,
    )


def _links(db, submission_id: UUID | None):
    if submission_id is None:
        return []
    return db.execute(
        select(RequirementDocument, Document)
        .join(Document, Document.id == RequirementDocument.document_id)
        .where(RequirementDocument.submission_id == submission_id)
        .order_by(RequirementDocument.created_at, RequirementDocument.id)
    ).all()


def _effective_links(db, request_id: UUID):
    rows = db.execute(
        select(RequirementDocument, Document, Submission)
        .join(Document, Document.id == RequirementDocument.document_id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .where(RequirementDocument.request_id == request_id)
        .order_by(Submission.round_no, RequirementDocument.created_at, RequirementDocument.id)
    ).all()
    effective = {}
    for link, document, submission in rows:
        effective[(link.requirement_id, document.id)] = (link, document, submission)
    return list(effective.values())


def _requirement_editable(
    item: CollectionRequest, requirement: Requirement | None
) -> bool:
    return item.status == "OPEN" or (
        item.status == "CHANGES_REQUESTED"
        and requirement is not None
        and requirement.status in ("PENDING", "RECEIVED", "NEEDS_ACTION")
    )


def _carry_unreviewed_documents(db, item, draft, requirements):
    if item.status != "CHANGES_REQUESTED":
        return
    carry_ids = {value.id for value in requirements if value.status in ("PENDING", "RECEIVED")}
    current = {(link.requirement_id, document.id) for link, document in _links(db, draft.id) if not link.excluded_at}
    for link, document, submission in _effective_links(db, item.id):
        key = (link.requirement_id, document.id)
        if submission.id == draft.id or link.requirement_id not in carry_ids or link.excluded_at or document.status != "AVAILABLE" or key in current:
            continue
        db.add(RequirementDocument(
            firm_id=item.firm_id, request_id=item.id, submission_id=draft.id,
            requirement_id=link.requirement_id, document_id=document.id,
            document_type=link.document_type, relation=link.relation,
        ))
        current.add(key)
    db.flush()


def _summary(db, item: CollectionRequest) -> PortalCollectionSummaryOut:
    requirements = list(db.scalars(select(Requirement).where(
        Requirement.request_id == item.id
    )))
    latest = _submission(db, item.id)
    rows = _links(db, latest.id if latest else None)
    ready_ids = {
        link.requirement_id for link, document in rows
        if link.requirement_id and not link.excluded_at and document.status == "AVAILABLE"
    }
    return PortalCollectionSummaryOut(
        id=item.id,
        client_id=item.client_id,
        client_name=db.get(Client, item.client_id).legal_name,
        period=item.period,
        due_at=item.due_at,
        status=item.status,
        assignee_name=db.get(User, item.assignee_id).name,
        required_count=sum(requirement.required for requirement in requirements),
        ready_count=sum(
            requirement.required
            and requirement.status != "NEEDS_ACTION"
            and (
                requirement.status in ("SATISFIED", "WAIVED")
                or requirement.id in ready_ids
            )
            for requirement in requirements
        ),
        updated_at=item.updated_at,
        review_status=collection_review_status(db, item, requirements, latest),
    )


def _detail(db, item: CollectionRequest) -> PortalCollectionDetailOut:
    requirements = list(db.scalars(
        select(Requirement).where(Requirement.request_id == item.id).order_by(Requirement.position)
    ))
    latest = _submission(db, item.id)
    rows = _effective_links(db, item.id)
    requirements_by_id = {requirement.id: requirement for requirement in requirements}
    by_requirement: dict[UUID | None, list[PortalDocumentOut]] = {}
    current_draft_id = latest.id if latest and latest.status == "DRAFT" else None
    for link, document, _ in rows:
        if link.excluded_at:
            continue
        by_requirement.setdefault(link.requirement_id, []).append(_document_out(
            document,
            link,
            editable=_requirement_editable(
                item, requirements_by_id.get(link.requirement_id)
            ),
            counts_for_submission=link.submission_id == current_draft_id,
        ))
    summary = _summary(db, item)
    return PortalCollectionDetailOut(
        **summary.model_dump(),
        scope_note=item.scope_note,
        requirements=[PortalRequirementOut(
            id=requirement.id,
            type=requirement.type,
            title=requirement.title,
            required=requirement.required,
            criteria=requirement.criteria,
            status=requirement.status,
            client_message=requirement.client_message,
            documents=by_requirement.get(requirement.id, []),
        ) for requirement in requirements] + [PortalRequirementOut(
            id=item.id,
            type="OTHER",
            title="Other supporting documents",
            required=False,
            criteria={},
            status="PENDING",
            client_message=None,
            documents=by_requirement.get(None, []),
        )],
        submission=PortalSubmissionOut.model_validate(latest, from_attributes=True) if latest else None,
    )


@router.get("/collection-requests", response_model=PortalCollectionListOut)
def list_collections(
    db: DbSession,
    principal: PortalUser,
    client_id: UUID | None = None,
    period: date | None = None,
    status: CollectionFilterStatus | None = None,
    sort: Literal["due_at", "period", "updated_at"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
):
    client_ids = [client.id for member, client in principal.client_memberships]
    statement = select(CollectionRequest).where(
        CollectionRequest.firm_id == principal.firm.id,
        CollectionRequest.client_id.in_(client_ids),
        CollectionRequest.status != "DRAFT",
    )
    if client_id:
        statement = statement.where(CollectionRequest.client_id == client_id)
    if period:
        statement = statement.where(CollectionRequest.period == period)
    if status:
        statement = statement.where(CollectionRequest.status == ("IN_REVIEW" if status == "AI_PASSED" else status))
    sort_column = getattr(CollectionRequest, sort)
    statement = statement.order_by(
        sort_column.desc() if order == "desc" else sort_column.asc(),
        CollectionRequest.id,
    )
    summaries = [_summary(db, item) for item in db.scalars(statement)] if client_ids else []
    if status == "AI_PASSED":
        summaries = [item for item in summaries if item.review_status == "AI_PASSED"]
    return PortalCollectionListOut(items=summaries, total=len(summaries))


@router.get("/collection-requests/{request_id}", response_model=PortalCollectionDetailOut)
def get_collection(request_id: UUID, db: DbSession, principal: PortalUser):
    return _detail(db, _load_request(db, principal, request_id))


def _validate_file(name: str, content_type: str | None, header: bytes) -> tuple[str, str]:
    extension = Path(name).suffix.lower()
    expected = ALLOWED_FILES.get(extension)
    if expected is None:
        raise APIError(415, "FILE_TYPE_NOT_ALLOWED", "Upload a PDF, PNG, or JPEG file")
    expected_type, signature = expected
    if content_type != expected_type or not header.startswith(signature):
        raise APIError(415, "FILE_TYPE_MISMATCH", "The file content does not match its type")
    return extension, expected_type


@router.post(
    "/collection-requests/{request_id}/documents",
    response_model=PortalUploadOut | StagedUploadOut,
    status_code=202,
)
async def upload_document(
    request_id: UUID,
    db: DbSession,
    principal: PortalUser,
    file: Annotated[UploadFile, File()],
    requirement_id: Annotated[UUID | None, Form()] = None,
    classification_run_id: Annotated[UUID | None, Form()] = None,
):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "This submission can no longer be changed")
    run = None
    if classification_run_id:
        run = db.scalar(select(AIRun).where(
            AIRun.id == classification_run_id, AIRun.request_id == item.id,
            AIRun.firm_id == item.firm_id, AIRun.requested_by == principal.user.id,
            AIRun.purpose == "CLASSIFY",
        ).with_for_update())
        if run is None:
            raise APIError(404, "CLASSIFICATION_NOT_FOUND", "Classification session not found")
        if run.status != "DRAFT" or run.confirmed_at or requirement_id:
            raise APIError(409, "CLASSIFICATION_NOT_EDITABLE", "Classification session cannot be changed")
        if len(run.input_snapshot.get("documents", [])) >= 100:
            raise APIError(422, "TOO_MANY_FILES", "Select at most 100 files")
    requirement = None
    if requirement_id:
        requirement = db.scalar(select(Requirement).where(
            Requirement.id == requirement_id,
            Requirement.request_id == item.id,
        ))
        if requirement is None:
            raise APIError(404, "REQUIREMENT_NOT_FOUND", "Requirement not found")
    if not run and not _requirement_editable(item, requirement):
        raise APIError(
            409, "SUBMISSION_READ_ONLY", "This requirement cannot be changed"
        )

    document_id = uuid4()
    quarantine_path = Path(settings.quarantine_path) / f"{document_id}.part"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256()
    size = 0
    header = b""
    try:
        with quarantine_path.open("xb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise APIError(413, "FILE_TOO_LARGE", "The file exceeds the upload limit")
                if len(header) < 16:
                    header += chunk[:16 - len(header)]
                digest.update(chunk)
                target.write(chunk)
        if size == 0:
            raise APIError(422, "FILE_EMPTY", "The file is empty")
        _, content_type = _validate_file(file.filename or "", file.content_type, header)
        existing = db.scalar(select(Document).where(
            Document.firm_id == item.firm_id,
            Document.client_id == item.client_id,
            Document.sha256 == digest.hexdigest(),
            Document.status.in_(("QUARANTINED", "AVAILABLE")),
        ).order_by(Document.created_at))
        if existing:
            quarantine_path.unlink(missing_ok=True)
            if run:
                return _stage_document(db, run, existing)
            draft = _draft_submission(db, item, principal)
            link = db.scalar(select(RequirementDocument).where(
                RequirementDocument.submission_id == draft.id,
                RequirementDocument.requirement_id == requirement_id,
                RequirementDocument.document_id == existing.id,
                RequirementDocument.excluded_at.is_(None),
            ))
            if link is None:
                link = RequirementDocument(
                    firm_id=item.firm_id, request_id=item.id, submission_id=draft.id,
                    requirement_id=requirement_id, document_id=existing.id,
                    document_type=requirement.type if requirement else "OTHER",
                )
                db.add(link)
            db.commit()
            return PortalUploadOut(
                submission_id=draft.id,
                document=_document_out(
                    existing, link, duplicate=True, editable=True,
                    counts_for_submission=True,
                ),
            )

        document = Document(
            id=document_id,
            firm_id=item.firm_id,
            client_id=item.client_id,
            uploader_id=principal.user.id,
            original_name=Path(file.filename or "file").name[:255],
            content_type=content_type,
            size_bytes=size,
            sha256=digest.hexdigest(),
            storage_key=f"{item.firm_id}/{item.client_id}/{document_id}",
        )
        db.add(document)
        db.flush()
        if run:
            return _stage_document(db, run, document)
        draft = _draft_submission(db, item, principal)
        link = RequirementDocument(
            firm_id=item.firm_id, request_id=item.id, submission_id=draft.id,
            requirement_id=requirement_id, document_id=document.id,
            document_type=requirement.type if requirement else "OTHER",
        )
        db.add(link)
        db.commit()
        return PortalUploadOut(
            submission_id=draft.id,
            document=_document_out(
                document, link, editable=True, counts_for_submission=True,
            ),
        )
    except Exception:
        quarantine_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _stage_document(db, run: AIRun, document: Document):
    documents = list(run.input_snapshot.get("documents", []))
    if not any(value["document_id"] == str(document.id) for value in documents):
        documents.append({"document_id": str(document.id)})
    run.input_snapshot = {**run.input_snapshot, "documents": documents}
    db.commit()
    return StagedUploadOut(document_id=document.id)


def _load_document_link(db, principal: Principal, document_id: UUID):
    row = db.execute(
        select(RequirementDocument, Document, Submission, CollectionRequest)
        .join(Document, Document.id == RequirementDocument.document_id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .join(CollectionRequest, CollectionRequest.id == RequirementDocument.request_id)
        .where(
            Document.id == document_id,
            Document.firm_id == principal.firm.id,
        )
        .order_by(RequirementDocument.created_at.desc())
    ).first()
    if row is None:
        raise APIError(404, "DOCUMENT_NOT_FOUND", "Document not found")
    link, document, submission, item = row
    ensure_client_access(
        db, principal, item.client_id,
        client_roles=("CLIENT_ADMIN", "CLIENT_SUBMITTER"),
    )
    return link, document, submission, item


@router.get("/documents/{document_id}", response_model=PortalDocumentOut)
def get_document(document_id: UUID, db: DbSession, principal: PortalUser):
    link, document, submission, item = _load_document_link(db, principal, document_id)
    requirement = db.get(Requirement, link.requirement_id) if link.requirement_id else None
    return _document_out(
        document,
        link,
        editable=_requirement_editable(item, requirement),
        counts_for_submission=submission.status == "DRAFT",
    )


def _load_link(db, principal: Principal, link_id: UUID):
    row = db.execute(
        select(RequirementDocument, Document, Submission, CollectionRequest)
        .join(Document, Document.id == RequirementDocument.document_id)
        .join(Submission, Submission.id == RequirementDocument.submission_id)
        .join(CollectionRequest, CollectionRequest.id == RequirementDocument.request_id)
        .where(
            RequirementDocument.id == link_id,
            RequirementDocument.firm_id == principal.firm.id,
        )
    ).first()
    if row is None:
        raise APIError(404, "DOCUMENT_NOT_FOUND", "Document not found")
    link, document, submission, item = row
    ensure_client_access(
        db, principal, item.client_id,
        client_roles=("CLIENT_ADMIN", "CLIENT_SUBMITTER"),
    )
    return link, document, submission, item


@router.get("/document-links/{link_id}", response_model=PortalDocumentOut)
def get_document_link(link_id: UUID, db: DbSession, principal: PortalUser):
    link, document, submission, item = _load_link(db, principal, link_id)
    requirement = db.get(Requirement, link.requirement_id) if link.requirement_id else None
    return _document_out(
        document,
        link,
        editable=_requirement_editable(item, requirement),
        counts_for_submission=submission.status == "DRAFT",
    )


@router.delete("/document-links/{link_id}", response_model=PortalDocumentOut)
def exclude_document(link_id: UUID, db: DbSession, principal: PortalUser):
    link, document, submission, item = _load_link(db, principal, link_id)
    item = _load_request(db, principal, item.id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "Submitted documents cannot be removed")
    requirement = db.get(Requirement, link.requirement_id) if link.requirement_id else None
    if not _requirement_editable(item, requirement):
        raise APIError(
            409, "SUBMISSION_READ_ONLY", "This requirement cannot be changed"
        )
    if submission.status != "DRAFT":
        draft = _draft_submission(db, item, principal)
        current = db.scalar(select(RequirementDocument).where(
            RequirementDocument.submission_id == draft.id,
            RequirementDocument.requirement_id == link.requirement_id,
            RequirementDocument.document_id == document.id,
        ))
        if current is None:
            current = RequirementDocument(
                firm_id=item.firm_id,
                request_id=item.id,
                submission_id=draft.id,
                requirement_id=link.requirement_id,
                document_id=document.id,
                document_type=link.document_type,
                relation="REFERENCE",
            )
            db.add(current)
        link = current
    if link.excluded_at is None:
        link.excluded_at = datetime.now(UTC)
        link.excluded_by = principal.user.id
        db.commit()
    return _document_out(
        document, link, editable=True, counts_for_submission=True,
    )


@router.get("/document-links/{link_id}/download")
def download_document(link_id: UUID, db: DbSession, principal: PortalUser):
    link, document, _, _ = _load_link(db, principal, link_id)
    if link.excluded_at or document.status != "AVAILABLE":
        raise APIError(409, "DOCUMENT_UNAVAILABLE", "Document is not available for download")
    return Response(headers={
        "X-Accel-Redirect": f"/__protected_documents/{document.storage_key}",
        "Content-Type": document.content_type,
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.original_name)}",
        "X-Content-Type-Options": "nosniff",
    })


@router.post(
    "/collection-requests/{request_id}/submit",
    response_model=PortalCollectionDetailOut,
)
def submit_collection(
    request_id: UUID,
    db: DbSession,
    principal: PortalUser,
    body: PortalSubmitInput | None = None,
):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "INVALID_TRANSITION", "This request cannot be submitted")
    draft = _submission(db, item.id, "DRAFT")
    if draft is None:
        raise APIError(422, "REQUIRED_DOCUMENTS_MISSING", "Upload the required documents")
    requirements = list(db.scalars(
        select(Requirement).where(
            Requirement.request_id == item.id,
        )
    ))
    _carry_unreviewed_documents(db, item, draft, requirements)
    rows = _links(db, draft.id)
    active = [(link, document) for link, document in rows if not link.excluded_at]
    if any(document.status == "QUARANTINED" for _, document in active):
        raise APIError(409, "DOCUMENTS_PROCESSING", "Wait for document processing to finish")
    available_ids = {
        link.requirement_id for link, document in active if document.status == "AVAILABLE"
    }
    missing = [str(requirement.id) for requirement in requirements
               if requirement.required
               and requirement.status not in ("SATISFIED", "WAIVED")
               and requirement.id not in available_ids]
    if missing:
        raise APIError(
            422, "REQUIRED_DOCUMENTS_MISSING",
            "Upload every required document before submitting",
            {"requirement_ids": missing},
        )
    now = datetime.now(UTC)
    draft.status = "SUBMITTED"
    draft.note = body.note or None if body else None
    draft.submitted_at = now
    for requirement in requirements:
        if (
            requirement.id in available_ids
            and requirement.status not in ("SATISFIED", "WAIVED")
        ):
            requirement.status = "RECEIVED"
    item.status = "IN_REVIEW"
    item.submitted_at = now
    item.updated_at = now
    from app.review_analysis import enqueue_review
    enqueue_review(db, item, draft, principal.user.id)
    add_workflow_event(
        db,
        item,
        "SUBMITTED",
        actor_id=principal.user.id,
        payload={"round_no": draft.round_no},
        created_at=now,
    )
    db.commit()
    return _detail(db, item)
