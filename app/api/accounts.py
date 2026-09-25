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
    lock_firm,
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
    ClientBankAccount,
    ClientMember,
    FirmMember,
    User,
    UserInvite,
)
from app.schemas import (
    AssignmentOut,
    AssignmentUpdate,
    BankAccountCreate,
    BankAccountOut,
    BankAccountUpdate,
    ClientCreate,
    ClientInvitationRequest,
    ClientListOut,
    ClientMemberOut,
    ClientMemberUpdate,
    ClientOut,
    ClientUpdate,
    FirmRole,
    InvitationListItem,
    InvitationListOut,
    InvitationOut,
    MessageResponse,
    StaffInvitationRequest,
    UserListOut,
    UserOut,
    UserStatus,
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
    lock_firm(db, principal.firm.id)
    if client_id is not None:
        client = db.scalar(
            select(Client).where(
                Client.id == client_id, Client.firm_id == principal.firm.id,
            ).with_for_update().execution_options(populate_existing=True)
        )
        if client is None or client.status != "ACTIVE":
            raise APIError(409, "CLIENT_DISABLED", "Enable this client before inviting members")
    normalized_email = normalize_email(email)
    now = datetime.now(UTC)
    existing_user = db.scalar(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    if existing_user and existing_user.firm_id != principal.firm.id:
        raise APIError(409, "EMAIL_UNAVAILABLE", "This email cannot be invited")
    if existing_user and existing_user.status != "ACTIVE":
        raise APIError(409, "ACCOUNT_DISABLED", "Enable this account before inviting it")
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
        ).with_for_update()
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
    search: str | None = Query(default=None, max_length=100),
    status: UserStatus | None = None,
    role: FirmRole | None = None,
    staff_only: bool = False,
) -> UserListOut:
    base = select(User).where(User.firm_id == principal.firm.id)
    if staff_only or role:
        base = base.join(
            FirmMember,
            (FirmMember.firm_id == User.firm_id) & (FirmMember.user_id == User.id),
        )
    if role:
        base = base.where(FirmMember.role == role)
    if status:
        base = base.where(User.status == status)
    if search:
        query = f"%{search.strip()}%"
        base = base.where(or_(User.name.ilike(query), User.email.ilike(query)))
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
    lock_firm(db, principal.firm.id)
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
        if (
            member.role == "FIRM_ADMIN"
            and payload.role != "FIRM_ADMIN"
            and user.status == "ACTIVE"
        ):
            admin_count = db.scalar(
                select(func.count()).select_from(FirmMember).where(
                    FirmMember.firm_id == principal.firm.id,
                    FirmMember.role == "FIRM_ADMIN",
                    FirmMember.user_id.in_(
                        select(User.id).where(User.status == "ACTIVE")
                    ),
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
    status: UserStatus | None = None,
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
    if status:
        base = base.where(Client.status == status)
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
        industry=payload.industry,
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
    # The client row serializes demotions/removals, so concurrent requests cannot
    # each see a different "other administrator" and remove the last two.
    db.scalar(select(Client).where(Client.id == client_id).with_for_update())
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
    if member.role == "CLIENT_ADMIN" and (
        not payload.active or payload.role != "CLIENT_ADMIN"
    ):
        other_admin = db.scalar(
            select(ClientMember.user_id).join(
                User,
                (User.id == ClientMember.user_id)
                & (User.firm_id == ClientMember.firm_id),
            ).where(
                ClientMember.firm_id == principal.firm.id,
                ClientMember.client_id == client_id,
                ClientMember.role == "CLIENT_ADMIN",
                ClientMember.user_id != user_id,
                User.status == "ACTIVE",
            ).limit(1)
        )
        if other_admin is None:
            raise APIError(
                409, "LAST_CLIENT_ADMIN_REQUIRED",
                "The client must keep at least one active administrator",
            )
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


@router.get("/clients/{client_id}/assignments", response_model=AssignmentOut)
def get_assignments(
    client_id: UUID,
    principal: Annotated[Principal, Depends(require_firm_role("FIRM_ADMIN", "ACCOUNTANT"))],
    db: DbSession,
) -> AssignmentOut:
    ensure_client_access(db, principal, client_id)
    return AssignmentOut(user_ids=list(db.scalars(
        select(ClientAssignment.user_id).where(
            ClientAssignment.firm_id == principal.firm.id,
            ClientAssignment.client_id == client_id,
        ).order_by(ClientAssignment.user_id)
    ).all()))


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


def _list_invitations(db, principal, client_id, page, page_size) -> InvitationListOut:
    base = select(UserInvite).where(
        UserInvite.firm_id == principal.firm.id,
        UserInvite.client_id == client_id,
        UserInvite.scope == ("CLIENT" if client_id else "FIRM"),
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    invites = db.scalars(
        base.order_by(UserInvite.created_at.desc(), UserInvite.id)
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    now = datetime.now(UTC)
    return InvitationListOut(
        items=[InvitationListItem(
            id=invite.id,
            email=invite.email,
            role=invite.role,
            expires_at=invite.expires_at,
            created_at=invite.created_at,
            status=("ACCEPTED" if invite.accepted_at else
                    "REVOKED" if invite.revoked_at else
                    "EXPIRED" if invite.expires_at <= now else "PENDING"),
        ) for invite in invites],
        total=total, page=page, page_size=page_size,
    )


def _change_invitation(db, principal, client_id, invite_id, *, resend):
    lock_firm(db, principal.firm.id)
    if client_id is not None:
        ensure_client_access(db, principal, client_id, client_roles=("CLIENT_ADMIN",))
        db.scalar(select(Client).where(Client.id == client_id).with_for_update())
    invite = db.scalar(select(UserInvite).where(
        UserInvite.id == invite_id,
        UserInvite.firm_id == principal.firm.id,
        UserInvite.client_id == client_id,
        UserInvite.scope == ("CLIENT" if client_id else "FIRM"),
    ).with_for_update())
    if invite is None:
        raise APIError(404, "INVITATION_NOT_FOUND", "Invitation not found")
    if invite.accepted_at is not None:
        raise APIError(409, "INVITATION_ALREADY_ACCEPTED", "Invitation was already accepted")
    invite.revoked_at = invite.revoked_at or datetime.now(UTC)
    _audit(db, principal, "INVITATION_REVOKED", "USER_INVITE", invite.id)
    if resend:
        return _create_invitation(
            db, principal, email=invite.email, scope=invite.scope,
            role=invite.role, client_id=client_id,
        )
    db.commit()
    return MessageResponse(message="Invitation revoked")


@router.get("/users/invitations", response_model=InvitationListOut)
def list_staff_invitations(
    principal: FirmAdmin, db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> InvitationListOut:
    return _list_invitations(db, principal, None, page, page_size)


@router.post("/users/invitations/{invite_id}/revoke", response_model=MessageResponse)
def revoke_staff_invitation(
    invite_id: UUID, principal: FirmAdmin, db: DbSession,
) -> MessageResponse:
    return _change_invitation(db, principal, None, invite_id, resend=False)


@router.post("/users/invitations/{invite_id}/resend", response_model=InvitationOut)
def resend_staff_invitation(
    invite_id: UUID, principal: FirmAdmin, db: DbSession,
) -> InvitationOut:
    return _change_invitation(db, principal, None, invite_id, resend=True)


@router.get("/clients/{client_id}/invitations", response_model=InvitationListOut)
def list_client_invitations(
    client_id: UUID, principal: CurrentPrincipal, db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> InvitationListOut:
    ensure_client_access(db, principal, client_id, client_roles=("CLIENT_ADMIN",))
    return _list_invitations(db, principal, client_id, page, page_size)


@router.post(
    "/clients/{client_id}/invitations/{invite_id}/revoke", response_model=MessageResponse,
)
def revoke_client_invitation(
    client_id: UUID, invite_id: UUID, principal: CurrentPrincipal, db: DbSession,
) -> MessageResponse:
    return _change_invitation(db, principal, client_id, invite_id, resend=False)


@router.post(
    "/clients/{client_id}/invitations/{invite_id}/resend", response_model=InvitationOut,
)
def resend_client_invitation(
    client_id: UUID, invite_id: UUID, principal: CurrentPrincipal, db: DbSession,
) -> InvitationOut:
    return _change_invitation(db, principal, client_id, invite_id, resend=True)


@router.get("/clients/{client_id}/bank-accounts", response_model=list[BankAccountOut])
def list_bank_accounts(
    client_id: UUID, principal: CurrentPrincipal, db: DbSession,
) -> list[BankAccountOut]:
    ensure_client_access(db, principal, client_id)
    accounts = db.scalars(select(ClientBankAccount).where(
        ClientBankAccount.firm_id == principal.firm.id,
        ClientBankAccount.client_id == client_id,
    ).order_by(ClientBankAccount.bank, ClientBankAccount.account_last4, ClientBankAccount.id))
    return [BankAccountOut.model_validate(account) for account in accounts]


@router.post(
    "/clients/{client_id}/bank-accounts", response_model=BankAccountOut, status_code=201,
)
def create_bank_account(
    client_id: UUID, payload: BankAccountCreate, principal: FirmAdmin, db: DbSession,
) -> BankAccountOut:
    ensure_client_access(db, principal, client_id)
    account = ClientBankAccount(
        firm_id=principal.firm.id, client_id=client_id, **payload.model_dump(),
    )
    db.add(account)
    db.flush()
    _audit(db, principal, "BANK_ACCOUNT_CREATED", "CLIENT_BANK_ACCOUNT", account.id)
    db.commit()
    return BankAccountOut.model_validate(account)


@router.patch(
    "/clients/{client_id}/bank-accounts/{bank_id}", response_model=BankAccountOut,
)
def update_bank_account(
    client_id: UUID, bank_id: UUID, payload: BankAccountUpdate,
    principal: FirmAdmin, db: DbSession,
) -> BankAccountOut:
    ensure_client_access(db, principal, client_id)
    account = db.scalar(select(ClientBankAccount).where(
        ClientBankAccount.id == bank_id,
        ClientBankAccount.firm_id == principal.firm.id,
        ClientBankAccount.client_id == client_id,
    ))
    if account is None:
        raise APIError(404, "BANK_ACCOUNT_NOT_FOUND", "Bank account not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, key, value)
    _audit(db, principal, "BANK_ACCOUNT_UPDATED", "CLIENT_BANK_ACCOUNT", account.id)
    db.commit()
    return BankAccountOut.model_validate(account)
