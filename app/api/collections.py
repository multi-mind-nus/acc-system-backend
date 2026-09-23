import calendar
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, update

from app.auth import Principal, ensure_client_access, lock_firm, require_firm_role
from app.collection_schemas import (
    CancelRequest,
    CollectionCreate,
    CollectionDashboardOut,
    CollectionDetailOut,
    CollectionListOut,
    CollectionStatus,
    CollectionSummaryOut,
    CollectionUpdate,
    RequirementInput,
    RequirementOut,
    RequirementUpdate,
    VersionRequest,
    WorkflowEventOut,
    default_analysis_type,
)
from app.db import DbSession
from app.errors import APIError
from app.models import (
    Client,
    ClientAssignment,
    CollectionRequest,
    FirmMember,
    IdempotencyRecord,
    Requirement,
    User,
    WorkflowEvent,
)

router = APIRouter(prefix="/api/v1/collection-requests")
requirements_router = APIRouter(prefix="/api/v1/requirements")
Staff = Annotated[
    Principal, Depends(require_firm_role("FIRM_ADMIN", "ACCOUNTANT"))
]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
]


def _event(
    db, principal: Principal, request_id: UUID, event_type: str,
    payload: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    db.execute(update(CollectionRequest).where(
        CollectionRequest.id == request_id,
    ).values(updated_at=now))
    db.add(WorkflowEvent(
        firm_id=principal.firm.id,
        request_id=request_id,
        actor_id=principal.user.id,
        event_type=event_type,
        payload=payload or {},
        created_at=now,
    ))


def _requirements(db, request_id: UUID) -> list[Requirement]:
    return list(db.scalars(
        select(Requirement)
        .where(Requirement.request_id == request_id)
        .order_by(Requirement.position)
    ))


def _events(db, request_id: UUID) -> list[WorkflowEventOut]:
    rows = db.execute(
        select(WorkflowEvent, User.name)
        .outerjoin(User, User.id == WorkflowEvent.actor_id)
        .where(WorkflowEvent.request_id == request_id)
        .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.id.desc())
    ).all()
    return [WorkflowEventOut(
        id=event.id,
        actor_id=event.actor_id,
        actor_type=event.actor_type,
        actor_name=actor_name or "System",
        event_type=event.event_type,
        payload=event.payload,
        created_at=event.created_at,
    ) for event, actor_name in rows]


def _summary(
    item: CollectionRequest, client_name: str, assignee_name: str,
    requirement_count: int,
) -> CollectionSummaryOut:
    return CollectionSummaryOut(
        id=item.id,
        client_id=item.client_id,
        client_name=client_name,
        period=item.period,
        due_at=item.due_at,
        status=item.status,
        scope_note=item.scope_note,
        version=item.version,
        assignee_id=item.assignee_id,
        assignee_name=assignee_name,
        requirement_count=requirement_count,
        updated_at=item.updated_at,
    )


def _detail(db, item: CollectionRequest) -> CollectionDetailOut:
    client = db.get(Client, item.client_id)
    assignee = db.get(User, item.assignee_id)
    requirements = _requirements(db, item.id)
    return CollectionDetailOut(
        ai_mode=item.ai_mode,
        ai_satisfy_threshold=item.ai_satisfy_threshold,
        ai_request_action_threshold=item.ai_request_action_threshold,
        **_summary(
            item, client.legal_name, assignee.name, len(requirements)
        ).model_dump(),
        requirements=[RequirementOut.model_validate({
            "id": requirement.id,
            "origin": requirement.origin,
            "position": requirement.position,
            "type": requirement.type,
            "analysis_type": requirement.analysis_type,
            "title": requirement.title,
            "required": requirement.required,
            "criteria": requirement.criteria,
            "status": requirement.status,
            "version": requirement.version,
        }) for requirement in requirements],
        events=_events(db, item.id),
    )


