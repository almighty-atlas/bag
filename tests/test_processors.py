import io
import os
import threading
from pathlib import Path

import pytest
from bag.config import Settings
from bag.ids import uuid7
from bag.processors import (
    MAX_EXTRACTED_BYTES,
    ProcessingItem,
    ProcessorError,
    kind_for,
    looks_like_text,
    mime_detect,
    sniff_mime,
    text_extract,
)
from bag.storage import FileSystemStorage, StoredBlob
from bag.worker import Worker
from pydantic import SecretStr

PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (b"", False),
        (b"hello world\n\tindented\r\n", True),
        ("Grüße 💼 日本語".encode(), True),
        ("Grüße".encode()[:-1], True),  # cut inside a multi-byte sequence
        (b"\xff\xfe\x00\x00", False),
        (b"text\x00with nul", False),
        (b"\x1b[0m escape", False),
        (b"del\x7f", False),
    ],
)
def test_looks_like_text(sample: bytes, expected: bool) -> None:
    assert looks_like_text(sample) is expected


def test_sniff_and_kind() -> None:
    assert sniff_mime(PDF) == "application/pdf" and kind_for("application/pdf") == "document"
    assert sniff_mime(b"plain") == "text/plain" and kind_for("text/plain") == "file"
    assert sniff_mime(b"\x00\x01\x02") == "application/octet-stream"
    assert kind_for("image/png") == "image"


def stored(storage: FileSystemStorage, data: bytes) -> StoredBlob:
    return storage.put(io.BytesIO(data), len(data) + 1)


def file_item(
    blob: StoredBlob | None, kind: str = "file", content: str | None = None
) -> ProcessingItem:
    return ProcessingItem(uuid7(), uuid7(), kind, None, content, blob)


def test_processors_without_database(tmp_path: Path) -> None:
    storage = FileSystemStorage(tmp_path)
    text = file_item(None, "text", "Original")
    assert mime_detect(text, storage).updates == {"mime_type": "text/plain"}
    assert text_extract(text, storage).updates == {"extracted_text": "Original"}
    url = file_item(None, "url", "https://example.org")
    assert mime_detect(url, storage).status == text_extract(url, storage).status == "skipped"

    pdf = file_item(stored(storage, PDF), "image")
    assert mime_detect(pdf, storage).updates == {"mime_type": "application/pdf", "kind": "document"}
    assert text_extract(pdf, storage).status == "skipped"

    # Kind set by a future client is preserved; only file kinds are re-derived.
    code = file_item(stored(storage, b"print('hi')\n"), "code")
    assert mime_detect(code, storage).updates == {"mime_type": "text/plain"}

    late_nul = file_item(stored(storage, b"a" * 9000 + b"\x00"))
    assert text_extract(late_nul, storage).status == "skipped"
    late_invalid = file_item(stored(storage, b"a" * 9000 + b"\xff"))
    assert text_extract(late_invalid, storage).status == "skipped"

    long_text = "Zeile ü\n" * (MAX_EXTRACTED_BYTES // 8 + 10)
    outcome = text_extract(file_item(stored(storage, long_text.encode())), storage)
    extracted = outcome.updates["extracted_text"]
    assert extracted is not None and long_text.startswith(extracted)
    assert len(extracted.encode()) <= MAX_EXTRACTED_BYTES
    assert outcome.metadata["truncated"] is True

    missing = file_item(StoredBlob("0" * 64, 1, "00/00/" + "0" * 64))
    with pytest.raises(ProcessorError) as error:
        mime_detect(missing, storage)
    assert error.value.retryable


def test_worker_loop_survives_database_outage_and_stops() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql://invalid:invalid@127.0.0.1:1/unavailable"),
        worker_poll_seconds=0.01,
    )
    worker = Worker(settings, FileSystemStorage(Path("unused")))
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert str(os.getpid()) in worker.worker_id
    assert worker.worker_id != Worker(settings, FileSystemStorage(Path("unused"))).worker_id
