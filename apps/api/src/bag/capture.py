import hashlib
import logging
from uuid import UUID

from fastapi import HTTPException

from bag.config import Settings
from bag.db import Row, connection
from bag.ids import uuid7
from bag.schemas import CaptureResponse, ItemResponse, TextCapture

logger = logging.getLogger("bag.capture")


def capture_text(settings: Settings, owner_id: UUID, payload: TextCapture) -> CaptureResponse:
    digest = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
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
            duplicate = conn.execute(
                "SELECT id FROM item WHERE owner_id = %s AND content_hash = %s "
                "AND deleted_at IS NULL ORDER BY created_at, id LIMIT 1",
                (owner_id, digest),
            ).fetchone()
            item_id = uuid7()
            conn.execute(
                "INSERT INTO item (id, owner_id, client_capture_id, kind, source, "
                "content, user_note, content_hash, mime_type, processing_status, captured_at) "
                "VALUES (%s, %s, %s, 'text', %s, %s, %s, %s, 'text/plain', 'ready', "
                "coalesce(%s, now()))",
                (
                    item_id,
                    owner_id,
                    payload.client_capture_id,
                    payload.source,
                    payload.content,
                    payload.user_note,
                    digest,
                    payload.captured_at,
                ),
            )
            duplicate_id = duplicate["id"] if duplicate else None
            if duplicate_id is not None:
                conn.execute(
                    "INSERT INTO relation (id, owner_id, source_item_id, target_item_id, "
                    "relation_type, created_by) VALUES (%s, %s, %s, %s, 'duplicate_of', 'system')",
                    (uuid7(), owner_id, item_id, duplicate_id),
                )
            result = CaptureResponse(
                id=item_id,
                processing_status="ready",
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
            "SELECT id, kind, source, content, user_note, mime_type, content_hash, "
            "processing_status, created_at, captured_at, updated_at FROM item "
            "WHERE owner_id = %s AND id = %s AND deleted_at IS NULL",
            (owner_id, item_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Item not found")
    return ItemResponse.model_validate(row)
