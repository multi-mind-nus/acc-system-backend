from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CollectionStatus = Literal[
    "DRAFT", "OPEN", "IN_REVIEW", "CHANGES_REQUESTED",
    "READY_FOR_BOOKKEEPING", "CLOSED", "CANCELLED",
]
RequirementStatus = Literal[
    "PENDING", "RECEIVED", "NEEDS_ACTION", "SATISFIED", "WAIVED",
]


class RequirementInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    title: str = Field(min_length=1, max_length=200)
    required: bool = True
    criteria: dict = Field(default_factory=dict)


class RequirementUpdate(RequirementInput):
    version: int = Field(ge=1)


class RequirementOut(RequirementInput):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    origin: Literal["INITIAL", "FOLLOW_UP"]
    status: RequirementStatus
    version: int


class CollectionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    client_id: UUID
    period: date
    due_at: datetime
    scope_note: str | None = Field(default=None, max_length=4000)
    assignee_id: UUID | None = None
    requirements: list[RequirementInput] = Field(min_length=1, max_length=100)

    @field_validator("period")
    @classmethod
    def period_starts_month(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("Period must be the first day of a month")
        return value

    @field_validator("due_at")
    @classmethod
    def due_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Due date must include a timezone")
        return value


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(ge=1)
    due_at: datetime | None = None
    scope_note: str | None = Field(default=None, max_length=4000)
    assignee_id: UUID | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not (self.model_fields_set - {"version"}):
            raise ValueError("At least one field must be updated")
        if "due_at" in self.model_fields_set and self.due_at is None:
            raise ValueError("Due date cannot be null")
        if "assignee_id" in self.model_fields_set and self.assignee_id is None:
            raise ValueError("Assignee cannot be null")
        if self.due_at is not None and self.due_at.tzinfo is None:
            raise ValueError("Due date must include a timezone")
        return self


class CollectionSummaryOut(BaseModel):
    id: UUID
    client_id: UUID
    client_name: str
    period: date
    due_at: datetime
    status: CollectionStatus
    scope_note: str | None
    version: int
    assignee_id: UUID
    assignee_name: str
    requirement_count: int
    updated_at: datetime


class WorkflowEventOut(BaseModel):
    id: UUID
    actor_id: UUID
    actor_name: str
    event_type: str
    payload: dict
    created_at: datetime


class CollectionDetailOut(CollectionSummaryOut):
    requirements: list[RequirementOut]
    events: list[WorkflowEventOut]


class CollectionListOut(BaseModel):
    items: list[CollectionSummaryOut]
    total: int
    page: int
    page_size: int


class CollectionDashboardOut(BaseModel):
    awaiting_review: list[CollectionSummaryOut]
    waiting_client: list[CollectionSummaryOut]
    due_soon: list[CollectionSummaryOut]
    overdue: list[CollectionSummaryOut]
    counts: dict[str, int]


class CancelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)


class VersionRequest(BaseModel):
    version: int = Field(ge=1)
