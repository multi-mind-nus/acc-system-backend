from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select, update

from app.auth import CurrentPrincipal
from app.db import DbSession
from app.errors import APIError
from app.models import Client, CollectionRequest, Notification, WorkflowEvent
from app.notification_schemas import NotificationListOut, NotificationOut


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def _query(principal):
    return (
        select(Notification, WorkflowEvent, CollectionRequest, Client.legal_name)
        .join(
            WorkflowEvent,
            (WorkflowEvent.id == Notification.event_id)
            & (WorkflowEvent.firm_id == Notification.firm_id),
        )
        .join(
            CollectionRequest,
            (CollectionRequest.id == WorkflowEvent.request_id)
            & (CollectionRequest.firm_id == WorkflowEvent.firm_id),
        )
        .join(
            Client,
            (Client.id == CollectionRequest.client_id)
            & (Client.firm_id == CollectionRequest.firm_id),
        )
        .where(
            Notification.firm_id == principal.firm.id,
            Notification.user_id == principal.user.id,
        )
    )


def _out(row) -> NotificationOut:
    notification, event, item, client_name = row
    return NotificationOut(
        id=notification.id,
        request_id=item.id,
        event_type=event.event_type,
        client_name=client_name,
        period=item.period,
        payload=event.payload,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get("", response_model=NotificationListOut)
def list_notifications(
    db: DbSession,
    principal: CurrentPrincipal,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = False,
):
    filters = (
        Notification.firm_id == principal.firm.id,
        Notification.user_id == principal.user.id,
    )
    unread_count = db.scalar(
        select(func.count(Notification.id)).where(
            *filters, Notification.read_at.is_(None)
        )
    ) or 0
    statement = _query(principal)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    total = db.scalar(
        select(func.count(Notification.id)).where(
            *filters,
            *([Notification.read_at.is_(None)] if unread_only else []),
        )
    ) or 0
    rows = db.execute(
        statement
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return NotificationListOut(
        items=[_out(row) for row in rows],
        total=total,
        unread_count=unread_count,
        page=page,
        page_size=page_size,
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: UUID,
    db: DbSession,
    principal: CurrentPrincipal,
):
    row = db.execute(
        _query(principal).where(Notification.id == notification_id)
    ).one_or_none()
    if row is None:
        raise APIError(404, "NOTIFICATION_NOT_FOUND", "Notification not found")
    notification = row[0]
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()
        row = db.execute(
            _query(principal).where(Notification.id == notification_id)
        ).one()
    return _out(row)


@router.post("/read-all", status_code=204)
def mark_all_notifications_read(db: DbSession, principal: CurrentPrincipal):
    db.execute(
        update(Notification)
        .where(
            Notification.firm_id == principal.firm.id,
            Notification.user_id == principal.user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return Response(status_code=204)
