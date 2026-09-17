from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.auth import (
    CurrentPrincipal,
    Principal,
    ensure_client_access,
    hash_token,
    normalize_email,
    opaque_token,
    require_firm_role,
    revoke_user_sessions,
)
from app.db import DbSession
from app.errors import APIError
from app.models import (
    AuditEvent,
    Client,
    ClientAssignment,
    ClientMember,
    FirmMember,
    User,
    UserInvite,
)
from app.schemas import (
    AssignmentOut,
    AssignmentUpdate,
    ClientCreate,
    ClientInvitationRequest,
    ClientListOut,
    ClientMemberOut,
    ClientMemberUpdate,
    ClientOut,
    ClientUpdate,
    InvitationOut,
    StaffInvitationRequest,
    UserListOut,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/api/v1")
FirmAdmin = Annotated[Principal, Depends(require_firm_role("FIRM_ADMIN"))]


def _audit(
    db,
    principal,
    action: str,
    target_type: str,
    target_id: UUID,
    details: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            firm_id=principal.firm.id,
            actor_id=principal.user.id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )


def _user_out(db, user: User) -> UserOut:
    firm_member = db.scalar(
        select(FirmMember).where(
            FirmMember.firm_id == user.firm_id,
            FirmMember.user_id == user.id,
        )
    )
    client_rows = db.execute(
        select(ClientMember, Client)
        .join(
            Client,
            (Client.id == ClientMember.client_id)
            & (Client.firm_id == ClientMember.firm_id),
        )
        .where(
            ClientMember.firm_id == user.firm_id,
            ClientMember.user_id == user.id,
        )
    ).all()
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        status=user.status,
        firm_role=firm_member.role if firm_member else None,
        client_roles=[
            {
                "client_id": client.id,
                "client_name": client.legal_name,
                "role": member.role,
            }
            for member, client in client_rows
        ],
        last_login_at=user.last_login_at,
    )


def _create_invitation(
    db,
    principal,
    *,
    email: str,
    scope: str,
    role: str,
    client_id: UUID | None = None,
) -> InvitationOut:
    normalized_email = normalize_email(email)
    now = datetime.now(UTC)
    existing_user = db.scalar(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    if existing_user and existing_user.firm_id != principal.firm.id:
        raise APIError(409, "EMAIL_UNAVAILABLE", "This email cannot be invited")
    if existing_user and scope == "FIRM" and db.get(
        FirmMember, (principal.firm.id, existing_user.id)
    ):
        raise APIError(409, "MEMBER_EXISTS", "This user is already a firm member")
    if existing_user and scope == "CLIENT" and db.get(
        ClientMember, (principal.firm.id, client_id, existing_user.id)
    ):
        raise APIError(409, "MEMBER_EXISTS", "This user is already a client member")

    active_invites = db.scalars(
        select(UserInvite).where(
            UserInvite.firm_id == principal.firm.id,
            UserInvite.client_id == client_id,
            func.lower(UserInvite.email) == normalized_email,
            UserInvite.accepted_at.is_(None),
            UserInvite.revoked_at.is_(None),
        )
    ).all()
    for old_invite in active_invites:
        old_invite.revoked_at = now

    raw_token = opaque_token()
    invite = UserInvite(
        firm_id=principal.firm.id,
        client_id=client_id,
        email=normalized_email,
        scope=scope,
        role=role,
        token_hash=hash_token(raw_token),
        invited_by=principal.user.id,
        expires_at=now + timedelta(days=7),
    )
    db.add(invite)
    db.flush()
    _audit(db, principal, "INVITATION_CREATED", "USER_INVITE", invite.id)
    db.commit()
    return InvitationOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        expires_at=invite.expires_at,
        token=raw_token,
    )


