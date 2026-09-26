import codecs
import re
import struct
from dataclasses import dataclass

import filetype
from pypdf import PasswordType, PdfReader
from pypdf.errors import PdfReadError

from bag.fetch import UrlFetcher, clean_text
from bag.processing import (
    MAX_EXTRACTED_BYTES,
    SKIPPED,
    Outcome,
    ProcessingItem,
    Processor,
    ProcessorError,
    SnapshotBlob,
    is_plain_text,
)
from bag.storage import BlobStorage, StorageError

SIGNATURE_BYTES = 8192
# Re-exported for processors' callers and tests; the contract lives in bag.processing.
__all__ = [
    "MAX_EXTRACTED_BYTES",
    "PROCESSORS",
    "SKIPPED",
    "Outcome",
    "ProcessingItem",
    "Processor",
    "ProcessorError",
    "SnapshotBlob",
    "detect_language",
    "image_dimensions",
    "image_meta",
    "is_plain_text",
    "kind_for",
    "language",
    "looks_like_text",
    "mime_detect",
    "pdf_text",
    "sniff_mime",
    "text_extract",
]


def kind_for(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type == "application/pdf":
        return "document"
    return "file"


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
    # Fall back to text another processor extracted (PDF, fetched page) on a rerun.
    text = found.text if found is not None else item.extracted_text
    if not text:
        return SKIPPED
    detected = detect_language(text)
    if detected is None:
        return Outcome("skipped", metadata={"detected": None})
    return Outcome("succeeded", {"language": detected}, {"detected": detected})


PDF_MAX_PAGES = 2000


def pdf_text(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    """Text layer of PDF originals via pypdf, bounded like every other extraction."""
    if item.original is None:
        return SKIPPED
    try:
        with storage.open_verified(item.original) as source:
            if sniff_mime(source.read(SIGNATURE_BYTES)) != "application/pdf":
                return SKIPPED
            source.seek(0)
            try:
                reader = PdfReader(source)
                if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                    return Outcome("skipped", metadata={"encrypted": True})
                pages = len(reader.pages)
                parts: list[str] = []
                size = 0
                truncated = pages > PDF_MAX_PAGES
                for index, page in enumerate(reader.pages):
                    if index >= PDF_MAX_PAGES:
                        break
                    chunk = page.extract_text() or ""
                    size += len(chunk.encode("utf-8"))
                    parts.append(chunk)
                    if size > MAX_EXTRACTED_BYTES:
                        truncated = True
                        break
            except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as exc:
                raise ProcessorError("PDF could not be parsed", retryable=False) from exc
    except StorageError as exc:
        raise ProcessorError("Original unavailable or failed its integrity check") from exc
    text = clean_text("\n".join(parts))
    if text is None:
        return Outcome("skipped", metadata={"pages": pages, "text_layer": False})
    return Outcome(
        "succeeded",
        {"extracted_text": text},
        {"pages": pages, "truncated": truncated, "extracted_bytes": len(text.encode("utf-8"))},
    )


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from the header of PNG, GIF, JPEG or WebP data, else None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(data) >= 30:
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                return None
            marker = data[offset + 1]
            if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
                return width, height
            offset += 2 + length
    return None


IMAGE_HEADER_BYTES = 64 * 1024


def image_meta(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    if item.original is None:
        return SKIPPED
    try:
        with storage.open_verified(item.original) as source:
            data = source.read(IMAGE_HEADER_BYTES)
    except StorageError as exc:
        raise ProcessorError("Original unavailable or failed its integrity check") from exc
    if not sniff_mime(data[:SIGNATURE_BYTES]).startswith("image/"):
        return SKIPPED
    dimensions = image_dimensions(data)
    if dimensions is None or min(dimensions) <= 0:
        return Outcome("skipped", metadata={"dimensions": None})
    width, height = dimensions
    return Outcome("succeeded", metadata={"width": width, "height": height})


PROCESSORS: dict[str, Processor] = {
    "mime_detect": mime_detect,
    "text_extract": text_extract,
    "language": language,
    "url_fetch": UrlFetcher(),
    "image_meta": image_meta,
    "pdf_text": pdf_text,
}
