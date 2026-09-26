"""Shared processor contract: item snapshot, outcome and error types."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from bag.storage import BlobStorage, StoredBlob

MAX_EXTRACTED_BYTES = 1_048_576
_ALLOWED_CONTROLS = {"\t", "\n", "\r", "\x0b", "\x0c"}


class ProcessorError(Exception):
    """A failure whose message is safe to persist; retried unless stated otherwise."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ProcessingItem:
    id: UUID
    owner_id: UUID
    kind: str
    mime_type: str | None
    content: str | None
    original: StoredBlob | None
    metadata: dict[str, Any] = field(default_factory=dict)
    extracted_text: str | None = None


@dataclass(frozen=True)
class SnapshotBlob:
    role: str
    blob: StoredBlob
    mime_type: str


@dataclass(frozen=True)
class Outcome:
    status: Literal["succeeded", "skipped"]
    # Only enrichment columns; originals and notes are never touched.
    updates: dict[str, str | None] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    # Applied only where the column is still NULL, so user input always wins.
    defaults: dict[str, str] = field(default_factory=dict)
    # Derived objects such as page snapshots; one row per role, replaced on rerun.
    blobs: list[SnapshotBlob] = field(default_factory=list)


Processor = Callable[[ProcessingItem, BlobStorage], Outcome]

SKIPPED = Outcome("skipped")


def is_plain_text(value: str) -> bool:
    """Non-empty text without NUL, DEL or C0 controls other than common whitespace."""
    return bool(value) and not any(
        (ord(c) < 32 and c not in _ALLOWED_CONTROLS) or ord(c) == 127 for c in value
    )
