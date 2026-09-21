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


class PortalSubmissionOut(BaseModel):
    id: UUID
    round_no: int
    status: Literal["DRAFT", "SUBMITTED"]
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


class PortalCollectionDetailOut(PortalCollectionSummaryOut):
    scope_note: str | None
    requirements: list[PortalRequirementOut]
    submission: PortalSubmissionOut | None


class PortalCollectionListOut(BaseModel):
    items: list[PortalCollectionSummaryOut]
    total: int


class PortalUploadOut(BaseModel):
    submission_id: UUID
    document: PortalDocumentOut


class ClassificationFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=0)


class ClassificationRequest(BaseModel):
    files: list[ClassificationFileInput] = Field(min_length=1, max_length=100)


class ClassificationItemOut(BaseModel):
    index: int
    category: Literal["REQUIREMENT", "OTHER", "INVALID"]
    requirement_id: UUID | None
    confidence: float


class ClassificationOut(BaseModel):
    provider: Literal["FAKE"] = "FAKE"
    items: list[ClassificationItemOut]
