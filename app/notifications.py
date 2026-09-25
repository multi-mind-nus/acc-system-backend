from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models import (
    ClientMember,
    CollectionRequest,
    FirmMember,
    Notification,
    User,
    WorkflowEvent,
)


CLIENT_EVENTS = {
    "PUBLISHED",
    "CHANGES_REQUESTED",
    "APPROVED",
    "APPROVAL_WITHDRAWN",
    "CLOSED",
    "CANCELLED",
}
STAFF_EVENTS = {"SUBMITTED", "AI_REVIEW_COMPLETED"}


def add_workflow_event(
    db,
    item: CollectionRequest,
    event_type: str,
    *,
    actor_id: UUID | None,
    payload: dict | None = None,
    created_at: datetime | None = None,
    notify: bool = True,
) -> WorkflowEvent:
    event = WorkflowEvent(
        id=uuid4(),
        firm_id=item.firm_id,
        request_id=item.id,
        actor_id=actor_id,
        actor_type="USER" if actor_id else "SYSTEM",
        event_type=event_type,
        payload=payload or {},
        created_at=created_at or datetime.now(UTC),
    )
    db.add(event)

    if not notify:
        return event

    if event_type in CLIENT_EVENTS:
        recipients = db.scalars(
            select(User.id)
            .join(
                ClientMember,
                (ClientMember.user_id == User.id)
                & (ClientMember.firm_id == User.firm_id),
            )
            .where(
                ClientMember.firm_id == item.firm_id,
                ClientMember.client_id == item.client_id,
                User.status == "ACTIVE",
            )
        )
    elif event_type in STAFF_EVENTS:
        recipients = db.scalars(
            select(User.id)
            .join(
                FirmMember,
                (FirmMember.user_id == User.id)
                & (FirmMember.firm_id == User.firm_id),
            )
            .where(
                FirmMember.firm_id == item.firm_id,
                User.status == "ACTIVE",
                (FirmMember.role == "FIRM_ADMIN") | (User.id == item.assignee_id),
            )
        )
    else:
        return event

    for user_id in set(recipients):
        if user_id != actor_id:
            db.add(Notification(
                firm_id=item.firm_id,
                user_id=user_id,
                event_id=event.id,
                created_at=event.created_at,
            ))
    return event
