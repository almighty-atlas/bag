import codecs
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import filetype

from bag.storage import BlobStorage, StorageError, StoredBlob

SIGNATURE_BYTES = 8192
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


@dataclass(frozen=True)
class Outcome:
    status: Literal["succeeded", "skipped"]
    # Only enrichment columns; originals, notes and titles are never touched.
    updates: dict[str, str | None] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


Processor = Callable[[ProcessingItem, BlobStorage], Outcome]

SKIPPED = Outcome("skipped")


def kind_for(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type == "application/pdf":
        return "document"
    return "file"


def is_plain_text(value: str) -> bool:
    """Non-empty text without NUL, DEL or C0 controls other than common whitespace."""
    return bool(value) and not any(
        (ord(c) < 32 and c not in _ALLOWED_CONTROLS) or ord(c) == 127 for c in value
    )


def looks_like_text(sample: bytes) -> bool:
    try:
        # A sample may end inside a multi-byte sequence; only invalid bytes are errors.
        decoded = codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return is_plain_text(decoded)


def sniff_mime(sample: bytes) -> str:
    mime: str | None = filetype.guess_mime(sample)
    if mime is not None:
        return mime
    return "text/plain" if looks_like_text(sample) else "application/octet-stream"


def _read_sample(item: ProcessingItem, storage: BlobStorage) -> bytes:
    assert item.original is not None
    try:
        with storage.open_verified(item.original) as source:
            return source.read(SIGNATURE_BYTES)
    except StorageError as exc:
        raise ProcessorError("Original unavailable or failed its integrity check") from exc


def mime_detect(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    if item.original is None:
        if item.kind == "text":
            return Outcome("succeeded", {"mime_type": "text/plain"})
        return SKIPPED
    mime = sniff_mime(_read_sample(item, storage))
    updates: dict[str, str | None] = {"mime_type": mime}
    if item.kind in {"file", "image", "document"}:
        updates["kind"] = kind_for(mime)
    return Outcome("succeeded", updates)


def text_extract(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    if item.original is None:
        if item.kind == "text" and item.content is not None:
            return Outcome("succeeded", {"extracted_text": item.content})
        return SKIPPED
    try:
        with storage.open_verified(item.original) as source:
            data = source.read(MAX_EXTRACTED_BYTES + 1)
    except StorageError as exc:
        raise ProcessorError("Original unavailable or failed its integrity check") from exc
    # Decide from content, not from a possibly not yet detected MIME type.
    if sniff_mime(data[:SIGNATURE_BYTES]) != "text/plain":
        return SKIPPED
    truncated = len(data) > MAX_EXTRACTED_BYTES
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(
            data[:MAX_EXTRACTED_BYTES], final=not truncated
        )
    except UnicodeDecodeError:
        return SKIPPED
    if not is_plain_text(text):
        return SKIPPED
    return Outcome(
        "succeeded",
        {"extracted_text": text},
        {"truncated": truncated, "extracted_bytes": len(text.encode("utf-8"))},
    )


PROCESSORS: dict[str, Processor] = {
    "mime_detect": mime_detect,
    "text_extract": text_extract,
}
