from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class CaptureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="api", min_length=1, max_length=100)
    user_note: str | None = None
    captured_at: AwareDatetime | None = None
    client_capture_id: UUID | None = None

    @field_validator("user_note", "source")
    @classmethod
    def postgres_text(cls, value: str | None) -> str | None:
        if value is not None:
            if "\x00" in value:
                raise ValueError("NUL characters cannot be stored as PostgreSQL text")
            value.encode("utf-8")
        return value


class TextCapture(CaptureMetadata):
    content: str = Field(min_length=1)
    kind: Literal["text"] = "text"

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        cls.postgres_text(value)
        return value


class FileCapture(CaptureMetadata):
    kind: Literal["file"] = "file"


class CaptureResponse(BaseModel):
    id: UUID
    status: Literal["stored"] = "stored"
    processing_status: str
    duplicate_of: UUID | None = None


class ItemResponse(BaseModel):
    id: UUID
    kind: str
    source: str
    content: str | None
    user_note: str | None
    mime_type: str | None
    original_filename: str | None
    content_hash: str | None
    processing_status: str
    created_at: datetime
    captured_at: datetime
    updated_at: datetime
