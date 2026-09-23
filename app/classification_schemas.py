from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClassificationChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    category: Literal["REQUIREMENT", "OTHER", "INVALID"]
    requirement_id: UUID | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.category == "REQUIREMENT") != (self.requirement_id is not None):
            raise ValueError("Only REQUIREMENT classification must have a requirement_id")
        return self


class ConfirmClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ClassificationChoice] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.document_id for item in self.items}) != len(self.items):
            raise ValueError("Document IDs must be unique")
        return self


class ClassificationItem(ClassificationChoice):
    document_type: str | None = Field(default=None, max_length=64)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class ClassificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"]
    run_id: UUID
    model_version: str = Field(min_length=1, max_length=200)
    classifications: list[ClassificationItem]


class StagedUploadOut(BaseModel):
    document_id: UUID


class StagedDocumentOut(BaseModel):
    document_id: UUID
    name: str
    status: str
    failure_code: str | None


class ClassificationRunOut(BaseModel):
    id: UUID
    status: str
    provider: str
    confirmed_at: datetime | None
    error: str | None
    documents: list[StagedDocumentOut]
    items: list[ClassificationItem]
