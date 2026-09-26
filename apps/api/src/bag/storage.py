import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

CHUNK_SIZE = 64 * 1024


class StorageError(Exception):
    pass


class UploadTooLarge(Exception):
    pass


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    storage_key: str


class BlobStorage(Protocol):
    def put(self, source: BinaryIO, limit: int) -> StoredBlob: ...
    def open_verified(self, blob: StoredBlob) -> BinaryIO: ...


def storage_key(digest: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise StorageError("Invalid content hash")
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(path: Path) -> None:
    if not path.exists():
        durable_directory(path.parent)
        path.mkdir(exist_ok=True)
    # Also sync existing entries: a concurrent writer may just have created them.
    sync_directory(path)
    sync_directory(path.parent)


def chunks(source: BinaryIO) -> Iterator[bytes]:
    while data := source.read(CHUNK_SIZE):
        yield data


class FileSystemStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def put(self, source: BinaryIO, limit: int) -> StoredBlob:
        temporary: Path | None = None
        try:
            durable_directory(self.root)
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".upload-", delete=False) as out:
                temporary = Path(out.name)
                for data in chunks(source):
                    size += len(data)
                    if size > limit:
                        raise UploadTooLarge
                    digest.update(data)
                    out.write(data)
                out.flush()
                os.fsync(out.fileno())
            blob = StoredBlob(digest.hexdigest(), size, storage_key(digest.hexdigest()))
            target = self.root / blob.storage_key
            durable_directory(target.parent)
            try:
                # Atomic, no replacement: racing captures never overwrite a shared original.
                os.link(temporary, target)
            except FileExistsError:
                with self.open_verified(blob) as existing:
                    os.fsync(existing.fileno())
            sync_directory(target.parent)
            sync_directory(target.parent.parent)
            sync_directory(self.root)
            return blob
        except OSError as exc:
            raise StorageError("Storage unavailable") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    raise StorageError("Temporary storage cleanup failed") from exc

    def open_verified(self, blob: StoredBlob) -> BinaryIO:
        if blob.storage_key != storage_key(blob.sha256):
            raise StorageError("Invalid storage key")
        try:
            source = (self.root / blob.storage_key).open("rb")
            try:
                digest = hashlib.sha256()
                size = 0
                for data in chunks(source):
                    digest.update(data)
                    size += len(data)
                if digest.hexdigest() != blob.sha256 or size != blob.size_bytes:
                    raise StorageError("Original integrity check failed")
                source.seek(0)
                return source
            except BaseException:
                source.close()
                raise
        except OSError as exc:
            raise StorageError("Storage unavailable") from exc
