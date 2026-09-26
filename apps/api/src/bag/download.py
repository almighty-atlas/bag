from typing import BinaryIO, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from bag.config import Settings
from bag.db import connection
from bag.storage import BlobStorage, StoredBlob, chunks

Role = Literal["original", "snapshot"]
SNAPSHOT_EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}


class OriginalResponse(StreamingResponse):
    def __init__(self, source: BinaryIO, headers: dict[str, str]) -> None:
        self.source = source
        super().__init__(chunks(source), media_type="application/octet-stream", headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.source.close()


def safe_filename(name: str | None, fallback: str) -> str:
    # Display names cannot inject headers or turn into download destination paths.
    candidate = (name or fallback).replace("\\", "/").split("/")[-1]
    return "".join(c for c in candidate if c.isprintable()).strip(" .") or fallback


def download(
    settings: Settings,
    owner_id: UUID,
    item_id: UUID,
    storage: BlobStorage,
    role: Role = "original",
) -> StreamingResponse:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT b.storage_key, b.sha256, b.size_bytes, b.mime_type, i.original_filename, "
            "i.title FROM blob b JOIN item i ON (i.owner_id = b.owner_id AND i.id = b.item_id) "
            "WHERE i.owner_id = %s AND i.id = %s AND i.deleted_at IS NULL "
            "AND b.role = %s ORDER BY b.created_at, b.id LIMIT 1",
            (owner_id, item_id, role),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"{role.capitalize()} not found")
    blob = StoredBlob(row["sha256"], row["size_bytes"], row["storage_key"])
    source: BinaryIO = storage.open_verified(blob)
    if role == "original":
        filename = safe_filename(row["original_filename"], "download")
    else:
        extension = SNAPSHOT_EXTENSIONS.get(row["mime_type"], "")
        filename = safe_filename(row["title"], "snapshot")[:120] + extension
    # Always an attachment with a sandbox: fetched pages and uploads are untrusted.
    return OriginalResponse(
        source,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "Content-Length": str(blob.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )
