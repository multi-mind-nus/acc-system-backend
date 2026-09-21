import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app.auth import Principal, current_principal, ensure_client_access
from app.config import settings
from app.db import DbSession
from app.errors import APIError
from app.models import (
    Client,
    CollectionRequest,
    Document,
    Requirement,
    RequirementDocument,
    Submission,
    User,
    WorkflowEvent,
)
from app.portal_schemas import (
    ClassificationItemOut,
    ClassificationOut,
    ClassificationRequest,
    PortalCollectionDetailOut,
    PortalCollectionListOut,
    PortalCollectionSummaryOut,
    PortalDocumentOut,
    PortalRequirementOut,
    PortalSubmissionOut,
    PortalUploadOut,
)

router = APIRouter(prefix="/api/v1/portal")
PortalUser = Annotated[Principal, Depends(current_principal)]

ALLOWED_FILES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}
TYPE_KEYWORDS = {
    "BANK_STATEMENT": ("bank", "statement", "对账单", "流水"),
    "SALES_INVOICE": ("sales", "invoice", "销售", "发票"),
    "PURCHASE_INVOICE": ("purchase", "supplier", "vendor", "采购", "供应商", "发票"),
    "RECEIPT": ("receipt", "收据", "小票"),
    "PAYMENT_PLATFORM_REPORT": ("stripe", "paypal", "settlement", "平台", "结算"),
    "LOAN_STATEMENT": ("loan", "贷款"),
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
    document: Document, link: RequirementDocument | None = None, *, duplicate=False
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
        ready_count=sum(requirement.required and requirement.id in ready_ids for requirement in requirements),
    )


def _detail(db, item: CollectionRequest) -> PortalCollectionDetailOut:
    requirements = list(db.scalars(
        select(Requirement).where(Requirement.request_id == item.id).order_by(Requirement.position)
    ))
    latest = _submission(db, item.id)
    rows = _links(db, latest.id if latest else None)
    by_requirement: dict[UUID | None, list[PortalDocumentOut]] = {}
    for link, document in rows:
        by_requirement.setdefault(link.requirement_id, []).append(_document_out(document, link))
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
def list_collections(db: DbSession, principal: PortalUser):
    client_ids = [client.id for member, client in principal.client_memberships]
    items = list(db.scalars(
        select(CollectionRequest)
        .where(
            CollectionRequest.firm_id == principal.firm.id,
            CollectionRequest.client_id.in_(client_ids),
            CollectionRequest.status != "DRAFT",
        )
        .order_by(CollectionRequest.due_at.desc())
    )) if client_ids else []
    return PortalCollectionListOut(items=[_summary(db, item) for item in items], total=len(items))


@router.get("/collection-requests/{request_id}", response_model=PortalCollectionDetailOut)
def get_collection(request_id: UUID, db: DbSession, principal: PortalUser):
    return _detail(db, _load_request(db, principal, request_id))


