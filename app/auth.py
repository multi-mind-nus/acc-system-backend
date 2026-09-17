from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated, Any
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import DbSession, redis_client
from app.errors import APIError
from app.models import Client, ClientAssignment, ClientMember, Firm, FirmMember, User
from app.schemas import ClientMembershipOut, FirmOut, PrincipalOut

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
algorithm = "HS256"


@dataclass
class Principal:
    user: User
    firm: Firm
    firm_role: str | None
    client_memberships: list[tuple[ClientMember, Client]]
    sid: UUID

    def to_schema(self) -> PrincipalOut:
        return PrincipalOut(
            id=self.user.id,
            email=self.user.email,
            name=self.user.name,
            firm=FirmOut(
                id=self.firm.id,
                name=self.firm.name,
                timezone=self.firm.timezone,
            ),
            firm_role=self.firm_role,
            client_memberships=[
                ClientMembershipOut(
                    client_id=client.id,
                    client_name=client.legal_name,
                    role=member.role,
                )
                for member, client in self.client_memberships
            ],
        )


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def opaque_token() -> str:
    return token_urlsafe(32)


def hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _encode_token(
    *, user_id: UUID, firm_id: UUID, sid: UUID, token_type: str, lifetime: timedelta
) -> tuple[str, str]:
    now = datetime.now(UTC)
    jti = str(uuid4())
    token = jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(sid),
            "firm_id": str(firm_id),
            "type": token_type,
            "iat": now,
            "exp": now + lifetime,
            "jti": jti,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=algorithm,
    )
    return token, jti


def decode_token(
    token: str, expected_type: str, *, verify_expiration: bool = True
) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"verify_exp": verify_expiration},
        )
        if payload.get("type") != expected_type:
            raise InvalidTokenError("Unexpected token type")
        for claim in ("sub", "sid", "firm_id", "jti"):
            if not payload.get(claim):
                raise InvalidTokenError(f"Missing {claim}")
        for claim in ("sub", "sid", "firm_id", "jti"):
            UUID(payload[claim])
        return payload
    except (InvalidTokenError, TypeError, ValueError) as exc:
        raise APIError(401, "INVALID_TOKEN", "Authentication token is invalid") from exc


def _session_key(sid: UUID | str) -> str:
    return f"auth:session:{sid}"


def _user_sessions_key(user_id: UUID | str) -> str:
    return f"auth:user:{user_id}:sessions"


def create_session(user: User) -> tuple[str, str, UUID]:
    sid = uuid4()
    refresh_lifetime = timedelta(days=settings.refresh_token_days)
    access_token, _ = _encode_token(
        user_id=user.id,
        firm_id=user.firm_id,
        sid=sid,
        token_type="access",
        lifetime=timedelta(minutes=settings.access_token_minutes),
    )
    refresh_token, refresh_jti = _encode_token(
        user_id=user.id,
        firm_id=user.firm_id,
        sid=sid,
        token_type="refresh",
        lifetime=refresh_lifetime,
    )
    ttl = int(refresh_lifetime.total_seconds())
    try:
        pipe = redis_client.pipeline()
        pipe.hset(
            _session_key(sid),
            mapping={
                "user_id": str(user.id),
                "firm_id": str(user.firm_id),
                "refresh_jti": refresh_jti,
            },
        )
        pipe.expire(_session_key(sid), ttl)
        pipe.sadd(_user_sessions_key(user.id), str(sid))
        pipe.expire(_user_sessions_key(user.id), ttl)
        pipe.execute()
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc
    return access_token, refresh_token, sid


def rotate_session(refresh_token: str) -> tuple[str, str, UUID, UUID, UUID]:
    payload = decode_token(refresh_token, "refresh")
    sid = UUID(payload["sid"])
    user_id = UUID(payload["sub"])
    firm_id = UUID(payload["firm_id"])
    lifetime = timedelta(days=settings.refresh_token_days)
    next_refresh, next_jti = _encode_token(
        user_id=user_id,
        firm_id=firm_id,
        sid=sid,
        token_type="refresh",
        lifetime=lifetime,
    )
    access_token, _ = _encode_token(
        user_id=user_id,
        firm_id=firm_id,
        sid=sid,
        token_type="access",
        lifetime=timedelta(minutes=settings.access_token_minutes),
    )
    script = """
    if redis.call('HGET', KEYS[1], 'refresh_jti') ~= ARGV[1] then
      return 0
    end
    redis.call('HSET', KEYS[1], 'refresh_jti', ARGV[2])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    redis.call('SADD', KEYS[2], ARGV[4])
    redis.call('EXPIRE', KEYS[2], ARGV[3])
    return 1
    """
    try:
        rotated = redis_client.eval(
            script,
            2,
            _session_key(sid),
            _user_sessions_key(user_id),
            payload["jti"],
            next_jti,
            int(lifetime.total_seconds()),
            str(sid),
        )
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc
    if rotated != 1:
        revoke_session(sid, user_id)
        raise APIError(401, "REFRESH_REUSED", "Refresh token is no longer valid")
    return access_token, next_refresh, sid, user_id, firm_id


