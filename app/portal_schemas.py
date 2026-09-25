from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DocumentStatus = Literal["QUARANTINED", "AVAILABLE", "FAILED", "EXCLUDED"]


class PortalDocumentOut(BaseModel):
    id: UUID
    link_id: UUID
    name: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    failure_code: str | None
    duplicate: bool = False
    editable: bool
    counts_for_submission: bool
    created_at: datetime


class PortalRequirementOut(BaseModel):
    id: UUID
    type: str
    title: str
    required: bool
    criteria: dict
    status: str
    client_message: str | None
    documents: list[PortalDocumentOut]


class PortalWorkflowEventOut(BaseModel):
    id: UUID
    event_type: str
    payload: dict
    created_at: datetime


class PortalSubmissionOut(BaseModel):
    id: UUID
    round_no: int
    status: Literal["DRAFT", "SUBMITTED"]
    note: str | None
    manual_review_requested: bool
    submitted_at: datetime | None
    created_at: datetime


class PortalCollectionSummaryOut(BaseModel):
    id: UUID
    client_id: UUID
    client_name: str
    period: date
    due_at: datetime
    status: str
    assignee_name: str
    required_count: int
    ready_count: int
    updated_at: datetime
    review_status: Literal["PROCESSING", "AI_PASSED", "AI_NEEDS_REVIEW", "AI_FAILED", "AWAITING_ACCOUNTANT"] | None = None


class PortalCollectionDetailOut(PortalCollectionSummaryOut):
    scope_note: str | None
    requirements: list[PortalRequirementOut]
    submission: PortalSubmissionOut | None
    manual_review_available: bool
    events: list[PortalWorkflowEventOut]


class PortalCollectionListOut(BaseModel):
    items: list[PortalCollectionSummaryOut]
    total: int


class PortalUploadOut(BaseModel):
    submission_id: UUID
    document: PortalDocumentOut


class PortalSubmitInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note: str | None = Field(default=None, max_length=2000)
    manual_review_requested: bool = False
