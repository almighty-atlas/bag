import hashlib
import logging
from typing import BinaryIO
from uuid import UUID

import filetype
from fastapi import HTTPException

from bag.config import Settings
from bag.db import Row, connection
from bag.ids import uuid7
from bag.jobs import enqueue
from bag.schemas import (
    CaptureResponse,
    FileCapture,
    ItemResponse,
    ProcessingRunResponse,
    TextCapture,
    UrlCapture,
)
from bag.storage import BlobStorage, StoredBlob

logger = logging.getLogger("bag.capture")


def capture_text(settings: Settings, owner_id: UUID, payload: TextCapture) -> CaptureResponse:
    return _capture(settings, owner_id, payload)


def capture_url(settings: Settings, owner_id: UUID, payload: UrlCapture) -> CaptureResponse:
    return _capture(settings, owner_id, payload)


def capture_file(
    settings: Settings,
    owner_id: UUID,
    payload: FileCapture,
    source: BinaryIO,
    filename: str,
    storage: BlobStorage,
) -> CaptureResponse:
    if len(filename) > 1024:
        raise HTTPException(422, "Filename too long")
    try:
        FileCapture.postgres_text(filename)
    except ValueError as exc:
        raise HTTPException(422, "Invalid filename") from exc
    return _capture(settings, owner_id, payload, source, filename, storage)


def _capture(
    settings: Settings,
    owner_id: UUID,
    payload: TextCapture | FileCapture | UrlCapture,
    source: BinaryIO | None = None,
    filename: str | None = None,
    storage: BlobStorage | None = None,
) -> CaptureResponse:
    with connection(settings) as conn:
        # Serializing captures per owner makes both replay and duplicate detection atomic.
        conn.execute('SELECT id FROM "user" WHERE id = %s FOR UPDATE', (owner_id,))
        existing: Row | None = None
        if payload.client_capture_id is not None:
            existing = conn.execute(
                "SELECT id, processing_status FROM item "
                "WHERE owner_id = %s AND client_capture_id = %s",
                (owner_id, payload.client_capture_id),
            ).fetchone()
        if existing is not None:
            relation = conn.execute(
                "SELECT target_item_id FROM relation WHERE owner_id = %s "
                "AND source_item_id = %s AND relation_type = 'duplicate_of' "
                "ORDER BY created_at, id LIMIT 1",
                (owner_id, existing["id"]),
            ).fetchone()
            result = CaptureResponse(
                id=existing["id"],
                processing_status=existing["processing_status"],
                duplicate_of=relation["target_item_id"] if relation else None,
            )
        else:
            blob: StoredBlob | None = None
            source_url: str | None = None
            mime_type: str | None
            if isinstance(payload, TextCapture):
                content: str | None = payload.content
                digest = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
                mime_type = "text/plain"
                kind = "text"
            elif isinstance(payload, UrlCapture):
                source_url = payload.url
                content = payload.url
                digest = hashlib.sha256(payload.url.encode("utf-8")).hexdigest()
                mime_type = None
                kind = "url"
            else:
                assert source is not None and storage is not None
                sample = source.read(8192)
                source.seek(0)
                mime_type = filetype.guess_mime(sample) or "application/octet-stream"
                kind = "image" if mime_type.startswith("image/") else "file"
                if mime_type == "application/pdf":
                    kind = "document"
                blob = storage.put(source, settings.max_upload_bytes)
                digest = blob.sha256
                content = None
            duplicate = conn.execute(
                "SELECT id FROM item WHERE owner_id = %s AND content_hash = %s "
                "AND deleted_at IS NULL ORDER BY created_at, id LIMIT 1",
                (owner_id, digest),
            ).fetchone()
            item_id = uuid7()
            conn.execute(
                "INSERT INTO item (id, owner_id, client_capture_id, kind, source, "
                "content, user_note, content_hash, mime_type, original_filename, source_url, "
                "processing_status, captured_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', "
                "coalesce(%s, now()))",
                (
                    item_id,
                    owner_id,
                    payload.client_capture_id,
                    kind,
                    payload.source,
                    content,
                    payload.user_note,
                    digest,
                    mime_type,
                    filename,
                    source_url,
                    payload.captured_at,
                ),
            )
            if blob is not None:
                conn.execute(
                    "INSERT INTO blob (id, owner_id, item_id, role, storage_key, sha256, "
                    "size_bytes, mime_type) VALUES (%s, %s, %s, 'original', %s, %s, %s, %s)",
                    (
                        uuid7(),
                        owner_id,
                        item_id,
                        blob.storage_key,
                        blob.sha256,
                        blob.size_bytes,
                        mime_type,
                    ),
                )
            duplicate_id = duplicate["id"] if duplicate else None
            if duplicate_id is not None:
                conn.execute(
                    "INSERT INTO relation (id, owner_id, source_item_id, target_item_id, "
                    "relation_type, created_by) VALUES (%s, %s, %s, %s, 'duplicate_of', 'system')",
                    (uuid7(), owner_id, item_id, duplicate_id),
                )
            # Jobs commit with the item: a stored capture is always scheduled, never orphaned.
            enqueue(conn, settings, owner_id, item_id)
            result = CaptureResponse(
                id=item_id,
                processing_status="queued",
                duplicate_of=duplicate_id,
            )
    # The connection context commits before success is returned or logged.
    logger.info(
        "capture_stored",
        extra={
            "item_id": result.id,
            "owner_id": owner_id,
            "client_capture_id": payload.client_capture_id,
        },
    )
    return result


def get_item(settings: Settings, owner_id: UUID, item_id: UUID) -> ItemResponse:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT id, kind, source, content, user_note, mime_type, original_filename, "
            "content_hash, source_url, extracted_text, "
            "processing_status, created_at, captured_at, updated_at FROM item "
            "WHERE owner_id = %s AND id = %s AND deleted_at IS NULL",
            (owner_id, item_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Item not found")
    return ItemResponse.model_validate(row)


def get_processing(
    settings: Settings, owner_id: UUID, item_id: UUID
) -> list[ProcessingRunResponse]:
    with connection(settings) as conn:
        item = conn.execute(
            "SELECT id FROM item WHERE owner_id = %s AND id = %s AND deleted_at IS NULL",
            (owner_id, item_id),
        ).fetchone()
        if item is None:
            raise HTTPException(404, "Item not found")
        rows = conn.execute(
            "SELECT processor, status, attempts, last_error, started_at, finished_at "
            "FROM processing_run WHERE owner_id = %s AND item_id = %s ORDER BY processor",
            (owner_id, item_id),
        ).fetchall()
    return [ProcessingRunResponse.model_validate(row) for row in rows]