@router.post(
    "/collection-requests/{request_id}/classify",
    response_model=ClassificationOut,
)
def classify_documents(
    request_id: UUID,
    body: ClassificationRequest,
    db: DbSession,
    principal: PortalUser,
):
    item = _load_request(db, principal, request_id)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "This submission can no longer be changed")
    requirements = list(db.scalars(
        select(Requirement).where(Requirement.request_id == item.id).order_by(Requirement.position)
    ))
    results = []
    for index, file in enumerate(body.files):
        name = file.name.casefold()
        expected = ALLOWED_FILES.get(Path(name).suffix)
        invalid = (
            file.size_bytes == 0
            or file.size_bytes > settings.max_upload_bytes
            or expected is None
            or file.content_type != expected[0]
        )
        match = None if invalid else next((requirement for requirement in requirements if any(
            keyword in name for keyword in TYPE_KEYWORDS.get(requirement.type, ())
        )), None)
        # ponytail: deterministic placeholder; replace this endpoint body when an AI provider is connected.
        results.append(ClassificationItemOut(
            index=index,
            category="INVALID" if invalid else "REQUIREMENT" if match else "OTHER",
            requirement_id=match.id if match else None,
            confidence=0.99 if invalid else 0.9 if match else 0.55,
        ))
    return ClassificationOut(items=results)


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
    response_model=PortalUploadOut,
    status_code=202,
)
async def upload_document(
    request_id: UUID,
    db: DbSession,
    principal: PortalUser,
    file: Annotated[UploadFile, File()],
    requirement_id: Annotated[UUID | None, Form()] = None,
):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "This submission can no longer be changed")
    requirement = None
    if requirement_id:
        requirement = db.scalar(select(Requirement).where(
            Requirement.id == requirement_id,
            Requirement.request_id == item.id,
        ))
        if requirement is None:
            raise APIError(404, "REQUIREMENT_NOT_FOUND", "Requirement not found")

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
        draft = _draft_submission(db, item, principal)
        existing = db.scalar(select(Document).where(
            Document.firm_id == item.firm_id,
            Document.client_id == item.client_id,
            Document.sha256 == digest.hexdigest(),
            Document.status.in_(("QUARANTINED", "AVAILABLE")),
        ).order_by(Document.created_at))
        if existing:
            quarantine_path.unlink(missing_ok=True)
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
                document=_document_out(existing, link, duplicate=True),
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
        link = RequirementDocument(
            firm_id=item.firm_id, request_id=item.id, submission_id=draft.id,
            requirement_id=requirement_id, document_id=document.id,
            document_type=requirement.type if requirement else "OTHER",
        )
        db.add(link)
        db.commit()
        return PortalUploadOut(submission_id=draft.id, document=_document_out(document, link))
    except Exception:
        quarantine_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


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
    link, document, _, _ = _load_document_link(db, principal, document_id)
    return _document_out(document, link)


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
    link, document, _, _ = _load_link(db, principal, link_id)
    return _document_out(document, link)


@router.delete("/document-links/{link_id}", response_model=PortalDocumentOut)
def exclude_document(link_id: UUID, db: DbSession, principal: PortalUser):
    link, document, submission, item = _load_link(db, principal, link_id)
    if submission.status != "DRAFT" or item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "SUBMISSION_READ_ONLY", "Submitted documents cannot be removed")
    if link.excluded_at is None:
        link.excluded_at = datetime.now(UTC)
        link.excluded_by = principal.user.id
        db.commit()
    return _document_out(document, link)


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
def submit_collection(request_id: UUID, db: DbSession, principal: PortalUser):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("OPEN", "CHANGES_REQUESTED"):
        raise APIError(409, "INVALID_TRANSITION", "This request cannot be submitted")
    draft = _submission(db, item.id, "DRAFT")
    if draft is None:
        raise APIError(422, "REQUIRED_DOCUMENTS_MISSING", "Upload the required documents")
    rows = _links(db, draft.id)
    active = [(link, document) for link, document in rows if not link.excluded_at]
    if any(document.status == "QUARANTINED" for _, document in active):
        raise APIError(409, "DOCUMENTS_PROCESSING", "Wait for document processing to finish")
    available_ids = {
        link.requirement_id for link, document in active if document.status == "AVAILABLE"
    }
    missing = [str(requirement.id) for requirement in db.scalars(
        select(Requirement).where(
            Requirement.request_id == item.id,
            Requirement.required.is_(True),
        )
    ) if requirement.id not in available_ids]
    if missing:
        raise APIError(
            422, "REQUIRED_DOCUMENTS_MISSING",
            "Upload every required document before submitting",
            {"requirement_ids": missing},
        )
    now = datetime.now(UTC)
    draft.status = "SUBMITTED"
    draft.submitted_at = now
    item.status = "IN_REVIEW"
    item.submitted_at = now
    db.add(WorkflowEvent(
        firm_id=item.firm_id,
        request_id=item.id,
        actor_id=principal.user.id,
        event_type="SUBMITTED",
        payload={"round_no": draft.round_no},
    ))
    db.commit()
    return _detail(db, item)
