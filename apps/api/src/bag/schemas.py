from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field, field_validator


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


class UrlCapture(CaptureMetadata):
    url: str = Field(min_length=1, max_length=8192)
    kind: Literal["url"] = "url"

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        cls.postgres_text(value)
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
            raise ValueError("URL must not contain whitespace, controls or backslashes")
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("An absolute HTTP(S) URL is required")
        AnyHttpUrl(value)
        # Validate without replacing the original with a normalized URL.
        return value


def valid_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100:
        raise ValueError("Name must be 1–100 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Name must not contain control characters")
    value.encode("utf-8")
    return value


class NameCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def checked(cls, value: str) -> str:
        return valid_name(value)


class NamedResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    item_count: int


class ItemUpdate(BaseModel):
    """Fields a user may edit; absent fields stay unchanged, null clears."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    user_note: str | None = None
    language: Literal["de", "en"] | None = None
    tags: list[str] | None = Field(default=None, max_length=100)
    collections: list[str] | None = Field(default=None, max_length=100)

    @field_validator("title", "user_note")
    @classmethod
    def postgres_text(cls, value: str | None) -> str | None:
        return CaptureMetadata.postgres_text(value)

    @field_validator("tags", "collections")
    @classmethod
    def checked_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return sorted({valid_name(name) for name in value})


class CaptureResponse(BaseModel):
    id: UUID
    status: Literal["stored"] = "stored"
    processing_status: str
    duplicate_of: UUID | None = None


class ItemResponse(BaseModel):
    id: UUID
    kind: str
    source: str
    source_url: str | None
    title: str | None
    content: str | None
    user_note: str | None
    mime_type: str | None
    original_filename: str | None
    content_hash: str | None
    extracted_text: str | None
    language: str | None
    tags: list[str]
    collections: list[str]
    processing_status: str
    created_at: datetime
    captured_at: datetime
    updated_at: datetime


class ItemSummary(BaseModel):
    id: UUID
    kind: str
    source: str
    title: str | None
    user_note: str | None
    mime_type: str | None
    original_filename: str | None
    source_url: str | None
    language: str | None
    tags: list[str]
    collections: list[str]
    processing_status: str
    created_at: datetime
    captured_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ItemPage(BaseModel):
    items: list[ItemSummary]
    next_cursor: UUID | None


class SearchResult(ItemSummary):
    rank: float
    snippet: str


class SearchPage(BaseModel):
    results: list[SearchResult]
    next_offset: int | None


class ProcessingRunResponse(BaseModel):
    processor: str
    status: str
    attempts: int
    last_error: str | None
    started_at: datetime | None
    finished_at: datetime | None