def revoke_session(sid: UUID | str, user_id: UUID | str | None = None) -> None:
    try:
        if user_id is None:
            value = redis_client.hget(_session_key(sid), "user_id")
            user_id = value or None
        pipe = redis_client.pipeline()
        pipe.delete(_session_key(sid))
        if user_id is not None:
            pipe.srem(_user_sessions_key(user_id), str(sid))
        pipe.execute()
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc


def revoke_user_sessions(user_id: UUID) -> None:
    try:
        user_key = _user_sessions_key(user_id)
        session_ids = redis_client.smembers(user_key)
        pipe = redis_client.pipeline()
        for sid in session_ids:
            pipe.delete(_session_key(sid))
        pipe.delete(user_key)
        pipe.execute()
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc


def check_rate_limit(kind: str, identifier: str, limit: int) -> None:
    digest = sha256(identifier.encode()).hexdigest()
    key = f"rate:{kind}:{digest}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, settings.rate_limit_window_seconds)
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc
    if count > limit:
        raise APIError(429, "RATE_LIMITED", "Too many attempts; try again later")


def validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.frontend_origin.rstrip("/"):
        raise APIError(403, "INVALID_ORIGIN", "Request origin is not allowed")


def load_principal(
    db: Session, user_id: UUID, firm_id: UUID, sid: UUID
) -> Principal:
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.firm_id == firm_id,
            User.status == "ACTIVE",
        )
    )
    firm = db.scalar(
        select(Firm).where(Firm.id == firm_id, Firm.status == "ACTIVE")
    )
    if user is None or firm is None:
        raise APIError(401, "ACCOUNT_UNAVAILABLE", "Account is unavailable")

    firm_member = db.scalar(
        select(FirmMember).where(
            FirmMember.firm_id == firm_id, FirmMember.user_id == user_id
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
            ClientMember.firm_id == firm_id,
            ClientMember.user_id == user_id,
            Client.status == "ACTIVE",
        )
    ).all()
    if firm_member is None and not client_rows:
        raise APIError(403, "MEMBERSHIP_REQUIRED", "No active membership found")
    return Principal(
        user=user,
        firm=firm,
        firm_role=firm_member.role if firm_member else None,
        client_memberships=list(client_rows),
        sid=sid,
    )


def current_principal(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise APIError(401, "AUTHENTICATION_REQUIRED", "Authentication required")
    payload = decode_token(credentials.credentials, "access")
    sid = UUID(payload["sid"])
    try:
        session_user = redis_client.hget(_session_key(sid), "user_id")
    except RedisError as exc:
        raise APIError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from exc
    if session_user != payload["sub"]:
        raise APIError(401, "SESSION_REVOKED", "Session has been revoked")
    return load_principal(
        db, UUID(payload["sub"]), UUID(payload["firm_id"]), sid
    )


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_firm_role(*roles: str):
    def dependency(principal: CurrentPrincipal) -> Principal:
        if principal.firm_role not in roles:
            raise APIError(403, "FORBIDDEN", "You do not have permission")
        return principal

    return dependency


def lock_firm(db: Session, firm_id: UUID) -> Firm | None:
    # ponytail: serialize low-volume membership/invitation changes per firm;
    # replace with finer-grained locks only if admin write contention matters.
    return db.scalar(
        select(Firm).where(Firm.id == firm_id).with_for_update()
        .execution_options(populate_existing=True)
    )


def ensure_client_access(
    db: Session,
    principal: Principal,
    client_id: UUID,
    *,
    client_roles: tuple[str, ...] = (),
) -> Client:
    client = db.scalar(
        select(Client).where(
            Client.id == client_id,
            Client.firm_id == principal.firm.id,
        )
    )
    if client is None:
        raise APIError(404, "CLIENT_NOT_FOUND", "Client not found")
    if principal.firm_role == "FIRM_ADMIN":
        return client
    if principal.firm_role == "ACCOUNTANT" and not client_roles:
        assigned = db.get(
            ClientAssignment,
            (principal.firm.id, client_id, principal.user.id),
        )
        if assigned:
            return client
    for member, member_client in principal.client_memberships:
        if member_client.id == client_id and (
            not client_roles or member.role in client_roles
        ):
            return client
    raise APIError(404, "CLIENT_NOT_FOUND", "Client not found")