def _load_request(
    db, principal: Principal, request_id: UUID, *, lock: bool = False,
) -> CollectionRequest:
    statement = select(CollectionRequest).where(
        CollectionRequest.id == request_id,
        CollectionRequest.firm_id == principal.firm.id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(statement)
    if item is None:
        raise APIError(404, "COLLECTION_NOT_FOUND", "Collection request not found")
    ensure_client_access(db, principal, item.client_id)
    return item


def _validate_assignee(
    db, principal: Principal, client_id: UUID, assignee_id: UUID,
) -> None:
    row = db.execute(
        select(FirmMember.role, User.status)
        .join(User, User.id == FirmMember.user_id)
        .where(
            FirmMember.firm_id == principal.firm.id,
            FirmMember.user_id == assignee_id,
        )
    ).one_or_none()
    if row is None or row.status != "ACTIVE":
        raise APIError(422, "INVALID_ASSIGNEE", "Assignee is unavailable")
    if row.role == "ACCOUNTANT" and db.get(
        ClientAssignment, (principal.firm.id, client_id, assignee_id)
    ) is None:
        raise APIError(422, "INVALID_ASSIGNEE", "Assignee is not assigned to this client")


def _request_hash(method: str, path: str, payload: dict) -> str:
    encoded = json.dumps(
        {"method": method, "path": path, "payload": jsonable_encoder(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode()).hexdigest()


def _start_idempotent(
    db, principal: Principal, key: str, method: str, path: str, payload: dict,
) -> tuple[IdempotencyRecord, dict | None]:
    lock_firm(db, principal.firm.id)
    digest = _request_hash(method, path, payload)
    record = db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.firm_id == principal.firm.id,
        IdempotencyRecord.actor_id == principal.user.id,
        IdempotencyRecord.key == key,
    ))
    now = datetime.now(UTC)
    if record and record.expires_at <= now:
        db.delete(record)
        db.flush()
        record = None
    if record:
        if record.request_hash != digest:
            raise APIError(
                409, "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was already used for a different request",
            )
        if record.status == "COMPLETED":
            return record, record.response_body
        raise APIError(409, "IDEMPOTENCY_IN_PROGRESS", "Request is still processing")
    record = IdempotencyRecord(
        firm_id=principal.firm.id,
        actor_id=principal.user.id,
        key=key,
        method=method,
        path=path,
        request_hash=digest,
        expires_at=now + timedelta(hours=24),
    )
    db.add(record)
    db.flush()
    return record, None


def _complete_idempotent(
    record: IdempotencyRecord, body: CollectionDetailOut, status_code: int = 200,
) -> None:
    record.status = "COMPLETED"
    record.status_code = status_code
    record.response_body = jsonable_encoder(body)


def _access_filter(statement, principal: Principal):
    if principal.firm_role == "ACCOUNTANT":
        assigned = select(ClientAssignment.client_id).where(
            ClientAssignment.firm_id == principal.firm.id,
            ClientAssignment.user_id == principal.user.id,
        )
        statement = statement.where(CollectionRequest.client_id.in_(assigned))
    return statement


def _list_statement(principal: Principal):
    requirement_count = (
        select(func.count(Requirement.id))
        .where(Requirement.request_id == CollectionRequest.id)
        .correlate(CollectionRequest)
        .scalar_subquery()
    )
    statement = (
        select(CollectionRequest, Client.legal_name, User.name, requirement_count)
        .join(Client, Client.id == CollectionRequest.client_id)
        .join(User, User.id == CollectionRequest.assignee_id)
        .where(CollectionRequest.firm_id == principal.firm.id)
    )
    return _access_filter(statement, principal)


@router.get("/dashboard", response_model=CollectionDashboardOut)
def dashboard(db: DbSession, principal: Staff):
    rows = db.execute(
        _list_statement(principal)
        .where(CollectionRequest.status.in_((
            "OPEN", "IN_REVIEW", "CHANGES_REQUESTED",
        )))
        .order_by(CollectionRequest.due_at)
    ).all()
    items = [_summary(*row) for row in rows]
    now = datetime.now(UTC)
    due_limit = now + timedelta(days=7)
    awaiting = [item for item in items if item.status == "IN_REVIEW" and item.assignee_id == principal.user.id]
    waiting = [item for item in items if item.status in ("OPEN", "CHANGES_REQUESTED")]
    due_soon = [item for item in waiting if now <= item.due_at <= due_limit]
    overdue = [item for item in waiting if item.due_at < now]
    return CollectionDashboardOut(
        awaiting_review=awaiting[:5],
        waiting_client=waiting[:5],
        due_soon=due_soon[:5],
        overdue=overdue[:5],
        counts={
            "awaiting_review": len(awaiting),
            "waiting_client": len(waiting),
            "due_soon": len(due_soon),
            "overdue": len(overdue),
        },
    )


@router.get("", response_model=CollectionListOut)
def list_collections(
    db: DbSession,
    principal: Staff,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    client_id: UUID | None = None,
    period: date | None = None,
    status: CollectionStatus | None = None,
    assignee_id: UUID | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    sort: Literal["due_at", "period", "created_at", "updated_at"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
):
    statement = _list_statement(principal)
    if client_id:
        statement = statement.where(CollectionRequest.client_id == client_id)
    if period:
        statement = statement.where(CollectionRequest.period == period)
    if status:
        statement = statement.where(CollectionRequest.status == status)
    if assignee_id:
        statement = statement.where(CollectionRequest.assignee_id == assignee_id)
    if due_from:
        statement = statement.where(CollectionRequest.due_at >= due_from)
    if due_to:
        statement = statement.where(CollectionRequest.due_at <= due_to)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    sort_column = getattr(CollectionRequest, sort)
    statement = statement.order_by(
        sort_column.desc() if order == "desc" else sort_column.asc(),
        CollectionRequest.id,
    ).offset((page - 1) * page_size).limit(page_size)
    return CollectionListOut(
        items=[_summary(*row) for row in db.execute(statement).all()],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=CollectionDetailOut, status_code=201)
def create_collection(
    body: CollectionCreate, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    record, replay = _start_idempotent(
        db, principal, idempotency_key, "POST", "/collection-requests",
        body.model_dump(),
    )
    if replay:
        return replay
    client = ensure_client_access(db, principal, body.client_id)
    if client.status != "ACTIVE":
        raise APIError(409, "CLIENT_DISABLED", "Client is disabled")
    if db.scalar(select(CollectionRequest.id).where(
        CollectionRequest.firm_id == principal.firm.id,
        CollectionRequest.client_id == body.client_id,
        CollectionRequest.period == body.period,
        CollectionRequest.status != "CANCELLED",
    )):
        raise APIError(409, "COLLECTION_EXISTS", "A request already exists for this period")
    assignee_id = body.assignee_id or principal.user.id
    _validate_assignee(db, principal, body.client_id, assignee_id)
    item = CollectionRequest(
        firm_id=principal.firm.id,
        client_id=body.client_id,
        period=body.period,
        due_at=body.due_at,
        scope_note=body.scope_note,
        ai_mode=body.ai_mode,
        ai_satisfy_threshold=body.ai_satisfy_threshold,
        ai_request_action_threshold=body.ai_request_action_threshold,
        created_by=principal.user.id,
        assignee_id=assignee_id,
    )
    db.add(item)
    db.flush()
    db.add_all([Requirement(
        firm_id=principal.firm.id,
        request_id=item.id,
        position=position,
        type=requirement.type,
        analysis_type=default_analysis_type(requirement.type),
        title=requirement.title,
        required=requirement.required,
        criteria=requirement.criteria,
    ) for position, requirement in enumerate(body.requirements)])
    _event(db, principal, item.id, "CREATED")
    db.flush()
    result = _detail(db, item)
    _complete_idempotent(record, result, 201)
    db.commit()
    return result


@router.get("/{request_id}", response_model=CollectionDetailOut)
def get_collection(request_id: UUID, db: DbSession, principal: Staff):
    return _detail(db, _load_request(db, principal, request_id))


@router.patch("/{request_id}", response_model=CollectionDetailOut)
def update_collection(
    request_id: UUID, body: CollectionUpdate, db: DbSession, principal: Staff,
):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status != "DRAFT":
        raise APIError(409, "COLLECTION_NOT_EDITABLE", "Only draft requests can be edited")
    if item.version != body.version:
        raise APIError(409, "VERSION_CONFLICT", "Request has changed", _detail(db, item))
    updates = body.model_dump(exclude_unset=True, exclude={"version"})
    assignee_id = updates.get("assignee_id")
    if assignee_id:
        _validate_assignee(db, principal, item.client_id, assignee_id)
    for field, value in updates.items():
        setattr(item, field, value)
    _event(db, principal, item.id, "UPDATED", {"fields": sorted(updates)})
    db.commit()
    return _detail(db, item)


@router.post("/{request_id}/publish", response_model=CollectionDetailOut)
def publish_collection(
    request_id: UUID, body: VersionRequest, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    path = f"/collection-requests/{request_id}/publish"
    record, replay = _start_idempotent(
        db, principal, idempotency_key, "POST", path, body.model_dump()
    )
    if replay:
        return replay
    item = _load_request(db, principal, request_id, lock=True)
    if item.version != body.version:
        raise APIError(409, "VERSION_CONFLICT", "Request has changed", _detail(db, item))
    if item.status != "DRAFT":
        raise APIError(409, "INVALID_TRANSITION", "Only draft requests can be published")
    if not _requirements(db, item.id):
        raise APIError(409, "REQUIREMENTS_REQUIRED", "Add at least one requirement")
    item.status = "OPEN"
    _event(db, principal, item.id, "PUBLISHED")
    db.flush()
    result = _detail(db, item)
    _complete_idempotent(record, result)
    db.commit()
    return result


@router.post("/{request_id}/cancel", response_model=CollectionDetailOut)
def cancel_collection(
    request_id: UUID, body: CancelRequest, db: DbSession, principal: Staff,
    idempotency_key: IdempotencyKey,
):
    path = f"/collection-requests/{request_id}/cancel"
    record, replay = _start_idempotent(
        db, principal, idempotency_key, "POST", path, body.model_dump()
    )
    if replay:
        return replay
    item = _load_request(db, principal, request_id, lock=True)
    if item.version != body.version:
        raise APIError(409, "VERSION_CONFLICT", "Request has changed", _detail(db, item))
    if item.status not in ("DRAFT", "OPEN", "IN_REVIEW", "CHANGES_REQUESTED"):
        raise APIError(409, "INVALID_TRANSITION", "Request cannot be cancelled")
    item.status = "CANCELLED"
    _event(db, principal, item.id, "CANCELLED", {"reason": body.reason})
    db.flush()
    result = _detail(db, item)
    _complete_idempotent(record, result)
    db.commit()
    return result


def _shift_due_at(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@router.post("/{request_id}/copy", response_model=CollectionDetailOut, status_code=201)
def copy_collection(
    request_id: UUID,
    period: date,
    db: DbSession,
    principal: Staff,
    idempotency_key: IdempotencyKey,
):
    if period.day != 1:
        raise APIError(422, "INVALID_PERIOD", "Period must be the first day of a month")
    path = f"/collection-requests/{request_id}/copy"
    record, replay = _start_idempotent(
        db, principal, idempotency_key, "POST", path, {"period": period}
    )
    if replay:
        return replay
    source = _load_request(db, principal, request_id, lock=True)
    if db.scalar(select(CollectionRequest.id).where(
        CollectionRequest.firm_id == principal.firm.id,
        CollectionRequest.client_id == source.client_id,
        CollectionRequest.period == period,
        CollectionRequest.status != "CANCELLED",
    )):
        raise APIError(409, "COLLECTION_EXISTS", "A request already exists for this period")
    month_delta = (period.year - source.period.year) * 12 + period.month - source.period.month
    assignee_id = source.assignee_id
    try:
        _validate_assignee(db, principal, source.client_id, assignee_id)
    except APIError:
        assignee_id = principal.user.id
        _validate_assignee(db, principal, source.client_id, assignee_id)
    item = CollectionRequest(
        firm_id=principal.firm.id,
        client_id=source.client_id,
        period=period,
        due_at=_shift_due_at(source.due_at, month_delta),
        scope_note=source.scope_note,
        ai_mode=source.ai_mode,
        ai_satisfy_threshold=source.ai_satisfy_threshold,
        ai_request_action_threshold=source.ai_request_action_threshold,
        created_by=principal.user.id,
        assignee_id=assignee_id,
    )
    db.add(item)
    db.flush()
    source_requirements = list(db.scalars(select(Requirement).where(
        Requirement.request_id == source.id,
        Requirement.origin == "INITIAL",
    ).order_by(Requirement.position)))
    db.add_all([Requirement(
        firm_id=principal.firm.id,
        request_id=item.id,
        position=requirement.position,
        type=requirement.type,
        analysis_type=default_analysis_type(requirement.type),
        title=requirement.title,
        required=requirement.required,
        criteria={key: value for key, value in requirement.criteria.items() if key != "target_transaction"},
    ) for requirement in source_requirements])
    _event(db, principal, item.id, "COPIED", {"source_request_id": str(source.id)})
    db.flush()
    result = _detail(db, item)
    _complete_idempotent(record, result, 201)
    db.commit()
    return result


@router.post("/{request_id}/requirements", response_model=RequirementOut, status_code=201)
def add_requirement(
    request_id: UUID, body: RequirementInput, db: DbSession, principal: Staff,
):
    item = _load_request(db, principal, request_id, lock=True)
    if item.status not in ("DRAFT", "IN_REVIEW"):
        raise APIError(409, "COLLECTION_NOT_EDITABLE", "Published requirements cannot be changed")
    follow_up = item.status == "IN_REVIEW"
    requirement = Requirement(
        firm_id=principal.firm.id,
        request_id=item.id,
        origin="FOLLOW_UP" if follow_up else "INITIAL",
        analysis_type=default_analysis_type(body.type),
        position=db.scalar(select(func.coalesce(func.max(Requirement.position), -1)).where(
            Requirement.request_id == item.id
        )) + 1,
        **body.model_dump(),
    )
    db.add(requirement)
    if follow_up:
        item.status = "CHANGES_REQUESTED"
        _event(db, principal, item.id, "FOLLOW_UP_ADDED", {"title": body.title})
    else:
        _event(db, principal, item.id, "REQUIREMENT_ADDED", {"title": body.title})
    db.commit()
    return requirement


@router.get("/{request_id}/events", response_model=list[WorkflowEventOut])
def list_events(request_id: UUID, db: DbSession, principal: Staff):
    item = _load_request(db, principal, request_id)
    return _events(db, item.id)


def _load_requirement(db, principal: Principal, requirement_id: UUID):
    requirement = db.scalar(select(Requirement).where(
        Requirement.id == requirement_id,
        Requirement.firm_id == principal.firm.id,
    ))
    if requirement is None:
        raise APIError(404, "REQUIREMENT_NOT_FOUND", "Requirement not found")
    item = _load_request(db, principal, requirement.request_id, lock=True)
    requirement = db.scalar(select(Requirement).where(
        Requirement.id == requirement_id,
        Requirement.firm_id == principal.firm.id,
    ).with_for_update().execution_options(populate_existing=True))
    return requirement, item


@requirements_router.patch("/{requirement_id}", response_model=RequirementOut)
def update_requirement(
    requirement_id: UUID, body: RequirementUpdate, db: DbSession, principal: Staff,
):
    requirement, item = _load_requirement(db, principal, requirement_id)
    if item.status != "DRAFT":
        raise APIError(409, "COLLECTION_NOT_EDITABLE", "Published requirements cannot be changed")
    if requirement.version != body.version:
        raise APIError(409, "VERSION_CONFLICT", "Requirement has changed")
    for field, value in body.model_dump(exclude={"version"}).items():
        setattr(requirement, field, value)
    requirement.analysis_type = default_analysis_type(body.type)
    _event(db, principal, item.id, "REQUIREMENT_UPDATED", {"requirement_id": str(requirement.id)})
    db.commit()
    return requirement


@requirements_router.delete("/{requirement_id}", status_code=204)
def delete_requirement(
    requirement_id: UUID, version: int, db: DbSession, principal: Staff,
):
    requirement, item = _load_requirement(db, principal, requirement_id)
    if item.status != "DRAFT":
        raise APIError(409, "COLLECTION_NOT_EDITABLE", "Published requirements cannot be deleted")
    if requirement.version != version:
        raise APIError(409, "VERSION_CONFLICT", "Requirement has changed")
    if db.scalar(select(func.count(Requirement.id)).where(
        Requirement.request_id == item.id
    )) <= 1:
        raise APIError(409, "REQUIREMENTS_REQUIRED", "Keep at least one requirement")
    db.delete(requirement)
    _event(db, principal, item.id, "REQUIREMENT_REMOVED", {"requirement_id": str(requirement.id)})
    db.commit()
