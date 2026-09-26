import hashlib
import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from bag.storage import FileSystemStorage, StorageError, StoredBlob, UploadTooLarge, storage_key


def test_atomic_concurrent_storage_and_integrity(tmp_path: Path) -> None:
    store = FileSystemStorage(tmp_path / "originals")
    content = bytes(range(256)) * 1000

    def put(index: int) -> StoredBlob:
        return store.put(io.BytesIO(content), len(content))

    with ThreadPoolExecutor(max_workers=8) as pool:
        blobs = list(pool.map(put, range(16)))
    assert len(set(blobs)) == 1
    blob = blobs[0]
    assert blob.sha256 == hashlib.sha256(content).hexdigest()
    assert blob.size_bytes == len(content)
    with store.open_verified(blob) as source:
        assert source.read() == content
    assert [p for p in store.root.rglob("*") if p.is_file()] == [store.root / blob.storage_key]
    with pytest.raises(StorageError):
        store.open_verified(StoredBlob(blob.sha256, blob.size_bytes, "../../outside"))


def test_empty_file_and_oversize_cleanup(tmp_path: Path) -> None:
    store = FileSystemStorage(tmp_path)
    empty = store.put(io.BytesIO(b""), 1)
    with store.open_verified(empty) as source:
        assert source.read() == b""
    with pytest.raises(UploadTooLarge):
        store.put(io.BytesIO(b"too large"), 3)
    assert not list(tmp_path.glob(".upload-*"))
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_corruption_never_overwrites_shared_original(tmp_path: Path) -> None:
    store = FileSystemStorage(tmp_path)
    blob = store.put(io.BytesIO(b"original"), 100)
    (tmp_path / blob.storage_key).write_bytes(b"corrupted")
    with pytest.raises(StorageError):
        store.open_verified(blob)
    with pytest.raises(StorageError):
        store.put(io.BytesIO(b"original"), 100)
    assert (tmp_path / blob.storage_key).read_bytes() == b"corrupted"


def test_failed_sync_never_reports_success_and_retry_reuses_orphan(tmp_path: Path) -> None:
    store = FileSystemStorage(tmp_path)
    # Root preparation succeeds; fail the first file fsync before publication.
    real_fsync = os.fsync

    def fail_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("disk failure")
        real_fsync(descriptor)

    with patch("bag.storage.os.fsync", side_effect=fail_file_sync):
        with pytest.raises(StorageError):
            store.put(io.BytesIO(b"original"), 100)
    assert not list(tmp_path.rglob(".upload-*"))
    real_link = os.link

    def publish_then_fail(source: Path, destination: Path) -> None:
        real_link(source, destination)
        raise OSError("crash after publication")

    with patch("bag.storage.os.link", side_effect=publish_then_fail):
        with pytest.raises(StorageError):
            store.put(io.BytesIO(b"original"), 100)
    blob = store.put(io.BytesIO(b"original"), 100)
    with store.open_verified(blob) as source:
        assert source.read() == b"original"
    assert not list(tmp_path.glob(".upload-*"))


@pytest.mark.parametrize("digest", ["../a", "a" * 63, "A" * 64, "g" * 64])
def test_storage_key_rejects_non_hash(digest: str) -> None:
    with pytest.raises(StorageError):
        storage_key(digest)