@router.get("/users", response_model=UserListOut)
def list_users(
    principal: FirmAdmin,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> UserListOut:
    base = select(User).where(User.firm_id == principal.firm.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    users = db.scalars(
        base.order_by(User.name, User.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return UserListOut(
        items=[_user_out(db, user) for user in users],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/users/invitations", response_model=InvitationOut, status_code=201)
def invite_staff(
    payload: StaffInvitationRequest,
    principal: FirmAdmin,
    db: DbSession,
) -> InvitationOut:
    return _create_invitation(
        db,
        principal,
        email=str(payload.email),
        scope="FIRM",
        role=payload.role,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    principal: FirmAdmin,
    db: DbSession,
) -> UserOut:
    user = db.scalar(
        select(User).where(User.id == user_id, User.firm_id == principal.firm.id)
    )
    if user is None:
        raise APIError(404, "USER_NOT_FOUND", "User not found")
    if payload.status == "DISABLED" and user.id == principal.user.id:
        raise APIError(400, "SELF_DISABLE_FORBIDDEN", "You cannot disable yourself")
    if payload.name is not None:
        user.name = payload.name.strip()
    if payload.status is not None and payload.status != user.status:
        if payload.status == "DISABLED":
            revoke_user_sessions(user.id)
        user.status = payload.status
    if payload.role is not None:
        member = db.scalar(
            select(FirmMember).where(
                FirmMember.firm_id == principal.firm.id,
                FirmMember.user_id == user.id,
            )
        )
        if member is None:
            raise APIError(400, "STAFF_MEMBERSHIP_REQUIRED", "User is not a staff member")
        if member.role == "FIRM_ADMIN" and payload.role != "FIRM_ADMIN":
            admin_count = db.scalar(
                select(func.count()).select_from(FirmMember).where(
                    FirmMember.firm_id == principal.firm.id,
                    FirmMember.role == "FIRM_ADMIN",
                )
            )
            if admin_count == 1:
                raise APIError(
                    400,
                    "LAST_ADMIN_REQUIRED",
                    "The firm must keep at least one administrator",
                )
        member.role = payload.role
    _audit(db, principal, "USER_UPDATED", "USER", user.id)
    db.commit()
    return _user_out(db, user)


@router.get("/clients", response_model=ClientListOut)
def list_clients(
    principal: CurrentPrincipal,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=100),
) -> ClientListOut:
    base = select(Client).where(Client.firm_id == principal.firm.id)
    if principal.firm_role == "ACCOUNTANT":
        base = base.join(
            ClientAssignment,
            (ClientAssignment.firm_id == Client.firm_id)
            & (ClientAssignment.client_id == Client.id),
        ).where(ClientAssignment.user_id == principal.user.id)
    elif principal.firm_role != "FIRM_ADMIN":
        client_ids = [client.id for _, client in principal.client_memberships]
        base = base.where(Client.id.in_(client_ids or [UUID(int=0)]))
    if search:
        query = f"%{search.strip()}%"
        base = base.where(
            or_(Client.code.ilike(query), Client.legal_name.ilike(query))
        )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    clients = db.scalars(
        base.order_by(Client.legal_name, Client.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ClientListOut(
        items=[ClientOut.model_validate(client) for client in clients],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/clients", response_model=ClientOut, status_code=201)
def create_client(
    payload: ClientCreate,
    principal: FirmAdmin,
    db: DbSession,
) -> ClientOut:
    client = Client(
        firm_id=principal.firm.id,
        code=payload.code.upper(),
        legal_name=payload.legal_name.strip(),
        base_currency=payload.base_currency,
        features=payload.features.model_dump(),
    )
    db.add(client)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise APIError(409, "CLIENT_CODE_EXISTS", "Client code already exists") from exc
    _audit(db, principal, "CLIENT_CREATED", "CLIENT", client.id)
    db.commit()
    return ClientOut.model_validate(client)


@router.get("/clients/{client_id}", response_model=ClientOut)
def get_client(
    client_id: UUID,
    principal: CurrentPrincipal,
    db: DbSession,
) -> ClientOut:
    return ClientOut.model_validate(ensure_client_access(db, principal, client_id))


@router.patch("/clients/{client_id}", response_model=ClientOut)
def update_client(
    client_id: UUID,
    payload: ClientUpdate,
    principal: FirmAdmin,
    db: DbSession,
) -> ClientOut:
    client = ensure_client_access(db, principal, client_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "features" and value is not None:
            value = payload.features.model_dump()
        setattr(client, key, value)
    _audit(db, principal, "CLIENT_UPDATED", "CLIENT", client.id)
    db.commit()
    return ClientOut.model_validate(client)


@router.get("/clients/{client_id}/members", response_model=list[ClientMemberOut])
def list_client_members(
    client_id: UUID,
    principal: CurrentPrincipal,
    db: DbSession,
) -> list[ClientMemberOut]:
    ensure_client_access(db, principal, client_id)
    rows = db.execute(
        select(ClientMember, User)
        .join(
            User,
            (User.id == ClientMember.user_id)
            & (User.firm_id == ClientMember.firm_id),
        )
        .where(
            ClientMember.firm_id == principal.firm.id,
            ClientMember.client_id == client_id,
        )
        .order_by(User.name, User.id)
    ).all()
    return [
        ClientMemberOut(
            user_id=user.id,
            email=user.email,
            name=user.name,
            status=user.status,
            role=member.role,
        )
        for member, user in rows
    ]


@router.patch(
    "/clients/{client_id}/members/{user_id}", response_model=ClientMemberOut | None
)
def update_client_member(
    client_id: UUID,
    user_id: UUID,
    payload: ClientMemberUpdate,
    principal: CurrentPrincipal,
    db: DbSession,
) -> ClientMemberOut | None:
    ensure_client_access(
        db, principal, client_id, client_roles=("CLIENT_ADMIN",)
    )
    member = db.get(
        ClientMember, (principal.firm.id, client_id, user_id)
    )
    if member is None:
        raise APIError(404, "CLIENT_MEMBER_NOT_FOUND", "Client member not found")
    user = db.scalar(
        select(User).where(User.id == user_id, User.firm_id == principal.firm.id)
    )
    if user is None:
        raise APIError(404, "CLIENT_MEMBER_NOT_FOUND", "Client member not found")
    if not payload.active:
        db.delete(member)
        _audit(db, principal, "CLIENT_MEMBER_REMOVED", "USER", user.id)
        db.commit()
        return None
    member.role = payload.role
    _audit(db, principal, "CLIENT_MEMBER_UPDATED", "USER", user.id)
    db.commit()
    return ClientMemberOut(
        user_id=user.id,
        email=user.email,
        name=user.name,
        status=user.status,
        role=member.role,
    )


@router.put("/clients/{client_id}/assignments", response_model=AssignmentOut)
def replace_assignments(
    client_id: UUID,
    payload: AssignmentUpdate,
    principal: FirmAdmin,
    db: DbSession,
) -> AssignmentOut:
    ensure_client_access(db, principal, client_id)
    user_ids = list(dict.fromkeys(payload.user_ids))
    if user_ids:
        valid_ids = set(
            db.scalars(
                select(FirmMember.user_id)
                .join(
                    User,
                    (User.id == FirmMember.user_id)
                    & (User.firm_id == FirmMember.firm_id),
                )
                .where(
                    FirmMember.firm_id == principal.firm.id,
                    FirmMember.user_id.in_(user_ids),
                    FirmMember.role == "ACCOUNTANT",
                    User.status == "ACTIVE",
                )
            ).all()
        )
        if valid_ids != set(user_ids):
            raise APIError(
                422,
                "INVALID_ASSIGNMENT",
                "Assignments must reference active accountants in this firm",
            )
    db.execute(
        delete(ClientAssignment).where(
            ClientAssignment.firm_id == principal.firm.id,
            ClientAssignment.client_id == client_id,
        )
    )
    db.add_all(
        ClientAssignment(
            firm_id=principal.firm.id,
            client_id=client_id,
            user_id=user_id,
        )
        for user_id in user_ids
    )
    _audit(
        db,
        principal,
        "CLIENT_ASSIGNMENTS_REPLACED",
        "CLIENT",
        client_id,
        {"count": len(user_ids)},
    )
    db.commit()
    return AssignmentOut(user_ids=user_ids)


@router.post(
    "/clients/{client_id}/invitations",
    response_model=InvitationOut,
    status_code=201,
)
def invite_client_member(
    client_id: UUID,
    payload: ClientInvitationRequest,
    principal: CurrentPrincipal,
    db: DbSession,
) -> InvitationOut:
    ensure_client_access(
        db, principal, client_id, client_roles=("CLIENT_ADMIN",)
    )
    return _create_invitation(
        db,
        principal,
        email=str(payload.email),
        scope="CLIENT",
        role=payload.role,
        client_id=client_id,
    )
