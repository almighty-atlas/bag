import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
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
    metadata: dict[str, Any] = field(default_factory=dict)


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


@dataclass(frozen=True)
class PlainText:
    text: str
    truncated: bool


def plain_text(item: ProcessingItem, storage: BlobStorage) -> PlainText | None:
    """The item's own text: inline content or a decoded UTF-8 text file, else None."""
    if item.original is None:
        if item.kind == "text" and item.content is not None:
            return PlainText(item.content, False)
        return None
    try:
        with storage.open_verified(item.original) as source:
            data = source.read(MAX_EXTRACTED_BYTES + 1)
    except StorageError as exc:
        raise ProcessorError("Original unavailable or failed its integrity check") from exc
    # Decide from content, not from a possibly not yet detected MIME type.
    if sniff_mime(data[:SIGNATURE_BYTES]) != "text/plain":
        return None
    truncated = len(data) > MAX_EXTRACTED_BYTES
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(
            data[:MAX_EXTRACTED_BYTES], final=not truncated
        )
    except UnicodeDecodeError:
        return None
    if not is_plain_text(text):
        return None
    return PlainText(text, truncated)


def text_extract(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    found = plain_text(item, storage)
    if found is None:
        return SKIPPED
    return Outcome(
        "succeeded",
        {"extracted_text": found.text},
        {"truncated": found.truncated, "extracted_bytes": len(found.text.encode("utf-8"))},
    )


# Frequent function words that rarely overlap between the two languages.
STOPWORDS: dict[str, frozenset[str]] = {
    "de": frozenset(
        "der die das und ist nicht ein eine sich mit auch auf für von dem den des im "
        "sind wird werden wurde noch nur aber oder wenn wie aus bei nach über zu zum zur "
        "als kann ich wir ihr sie es dass sein seine ihre einem einer eines diese dieser "
        "haben hat mich dich uns euch schon sehr mehr hier dort keine kein".split()
    ),
    "en": frozenset(
        "the and is not a an with also for of in are will was still only but or if how "
        "from at after about to as can i we you they it that be his her this these have "
        "has me us already very more here there no their our its which what were been "
        "would should could into than then some any all".split()
    ),
}
WORD = re.compile(r"[^\W\d_]+")
MIN_HITS = 3
MIN_RATIO = 1.5
DETECT_CHARS = 20_000


def detect_language(text: str) -> str | None:
    """`de`, `en` or None when the evidence is thin or ambiguous."""
    words = WORD.findall(text[:DETECT_CHARS].lower())
    hits = {lang: sum(word in stopwords for word in words) for lang, stopwords in STOPWORDS.items()}
    winner, runner_up = sorted(hits, key=hits.get, reverse=True)  # type: ignore[arg-type]
    if hits[winner] < MIN_HITS or hits[winner] < MIN_RATIO * hits[runner_up]:
        return None
    return winner


def language(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    marker = item.metadata.get("language")
    if isinstance(marker, dict) and marker.get("user"):
        # A user-set language is never overwritten by detection.
        return SKIPPED
    found = plain_text(item, storage)
    if found is None:
        return SKIPPED
    detected = detect_language(found.text)
    if detected is None:
        return Outcome("skipped", metadata={"detected": None})
    return Outcome("succeeded", {"language": detected}, {"detected": detected})


PROCESSORS: dict[str, Processor] = {
    "mime_detect": mime_detect,
    "text_extract": text_extract,
    "language": language,
}
