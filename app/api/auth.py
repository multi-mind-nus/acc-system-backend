from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from app.auth import (
    CurrentPrincipal,
    check_rate_limit,
    create_session,
    decode_token,
    hash_password,
    hash_token,
    load_principal,
    normalize_email,
    opaque_token,
    revoke_session,
    revoke_user_sessions,
    rotate_session,
    validate_origin,
    verify_password,
)
from app.config import settings
from app.db import DbSession
from app.errors import APIError
from app.models import (
    AuditEvent,
    ClientMember,
    FirmMember,
    PasswordResetToken,
    User,
    UserInvite,
)
from app.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    InvitationAcceptRequest,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PasswordResetRequested,
    PrincipalOut,
)

router = APIRouter(prefix="/api/v1")
dummy_password_hash = hash_password("not-a-real-user-password")


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "refresh_token",
        token,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        "refresh_token",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/v1/auth",
    )


def _auth_response(access_token: str, principal) -> AuthResponse:
    return AuthResponse(
        access_token=access_token,
        expires_in=settings.access_token_minutes * 60,
        user=principal.to_schema(),
    )


@router.post("/auth/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> AuthResponse:
    email = normalize_email(str(payload.email))
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("login-ip", client_ip, settings.login_rate_limit)
    check_rate_limit("login-account", email, settings.login_rate_limit)
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    valid_password = verify_password(
        payload.password, user.password_hash if user else dummy_password_hash
    )
    if user is None or not valid_password or user.status != "ACTIVE":
        raise APIError(401, "INVALID_CREDENTIALS", "Email or password is incorrect")

    principal = load_principal(db, user.id, user.firm_id, uuid4())
    access_token, refresh_token, sid = create_session(user)
    principal.sid = sid
    user.last_login_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            firm_id=user.firm_id,
            actor_id=user.id,
            action="LOGIN",
            target_type="USER",
            target_id=user.id,
        )
    )
    db.commit()
    _set_refresh_cookie(response, refresh_token)
    return _auth_response(access_token, principal)


@router.post("/auth/refresh", response_model=AuthResponse)
def refresh(
    request: Request,
    response: Response,
    db: DbSession,
) -> AuthResponse:
    validate_origin(request)
    token = request.cookies.get("refresh_token")
    if not token:
        raise APIError(401, "REFRESH_REQUIRED", "Refresh token is required")
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("refresh", client_ip, settings.token_rate_limit)
    access_token, next_refresh, sid, user_id, firm_id = rotate_session(token)
    try:
        principal = load_principal(db, user_id, firm_id, sid)
    except APIError:
        revoke_session(sid, user_id)
        raise
    _set_refresh_cookie(response, next_refresh)
    return _auth_response(access_token, principal)


@router.post("/auth/logout", response_model=MessageResponse)
def logout(request: Request, response: Response, db: DbSession) -> MessageResponse:
    validate_origin(request)
    token = request.cookies.get("refresh_token")
    if token:
        try:
            payload = decode_token(token, "refresh", verify_expiration=False)
        except APIError:
            payload = None
        if payload is not None:
            revoke_session(payload["sid"], payload["sub"])
            db.add(
                AuditEvent(
                    firm_id=UUID(payload["firm_id"]),
                    actor_id=UUID(payload["sub"]),
                    action="LOGOUT",
                    target_type="USER",
                    target_id=UUID(payload["sub"]),
                )
            )
            db.commit()
    _clear_refresh_cookie(response)
    return MessageResponse(message="Signed out")


@router.get("/me", response_model=PrincipalOut)
def me(principal: CurrentPrincipal) -> PrincipalOut:
    return principal.to_schema()


@router.patch("/me/password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    principal: CurrentPrincipal,
    db: DbSession,
) -> MessageResponse:
    if not verify_password(payload.current_password, principal.user.password_hash):
        raise APIError(400, "CURRENT_PASSWORD_INVALID", "Current password is incorrect")
    principal.user.password_hash = hash_password(payload.new_password)
    db.add(
        AuditEvent(
            firm_id=principal.firm.id,
            actor_id=principal.user.id,
            action="PASSWORD_CHANGED",
            target_type="USER",
            target_id=principal.user.id,
        )
    )
    revoke_user_sessions(principal.user.id)
    db.commit()
    _clear_refresh_cookie(response)
    return MessageResponse(message="Password changed; sign in again")


