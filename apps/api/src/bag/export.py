import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from bag.config import Settings
from bag.db import Row, connection
from bag.storage import FileSystemStorage, StoredBlob, sync_directory
from bag.tokens import resolve_owner

FORMAT_VERSION = 1
ITEM_COLUMNS = (
    "id, client_capture_id, kind, source, source_application, source_url, mime_type, language, "
    "original_filename, title, user_note, content, extracted_text, content_hash, "
    "processing_status, metadata, created_at, captured_at, updated_at, deleted_at"
)


class ExportError(Exception):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _write_jsonl(path: Path, rows: list[Row]) -> int:
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps({k: _jsonable(v) for k, v in row.items()}, ensure_ascii=False))
            out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    return len(rows)


def _rows(conn: psycopg.Connection[Row], sql: str, owner_id: UUID) -> list[Row]:
    return conn.execute(sql, (owner_id,)).fetchall()


def export_bag(
    settings: Settings,
    storage: FileSystemStorage,
    target: Path,
    owner_id: UUID | None = None,
) -> dict[str, int]:
    """Write every original object plus JSONL metadata of one owner into an empty directory."""
    if target.exists() and any(target.iterdir()):
        raise ExportError("Target directory must be empty")
    target.mkdir(parents=True, exist_ok=True)
    (target / "objects").mkdir()
    with connection(settings) as conn:
        # One snapshot: every table is read in the same repeatable-read transaction.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        owner = resolve_owner(conn, owner_id)
        items = _rows(
            conn, f"SELECT {ITEM_COLUMNS} FROM item WHERE owner_id = %s ORDER BY id", owner
        )
        blobs = _rows(
            conn,
            "SELECT id, item_id, role, storage_key, sha256, size_bytes, mime_type, created_at "
            "FROM blob WHERE owner_id = %s ORDER BY id",
            owner,
        )
        tags = _rows(
            conn,
            "SELECT t.id, t.name, t.created_at, coalesce(json_agg(json_build_object("
            "'item_id', it.item_id, 'created_by', it.created_by, 'confidence', it.confidence)) "
            "FILTER (WHERE it.id IS NOT NULL), '[]') AS items FROM tag t LEFT JOIN item_tag it "
            "ON (it.owner_id = t.owner_id AND it.tag_id = t.id) WHERE t.owner_id = %s "
            "GROUP BY t.id ORDER BY t.id",
            owner,
        )
        collections = _rows(
            conn,
            "SELECT c.id, c.name, c.created_at, coalesce(json_agg(ic.item_id) "
            "FILTER (WHERE ic.id IS NOT NULL), '[]') AS items FROM collection c "
            "LEFT JOIN item_collection ic ON (ic.owner_id = c.owner_id "
            "AND ic.collection_id = c.id) WHERE c.owner_id = %s GROUP BY c.id ORDER BY c.id",
            owner,
        )
        relations = _rows(
            conn,
            "SELECT id, source_item_id, target_item_id, relation_type, created_by, confidence, "
            "metadata, created_at FROM relation WHERE owner_id = %s ORDER BY id",
            owner,
        )
        runs = _rows(
            conn,
            "SELECT id, item_id, processor, status, attempts, last_error, started_at, "
            "finished_at FROM processing_run WHERE owner_id = %s ORDER BY id",
            owner,
        )
    counts = {
        "items": _write_jsonl(target / "items.jsonl", items),
        "blobs": _write_jsonl(target / "blobs.jsonl", blobs),
        "tags": _write_jsonl(target / "tags.jsonl", tags),
        "collections": _write_jsonl(target / "collections.jsonl", collections),
        "relations": _write_jsonl(target / "relations.jsonl", relations),
        "processing_runs": _write_jsonl(target / "processing_runs.jsonl", runs),
    }
    copied = 0
    for blob in blobs:
        stored = StoredBlob(blob["sha256"], blob["size_bytes"], blob["storage_key"])
        destination = target / "objects" / stored.sha256
        if destination.exists():
            continue
        # Verified read: a corrupt or missing original aborts the export loudly.
        with storage.open_verified(stored) as source, destination.open("wb") as out:
            shutil.copyfileobj(source, out)
            out.flush()
            os.fsync(out.fileno())
        copied += 1
    sync_directory(target / "objects")
    manifest = {
        "format": "bag-export",
        "version": FORMAT_VERSION,
        "owner_id": str(owner),
        "exported_at": datetime.now().astimezone().isoformat(),
        "counts": {**counts, "objects": copied},
    }
    with (target / "manifest.json").open("w", encoding="utf-8") as out:
        json.dump(manifest, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    sync_directory(target)
    return {**counts, "objects": copied}
