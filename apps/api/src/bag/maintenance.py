import logging
import time
from datetime import timedelta
from uuid import UUID

import psycopg

from bag.config import Settings
from bag.db import Row, connection
from bag.storage import FileSystemStorage, StoredObject

logger = logging.getLogger("bag.maintenance")

GC_BATCH = 500
# Child rows first; every statement is owner-scoped.
PURGE_ORDER = (
    "DELETE FROM job WHERE owner_id = %(owner)s AND item_id = %(item)s",
    "DELETE FROM processing_run WHERE owner_id = %(owner)s AND item_id = %(item)s",
    "DELETE FROM relation WHERE owner_id = %(owner)s "
    "AND (source_item_id = %(item)s OR target_item_id = %(item)s)",
    "DELETE FROM item_tag WHERE owner_id = %(owner)s AND item_id = %(item)s",
    "DELETE FROM item_collection WHERE owner_id = %(owner)s AND item_id = %(item)s",
    "DELETE FROM blob WHERE owner_id = %(owner)s AND item_id = %(item)s",
    "DELETE FROM item WHERE owner_id = %(owner)s AND id = %(item)s",
)


def _owners(conn: psycopg.Connection[Row], owner_id: UUID | None) -> list[UUID]:
    if owner_id is not None:
        return [owner_id]
    return [row["id"] for row in conn.execute('SELECT id FROM "user" ORDER BY id').fetchall()]


def purge_items(
    settings: Settings,
    *,
    retention: timedelta | None = None,
    owner_id: UUID | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Hard-delete trashed items older than the retention, one transaction each."""
    retention = timedelta(days=settings.trash_retention_days) if retention is None else retention
    with connection(settings) as conn:
        expired = [
            (owner, row["id"])
            for owner in _owners(conn, owner_id)
            for row in conn.execute(
                "SELECT id FROM item WHERE owner_id = %s AND deleted_at IS NOT NULL "
                "AND deleted_at <= now() - %s ORDER BY id",
                (owner, retention),
            ).fetchall()
        ]
    purged = 0
    for owner, item_id in expired:
        if dry_run:
            continue
        with connection(settings) as conn:
            # Re-check under the row lock: a restore may have raced the listing.
            row = conn.execute(
                "SELECT id FROM item WHERE owner_id = %s AND id = %s AND deleted_at IS NOT NULL "
                "AND deleted_at <= now() - %s FOR UPDATE",
                (owner, item_id, retention),
            ).fetchone()
            if row is None:
                continue
            for statement in PURGE_ORDER:
                conn.execute(statement, {"owner": owner, "item": item_id})
        purged += 1
        logger.info("item_purged", extra={"item_id": item_id, "owner_id": owner})
    return {"expired": len(expired), "purged": purged}


def _unreferenced(
    conn: psycopg.Connection[Row], candidates: list[StoredObject]
) -> list[StoredObject]:
    keys = [obj.storage_key for obj in candidates if obj.storage_key is not None]
    referenced = {
        row["storage_key"]
        for row in conn.execute(
            # Deliberately across all owners: physical objects are shared.
            "SELECT DISTINCT storage_key FROM blob WHERE storage_key = ANY(%s)",
            (keys,),
        ).fetchall()
    }
    return [obj for obj in candidates if obj.storage_key not in referenced]


def collect_garbage(
    settings: Settings,
    storage: FileSystemStorage,
    *,
    min_age: timedelta = timedelta(hours=1),
    dry_run: bool = False,
) -> dict[str, int]:
    """Remove unreferenced objects and crash-left temporaries.

    Candidates are gathered without locks, then deleted in bounded batches while
    holding every owner's capture lock and re-checking references, so a capture
    that publishes or reuses an object can never lose it.
    """
    cutoff = time.time() - min_age.total_seconds()
    candidates = [obj for obj in storage.scan() if obj.modified_at <= cutoff]
    result = {"scanned": len(candidates), "removed": 0, "freed_bytes": 0, "kept": 0}
    for start in range(0, len(candidates), GC_BATCH):
        batch = candidates[start : start + GC_BATCH]
        with connection(settings) as conn:
            # Waiting for a large upload to commit may exceed the default statement timeout.
            conn.execute("SET LOCAL statement_timeout = 0")
            conn.execute('SELECT id FROM "user" ORDER BY id FOR UPDATE')
            doomed = _unreferenced(conn, batch)
            result["kept"] += len(batch) - len(doomed)
            for obj in doomed:
                if not dry_run:
                    storage.remove(obj)
                result["removed"] += 1
                result["freed_bytes"] += obj.size_bytes
    logger.info("storage_collected")
    return result