@router.post("/auth/invitations/accept", response_model=MessageResponse)
def accept_invitation(
    payload: InvitationAcceptRequest,
    request: Request,
    db: DbSession,
) -> MessageResponse:
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("invite", client_ip, settings.token_rate_limit)
    now = datetime.now(UTC)
    invite = db.scalar(
        select(UserInvite)
        .where(UserInvite.token_hash == hash_token(payload.token))
        .with_for_update()
    )
    if (
        invite is None
        or invite.accepted_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= now
    ):
        raise APIError(400, "INVITATION_INVALID", "Invitation is invalid or expired")

    user = db.scalar(
        select(User).where(func.lower(User.email) == normalize_email(invite.email))
    )
    if user:
        if user.firm_id != invite.firm_id or not verify_password(
            payload.password, user.password_hash
        ):
            raise APIError(400, "INVITATION_INVALID", "Invitation cannot be accepted")
    else:
        user = User(
            firm_id=invite.firm_id,
            email=normalize_email(invite.email),
            password_hash=hash_password(payload.password),
            name=payload.name.strip(),
        )
        db.add(user)
        db.flush()

    if invite.scope == "FIRM":
        member = db.get(FirmMember, (invite.firm_id, user.id))
        if member:
            member.role = invite.role
        else:
            db.add(
                FirmMember(
                    firm_id=invite.firm_id, user_id=user.id, role=invite.role
                )
            )
    else:
        member = db.get(
            ClientMember, (invite.firm_id, invite.client_id, user.id)
        )
        if member:
            member.role = invite.role
        else:
            db.add(
                ClientMember(
                    firm_id=invite.firm_id,
                    client_id=invite.client_id,
                    user_id=user.id,
                    role=invite.role,
                )
            )
    invite.accepted_at = now
    db.add(
        AuditEvent(
            firm_id=invite.firm_id,
            actor_id=user.id,
            action="INVITATION_ACCEPTED",
            target_type="USER_INVITE",
            target_id=invite.id,
        )
    )
    db.commit()
    return MessageResponse(message="Invitation accepted")


@router.post("/auth/password-reset/request", response_model=PasswordResetRequested)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    db: DbSession,
) -> PasswordResetRequested:
    email = normalize_email(str(payload.email))
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("reset-ip", client_ip, settings.token_rate_limit)
    check_rate_limit("reset-account", email, settings.token_rate_limit)
    raw_token = opaque_token()
    user = db.scalar(
        select(User).where(func.lower(User.email) == email, User.status == "ACTIVE")
    )
    if user:
        now = datetime.now(UTC)
        old_tokens = db.scalars(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.consumed_at.is_(None),
            )
        ).all()
        for old_token in old_tokens:
            old_token.consumed_at = now
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=now + timedelta(hours=1),
            )
        )
        db.commit()
    return PasswordResetRequested(
        message="If the account exists, password reset instructions will be sent",
        development_token=raw_token if settings.environment != "production" else None,
    )


@router.post("/auth/password-reset/confirm", response_model=MessageResponse)
def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    request: Request,
    db: DbSession,
) -> MessageResponse:
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("reset-confirm", client_ip, settings.token_rate_limit)
    now = datetime.now(UTC)
    reset = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hash_token(payload.token))
        .with_for_update()
    )
    if reset is None or reset.consumed_at is not None or reset.expires_at <= now:
        raise APIError(400, "RESET_INVALID", "Reset token is invalid or expired")
    user = db.get(User, reset.user_id)
    if user is None or user.status != "ACTIVE":
        raise APIError(400, "RESET_INVALID", "Reset token is invalid or expired")
    user.password_hash = hash_password(payload.new_password)
    reset.consumed_at = now
    db.add(
        AuditEvent(
            firm_id=user.firm_id,
            actor_id=user.id,
            action="PASSWORD_RESET",
            target_type="USER",
            target_id=user.id,
        )
    )
    revoke_user_sessions(user.id)
    db.commit()
    return MessageResponse(message="Password reset; sign in with the new password")
