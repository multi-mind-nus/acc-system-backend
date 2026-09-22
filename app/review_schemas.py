from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.collection_schemas import CollectionStatus, WorkflowEventOut

EvidenceRelation = Literal["SUPPORTS", "CONTRADICTS", "REFERENCE"]
ReviewAction = Literal["SATISFY", "REQUEST_ACTION", "WAIVE"]
IssueCode = Literal[
    "MISSING", "WRONG_PERIOD", "ENTITY_MISMATCH", "UNREADABLE", "INCOMPLETE", "OTHER"
]


class EvidenceInput(BaseModel):
    document_id: UUID
    relation: EvidenceRelation = "SUPPORTS"


class RequirementReviewInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(ge=1)
    submission_id: UUID
    decision: ReviewAction
    issue_code: IssueCode | None = None
    client_message: str | None = Field(default=None, max_length=2000)
    internal_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_reason(self):
        if self.decision == "REQUEST_ACTION" and not (
            self.issue_code and self.client_message
        ):
            raise ValueError("Requesting action requires an issue code and client message")
        if self.decision == "WAIVE" and not (self.client_message or self.internal_note):
            raise ValueError("Waiving a requirement requires a reason")
        return self


class TransitionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class ApprovalInput(BaseModel):
    version: int = Field(ge=1)


class ReviewDocumentOut(BaseModel):
    id: UUID
    link_id: UUID
    submission_id: UUID
    round_no: int
    name: str
    content_type: str
    size_bytes: int
    status: str
    document_type: str
    relation: EvidenceRelation
    created_at: datetime


class ReviewDecisionOut(BaseModel):
    id: UUID
    submission_id: UUID
    decision: ReviewAction
    issue_code: IssueCode | None
    client_message: str | None
    internal_note: str | None
    created_by: UUID
    created_by_name: str
    created_at: datetime
    evidence: list[EvidenceInput]


class ReviewRequirementOut(BaseModel):
    id: UUID
    type: str
    title: str
    required: bool
    status: str
    version: int
    issue_code: IssueCode | None
    client_message: str | None
    internal_note: str | None
    documents: list[ReviewDocumentOut]
    decisions: list[ReviewDecisionOut]


class ReviewSubmissionOut(BaseModel):
    id: UUID
    round_no: int
    note: str | None
    submitted_at: datetime | None


class ReviewCollectionOut(BaseModel):
    id: UUID
    client_id: UUID
    client_name: str
    period: date
    due_at: datetime
    status: CollectionStatus
    version: int
    assignee_name: str
    requirements: list[ReviewRequirementOut]
    other_documents: list[ReviewDocumentOut]
    submissions: list[ReviewSubmissionOut]
    events: list[WorkflowEventOut]
