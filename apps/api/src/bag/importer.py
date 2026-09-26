"""Read an export directory (ADR-0014) back into one owner's bag, idempotently by ID."""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from bag.config import Settings
from bag.db import Row, connection
from bag.export import FORMAT_VERSION, ExportError
from bag.ids import uuid7
from bag.storage import FileSystemStorage, chunks
from bag.tokens import resolve_owner

ITEM_FIELDS = (
    "id, owner_id, client_capture_id, kind, source, source_application, source_url, mime_type, "
    "language, original_filename, title, user_note, content, extracted_text, content_hash, "
    "processing_status, metadata, created_at, captured_at, updated_at, deleted_at"
)
ITEM_VALUES = (
    "%(id)s::uuid, %(owner_id)s::uuid, %(client_capture_id)s::uuid, %(kind)s, %(source)s, "
    "%(source_application)s, "
    "%(source_url)s, %(mime_type)s, %(language)s, %(original_filename)s, %(title)s, "
    "%(user_note)s, %(content)s, %(extracted_text)s, %(content_hash)s, %(processing_status)s, "
    "%(metadata)s, %(created_at)s::timestamptz, %(captured_at)s::timestamptz, "
    "%(updated_at)s::timestamptz, %(deleted_at)s::timestamptz"
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ExportError(f"Missing {path.name}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExportError(f"{path.name} line {number} is not valid JSON") from exc
            if not isinstance(row, dict):
                raise ExportError(f"{path.name} line {number} is not an object")
            rows.append(row)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in chunks(source):
            digest.update(chunk)
    return digest.hexdigest()


def _inserted(conn: psycopg.Connection[Row], sql: str, params: dict[str, Any]) -> bool:
    return conn.execute(sql + " ON CONFLICT DO NOTHING RETURNING 1", params).fetchone() is not None


def import_bag(
    settings: Settings,
    storage: FileSystemStorage,
    source: Path,
    owner_id: UUID | None = None,
) -> dict[str, int]:
    """Verify everything first, then insert missing rows in one transaction."""
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise ExportError("Missing manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "bag-export" or manifest.get("version") != FORMAT_VERSION:
        raise ExportError("Unsupported export format or version")
    items = _read_jsonl(source / "items.jsonl")
    blobs = _read_jsonl(source / "blobs.jsonl")
    tags = _read_jsonl(source / "tags.jsonl")
    collections = _read_jsonl(source / "collections.jsonl")
    relations = _read_jsonl(source / "relations.jsonl")
    runs = _read_jsonl(source / "processing_runs.jsonl")
    # Every referenced object must exist and match its hash before any row is written.
    for blob in blobs:
        path = source / "objects" / str(blob["sha256"])
        if not path.is_file() or _sha256(path) != blob["sha256"]:
            raise ExportError(f"Object {blob['sha256'][:12]}… is missing or corrupt")
    counts = dict.fromkeys(
        ("items", "blobs", "tags", "collections", "relations", "processing_runs", "objects"), 0
    )
    stored: set[str] = set()
    for blob in blobs:
        if blob["sha256"] in stored:
            continue
        with (source / "objects" / str(blob["sha256"])).open("rb") as data:
            storage.put(data, int(blob["size_bytes"]) + 1)
        stored.add(str(blob["sha256"]))
    counts["objects"] = len(stored)
    with connection(settings) as conn:
        owner = resolve_owner(conn, owner_id)
        ids = [UUID(str(item["id"])) for item in items]
        foreign = conn.execute(
            "SELECT count(*) AS n FROM item WHERE id = ANY(%s) AND owner_id <> %s", (ids, owner)
        ).fetchone()
        if foreign and foreign["n"]:
            raise ExportError("An item ID in the export belongs to another owner")
        for item in items:
            params = {key: item.get(key) for key in ITEM_FIELDS.split(", ")}
            params["owner_id"] = owner
            params["metadata"] = Jsonb(item.get("metadata") or {})
            if _inserted(conn, f"INSERT INTO item ({ITEM_FIELDS}) VALUES ({ITEM_VALUES})", params):
                counts["items"] += 1
        for blob in blobs:
            if _inserted(
                conn,
                "INSERT INTO blob (id, owner_id, item_id, role, storage_key, sha256, size_bytes, "
                "mime_type, created_at) VALUES (%(id)s::uuid, %(owner)s::uuid, %(item_id)s::uuid, "
                "%(role)s, %(storage_key)s, %(sha256)s, %(size_bytes)s, %(mime_type)s, "
                "%(created_at)s::timestamptz)",
                {**blob, "owner": owner},
            ):
                counts["blobs"] += 1
        for kind, rows, link, column in (
            ("tag", tags, "item_tag", "tag_id"),
            ("collection", collections, "item_collection", "collection_id"),
        ):
            for named in rows:
                if _inserted(
                    conn,
                    f"INSERT INTO {kind} (id, owner_id, name, created_at) VALUES "
                    "(%(id)s::uuid, %(owner)s::uuid, %(name)s, %(created_at)s::timestamptz)",
                    {**named, "owner": owner},
                ):
                    counts[f"{kind}s"] += 1
                existing = conn.execute(
                    f"SELECT id FROM {kind} WHERE owner_id = %s AND name = %s",
                    (owner, named["name"]),
                ).fetchone()
                assert existing is not None
                for assignment in named.get("items", []):
                    if kind == "tag":
                        _inserted(
                            conn,
                            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by, "
                            "confidence) VALUES (%(id)s::uuid, %(owner)s::uuid, %(item)s::uuid, "
                            "%(named)s::uuid, %(created_by)s, %(confidence)s::double precision)",
                            {
                                "id": uuid7(),
                                "owner": owner,
                                "item": assignment["item_id"],
                                "named": existing["id"],
                                "created_by": assignment.get("created_by", "user"),
                                "confidence": assignment.get("confidence"),
                            },
                        )
                    else:
                        _inserted(
                            conn,
                            f"INSERT INTO {link} (id, owner_id, item_id, {column}) VALUES "
                            "(%(id)s::uuid, %(owner)s::uuid, %(item)s::uuid, %(named)s::uuid)",
                            {
                                "id": uuid7(),
                                "owner": owner,
                                "item": assignment,
                                "named": existing["id"],
                            },
                        )
        for relation in relations:
            if _inserted(
                conn,
                "INSERT INTO relation (id, owner_id, source_item_id, target_item_id, "
                "relation_type, created_by, confidence, metadata, created_at) VALUES "
                "(%(id)s::uuid, %(owner)s::uuid, %(source_item_id)s::uuid, "
                "%(target_item_id)s::uuid, %(relation_type)s, %(created_by)s, "
                "%(confidence)s::double precision, %(metadata)s, %(created_at)s::timestamptz)",
                {**relation, "owner": owner, "metadata": Jsonb(relation.get("metadata") or {})},
            ):
                counts["relations"] += 1
        for run in runs:
            if _inserted(
                conn,
                "INSERT INTO processing_run (id, owner_id, item_id, processor, status, attempts, "
                "last_error, started_at, finished_at) VALUES (%(id)s::uuid, %(owner)s::uuid, "
                "%(item_id)s::uuid, %(processor)s, %(status)s, %(attempts)s, %(last_error)s, "
                "%(started_at)s::timestamptz, %(finished_at)s::timestamptz)",
                {**run, "owner": owner},
            ):
                counts["processing_runs"] += 1
    return counts
