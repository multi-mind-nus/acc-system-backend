from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    industry: Mapped[str] = mapped_column(String(40), default="OTHER", server_default="OTHER")
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


class CollectionRequest(Base):
    __tablename__ = "collection_requests"
    __table_args__ = (
        CheckConstraint("ai_mode IN ('OFF', 'SUGGEST', 'AUTO_REVIEW')", name="ck_collection_ai_mode"),
        CheckConstraint("review_preference IN ('CAUTIOUS', 'STANDARD', 'EFFICIENT')", name="ck_collection_review_preference"),
        CheckConstraint("ai_satisfy_threshold BETWEEN 0.500 AND 1.000 AND ai_request_action_threshold BETWEEN 0.500 AND 1.000", name="ck_collection_ai_thresholds"),
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        ForeignKeyConstraint(
            ["firm_id", "assignee_id"],
            ["firm_members.firm_id", "firm_members.user_id"],
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'OPEN', 'IN_REVIEW', 'CHANGES_REQUESTED', "
            "'READY_FOR_BOOKKEEPING', 'CLOSED', 'CANCELLED')"
        ),
        Index(
            "ix_collection_requests_client_period",
            "firm_id",
            "client_id",
            "period",
        ),
        UniqueConstraint("firm_id", "id"),
        Index("ix_collection_requests_dashboard", "firm_id", "status", "due_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[UUID]
    period: Mapped[date] = mapped_column(Date)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    scope_note: Mapped[str | None] = mapped_column(Text)
    ai_mode: Mapped[str] = mapped_column(String(16), default="AUTO_REVIEW", server_default="AUTO_REVIEW")
    review_preference: Mapped[str] = mapped_column(String(16), default="STANDARD", server_default="STANDARD")
    ai_satisfy_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.980"), server_default="0.980")
    ai_request_action_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.980"), server_default="0.980")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[UUID]
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __mapper_args__ = {"version_id_col": version}


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (
        CheckConstraint("analysis_type IN ('DOCUMENT_REQUIREMENT_VALIDATION', 'BANK_TRANSACTION_RECONCILIATION')", name="ck_requirement_analysis_type"),
        ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        CheckConstraint("origin IN ('INITIAL', 'FOLLOW_UP')"),
        CheckConstraint(
            "status IN ('PENDING', 'RECEIVED', 'NEEDS_ACTION', 'SATISFIED', 'WAIVED')"
        ),
        CheckConstraint(
            "issue_code IS NULL OR issue_code IN ('MISSING', 'WRONG_PERIOD', "
            "'ENTITY_MISMATCH', 'UNREADABLE', 'INCOMPLETE', 'OTHER')"
        ),
        UniqueConstraint("firm_id", "id"),
        Index("ix_requirements_request", "request_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    origin: Mapped[str] = mapped_column(String(16), default="INITIAL")
    type: Mapped[str] = mapped_column(String(64))
    analysis_type: Mapped[str] = mapped_column(String(40), default="DOCUMENT_REQUIREMENT_VALIDATION", server_default="DOCUMENT_REQUIREMENT_VALIDATION")
    title: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    criteria: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    issue_code: Mapped[str | None] = mapped_column(String(32))
    client_message: Mapped[str | None] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __mapper_args__ = {"version_id_col": version}


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        CheckConstraint("(actor_type = 'USER' AND actor_id IS NOT NULL) OR (actor_type = 'SYSTEM' AND actor_id IS NULL)", name="ck_workflow_actor"),
        ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        UniqueConstraint("firm_id", "id"),
        Index("ix_workflow_events_request_created", "request_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_type: Mapped[str] = mapped_column(String(16), default="USER", server_default="USER")
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("status IN ('PROCESSING', 'COMPLETED')"),
        UniqueConstraint("firm_id", "actor_id", "key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(300))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PROCESSING")
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("firm_id", "request_id", "id", name="uq_submissions_firm_request_id"),
        ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        CheckConstraint("status IN ('DRAFT', 'SUBMITTED')"),
        UniqueConstraint("request_id", "round_no"),
        Index("ix_submissions_request_status", "request_id", "status"),
        Index(
            "uq_submissions_one_draft",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'DRAFT'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    round_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_analysis_search", "firm_id", "client_id", "period", "document_type", "created_at"),
        ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        CheckConstraint("status IN ('QUARANTINED', 'AVAILABLE', 'FAILED')"),
        Index("ix_documents_scan_queue", "status", "next_attempt_at"),
        Index("ix_documents_client_sha", "firm_id", "client_id", "sha256"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[UUID]
    uploader_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    document_type: Mapped[str | None] = mapped_column(String(64))
    entity_name: Mapped[str | None] = mapped_column(String(200))
    period: Mapped[date | None] = mapped_column(Date)
    extracted_data: Mapped[dict | None] = mapped_column(JSONB)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="QUARANTINED")
    failure_code: Mapped[str | None] = mapped_column(String(64))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RequirementDocument(Base):
    __tablename__ = "requirement_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        Index("ix_requirement_documents_submission", "submission_id"),
        Index("ix_requirement_documents_document", "document_id"),
        CheckConstraint("relation IN ('SUPPORTS', 'CONTRADICTS', 'REFERENCE')"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id"))
    requirement_id: Mapped[UUID | None] = mapped_column(ForeignKey("requirements.id"))
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    document_type: Mapped[str] = mapped_column(String(64))
    relation: Mapped[str] = mapped_column(String(16), default="SUPPORTS")
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        CheckConstraint("(source = 'HUMAN' AND created_by IS NOT NULL AND ai_run_id IS NULL) OR (source = 'AI' AND created_by IS NULL AND ai_run_id IS NOT NULL AND decision IN ('SATISFY', 'REQUEST_ACTION'))", name="ck_review_decision_source"),
        ForeignKeyConstraint(["firm_id", "submission_id", "ai_run_id"], ["ai_runs.firm_id", "ai_runs.submission_id", "ai_runs.id"], name="fk_review_ai_run_submission"),
        ForeignKeyConstraint(["firm_id", "requirement_id"], ["requirements.firm_id", "requirements.id"], name="fk_review_requirement_firm"),
        Index("uq_review_ai_run_requirement", "ai_run_id", "requirement_id", unique=True, postgresql_where=text("source = 'AI'")),
        CheckConstraint("decision IN ('SATISFY', 'REQUEST_ACTION', 'WAIVE')"),
        CheckConstraint(
            "issue_code IS NULL OR issue_code IN ('MISSING', 'WRONG_PERIOD', "
            "'ENTITY_MISMATCH', 'UNREADABLE', 'INCOMPLETE', 'OTHER')"
        ),
        Index("ix_review_decisions_requirement", "requirement_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    requirement_id: Mapped[UUID] = mapped_column(ForeignKey("requirements.id"))
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id"))
    decision: Mapped[str] = mapped_column(String(24))
    issue_code: Mapped[str | None] = mapped_column(String(32))
    client_message: Mapped[str | None] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="HUMAN", server_default="HUMAN")
    ai_run_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReviewDecisionDocument(Base):
    __tablename__ = "review_decision_documents"
    __table_args__ = (
        CheckConstraint("relation IN ('SUPPORTS', 'CONTRADICTS', 'REFERENCE')"),
    )

    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_decisions.id"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(16))


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        ForeignKeyConstraint(["firm_id", "request_id"], ["collection_requests.firm_id", "collection_requests.id"]),
        ForeignKeyConstraint(["firm_id", "request_id", "submission_id"], ["submissions.firm_id", "submissions.request_id", "submissions.id"]),
        ForeignKeyConstraint(["firm_id", "requested_by"], ["users.firm_id", "users.id"]),
        UniqueConstraint("firm_id", "submission_id", "id", name="uq_ai_runs_firm_submission_id"),
        CheckConstraint("purpose IN ('CLASSIFY', 'REVIEW')", name="ck_ai_runs_purpose"),
        CheckConstraint("purpose <> 'REVIEW' OR submission_id IS NOT NULL", name="ck_ai_runs_review_submission"),
        CheckConstraint("status IN ('DRAFT', 'QUEUED', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_ai_runs_status"),
        CheckConstraint("attempts >= 0", name="ck_ai_runs_attempts"),
        Index("ix_ai_runs_queue", "status", "next_attempt_at"),
        Index("ix_ai_runs_request_created", "request_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    submission_id: Mapped[UUID | None]
    purpose: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", server_default="DRAFT")
    model_version: Mapped[str | None] = mapped_column(String(200))
    input_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[UUID | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation: Mapped[list | None] = mapped_column(JSONB)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        ForeignKeyConstraint(["firm_id", "request_id"], ["collection_requests.firm_id", "collection_requests.id"]),
        UniqueConstraint("firm_id", "dedupe_key", name="uq_notification_outbox_dedupe"),
        CheckConstraint("status = 'SUPPRESSED' AND last_error = 'PROVIDER_DISABLED' AND sent_at IS NULL", name="ck_outbox_delivery_disabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    request_id: Mapped[UUID]
    channel: Mapped[str] = mapped_column(String(16), default="EMAIL", server_default="EMAIL")
    recipient: Mapped[str] = mapped_column(String(320))
    template: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="SUPPRESSED", server_default="SUPPRESSED")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(64), default="PROVIDER_DISABLED", server_default="PROVIDER_DISABLED")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(["firm_id", "user_id"], ["users.firm_id", "users.id"]),
        ForeignKeyConstraint(
            ["firm_id", "event_id"],
            ["workflow_events.firm_id", "workflow_events.id"],
        ),
        UniqueConstraint("user_id", "event_id", name="uq_notifications_user_event"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    firm_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"))
    user_id: Mapped[UUID]
    event_id: Mapped[UUID]
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
