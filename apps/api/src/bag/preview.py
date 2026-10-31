"""Authenticated, owner-scoped delivery of re-encoded thumbnails (ADR-0023).

This is the only place Bag answers with an image inline. The bytes always come
from the derived `thumbnail` blob, never from an original. Before every response,
a 304 included, the blob is integrity-checked and its WebP structure and size are
validated from the headers. Pixel data is never decoded here: that is established
by the renderer and the import check in a limited child process (ADR-0023).
"""

import logging
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import Response

from bag.config import Settings
from bag.db import connection
from bag.imaging import MAX_OUTPUT_BYTES, OUTPUT_MIME_TYPE, is_clean_webp
from bag.storage import BlobStorage, StorageError, StoredBlob

logger = logging.getLogger("bag.preview")


def _unavailable() -> HTTPException:
    return HTTPException(404, "Thumbnail not available", headers={"Cache-Control": "no-store"})


def _etag(sha256: str) -> str:
    return f'"{sha256}"'


def _matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return "*" in candidates or etag in candidates


def thumbnail(
    settings: Settings,
    owner_id: UUID,
    item_id: UUID,
    storage: BlobStorage,
    if_none_match: str | None = None,
) -> Response:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT b.storage_key, b.sha256, b.size_bytes, b.mime_type "
            "FROM blob b JOIN item i ON (i.owner_id = b.owner_id AND i.id = b.item_id) "
            "WHERE i.owner_id = %s AND i.id = %s AND i.deleted_at IS NULL "
            "AND b.role = 'thumbnail' ORDER BY b.created_at DESC, b.id DESC LIMIT 1",
            (owner_id, item_id),
        ).fetchone()
    if row is None or row["mime_type"] != OUTPUT_MIME_TYPE or row["size_bytes"] > MAX_OUTPUT_BYTES:
        raise _unavailable()
    # The stored object is verified before anything is confirmed, a 304 included: a
    # missing, damaged or foreign blob must never be revalidated as good.
    blob = StoredBlob(row["sha256"], row["size_bytes"], row["storage_key"])
    try:
        with storage.open_verified(blob) as source:
            data = source.read(MAX_OUTPUT_BYTES + 1)
    except StorageError:
        logger.error("thumbnail_unreadable", extra={"item_id": item_id, "owner_id": owner_id})
        raise _unavailable() from None
    if not is_clean_webp(data):
        # Tampered or foreign bytes (e.g. an altered import) are never sent inline.
        logger.error("thumbnail_invalid", extra={"item_id": item_id, "owner_id": owner_id})
        raise _unavailable()
    etag = _etag(row["sha256"])
    # The URL names the item, so the browser may keep the bytes but must ask first: a
    # trashed item or a regenerated thumbnail must not be served from a stale copy.
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Vary": "Authorization, Cookie",
    }
    if _matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        data,
        media_type=OUTPUT_MIME_TYPE,
        headers={
            **headers,
            "Content-Disposition": 'inline; filename="thumbnail.webp"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )
