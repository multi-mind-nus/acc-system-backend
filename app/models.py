from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import metadata


class Base(DeclarativeBase):
    metadata = metadata


class Firm(Base):
    __tablename__ = "firms"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Singapore")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        UniqueConstraint("firm_id", "id"),
        Index("uq_users_email_lower", func.lower(email), unique=True),
    )


class FirmMember(Base):
    __tablename__ = "firm_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "user_id"], ["users.firm_id", "users.id"]
        ),
        CheckConstraint("role IN ('FIRM_ADMIN', 'ACCOUNTANT')"),
        UniqueConstraint("user_id"),
    )

    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        UniqueConstraint("firm_id", "code"),
        UniqueConstraint("firm_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    code: Mapped[str] = mapped_column(String(50))
    legal_name: Mapped[str] = mapped_column(String(200))
    base_currency: Mapped[str] = mapped_column(String(3), default="SGD")
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClientMember(Base):
    __tablename__ = "client_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        ForeignKeyConstraint(
            ["firm_id", "user_id"], ["users.firm_id", "users.id"]
        ),
        CheckConstraint("role IN ('CLIENT_ADMIN', 'CLIENT_SUBMITTER')"),
    )

    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"), primary_key=True)
    client_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClientBankAccount(Base):
    __tablename__ = "client_bank_accounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        CheckConstraint("account_last4 ~ '^[0-9]{4}$'"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        Index("ix_client_bank_accounts_firm_client", "firm_id", "client_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[UUID]
    bank: Mapped[str] = mapped_column(String(100))
    account_last4: Mapped[str] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class ClientAssignment(Base):
    __tablename__ = "client_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        ForeignKeyConstraint(
            ["firm_id", "user_id"],
            ["firm_members.firm_id", "firm_members.user_id"],
        ),
    )

    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"), primary_key=True)
    client_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserInvite(Base):
    __tablename__ = "user_invites"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        CheckConstraint("scope IN ('FIRM', 'CLIENT')"),
        CheckConstraint(
            "role IN ('FIRM_ADMIN', 'ACCOUNTANT', 'CLIENT_ADMIN', 'CLIENT_SUBMITTER')"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[UUID | None]
    email: Mapped[str] = mapped_column(String(320))
    scope: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID | None] = mapped_column(ForeignKey("firms.id"))
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[UUID | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
