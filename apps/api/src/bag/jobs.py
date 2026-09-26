import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from bag.config import Settings
from bag.db import Row
from bag.ids import uuid7
from bag.processors import PROCESSORS, Outcome, ProcessingItem, ProcessorError
from bag.storage import StoredBlob

logger = logging.getLogger("bag.jobs")

ENRICHMENT_COLUMNS = frozenset({"mime_type", "kind", "extracted_text", "language"})
MAX_ERROR_LENGTH = 500
MAX_RETRY_SECONDS = 3600.0

CLAIM_SQL = """
WITH candidate AS (
    SELECT id FROM job
    WHERE (status = 'queued' AND run_after <= now())
       OR (status = 'running' AND lease_expires_at <= now())
    ORDER BY run_after, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE job SET status = 'running', attempts = job.attempts + 1, worker_id = %s,
    lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
FROM candidate WHERE job.id = candidate.id
RETURNING job.id, job.owner_id, job.item_id, job.processor, job.attempts, job.max_attempts
"""

DERIVE_STATUS_SQL = """
UPDATE item SET processing_status = derived.status, updated_at = now()
FROM (
    SELECT CASE
        WHEN count(*) = 0 THEN 'ready'
        WHEN bool_and(status = 'pending' AND started_at IS NULL) THEN 'queued'
        WHEN bool_or(status IN ('pending', 'running')) THEN 'processing'
        WHEN NOT bool_or(status = 'failed') THEN 'ready'
        WHEN NOT bool_or(status = 'succeeded') THEN 'failed'
        ELSE 'partial' END AS status
    FROM processing_run WHERE owner_id = %s AND item_id = %s
) AS derived
WHERE item.owner_id = %s AND item.id = %s AND item.processing_status <> derived.status
"""


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    owner_id: UUID
    item_id: UUID
    processor: str
    attempts: int
    max_attempts: int


def schedule(
    conn: psycopg.Connection[Row],
    settings: Settings,
    owner_id: UUID,
    item_id: UUID,
    processors: Iterable[str] = PROCESSORS,
) -> list[str]:
    """Reset runs and enqueue jobs inside the caller's transaction.

    Processors with a queued or running job are left alone; the unique active-job
    index rejects duplicates that slip past this check.
    """
    scheduled: list[str] = []
    for processor in processors:
        active = conn.execute(
            "SELECT id FROM job WHERE owner_id = %s AND item_id = %s AND processor = %s "
            "AND status IN ('queued', 'running')",
            (owner_id, item_id, processor),
        ).fetchone()
        if active is not None:
            continue
        conn.execute(
            "INSERT INTO processing_run (id, owner_id, item_id, processor) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (owner_id, item_id, processor) DO UPDATE SET status = 'pending', "
            "attempts = 0, last_error = NULL, started_at = NULL, finished_at = NULL",
            (uuid7(), owner_id, item_id, processor),
        )
        conn.execute(
            "INSERT INTO job (id, owner_id, item_id, processor, max_attempts) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uuid7(), owner_id, item_id, processor, settings.job_max_attempts),
        )
        scheduled.append(processor)
    return scheduled


Scope = Literal["all", "missing", "failed"]


def reprocess(
    conn: psycopg.Connection[Row],
    settings: Settings,
    owner_id: UUID,
    item_id: UUID,
    scope: Scope = "all",
) -> list[str] | None:
    """Schedule processors again for a live item; None when the item is not visible."""
    # Lock order matches claim/finish (job before item) to avoid deadlocks.
    conn.execute(
        "SELECT id FROM job WHERE owner_id = %s AND item_id = %s "
        "AND status IN ('queued', 'running') FOR UPDATE",
        (owner_id, item_id),
    )
    item = conn.execute(
        "SELECT id FROM item WHERE owner_id = %s AND id = %s AND deleted_at IS NULL FOR UPDATE",
        (owner_id, item_id),
    ).fetchone()
    if item is None:
        return None
    if scope == "all":
        processors = list(PROCESSORS)
    else:
        runs = {
            row["processor"]: row["status"]
            for row in conn.execute(
                "SELECT processor, status FROM processing_run WHERE owner_id = %s AND item_id = %s",
                (owner_id, item_id),
            ).fetchall()
        }
        if scope == "missing":
            processors = [name for name in PROCESSORS if name not in runs]
        else:
            processors = [name for name in PROCESSORS if runs.get(name) == "failed"]
    scheduled = schedule(conn, settings, owner_id, item_id, processors)
    derive_status(conn, owner_id, item_id)
    return scheduled


def derive_status(conn: psycopg.Connection[Row], owner_id: UUID, item_id: UUID) -> None:
    conn.execute(DERIVE_STATUS_SQL, (owner_id, item_id, owner_id, item_id))


def claim(conn: psycopg.Connection[Row], worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    row = conn.execute(CLAIM_SQL, (worker_id, lease_seconds)).fetchone()
    if row is None:
        return None
    job = ClaimedJob(**row)
    conn.execute(
        "UPDATE processing_run SET status = 'running', attempts = %s, started_at = now(), "
        "finished_at = NULL WHERE owner_id = %s AND item_id = %s AND processor = %s",
        (job.attempts, job.owner_id, job.item_id, job.processor),
    )
    derive_status(conn, job.owner_id, job.item_id)
    return job


def load_item(conn: psycopg.Connection[Row], job: ClaimedJob) -> ProcessingItem | None:
    row = conn.execute(
        "SELECT i.id, i.owner_id, i.kind, i.mime_type, i.content, "
        "b.sha256, b.size_bytes, b.storage_key FROM item i LEFT JOIN blob b "
        "ON (b.owner_id = i.owner_id AND b.item_id = i.id AND b.role = 'original') "
        "WHERE i.owner_id = %s AND i.id = %s ORDER BY b.created_at, b.id LIMIT 1",
        (job.owner_id, job.item_id),
    ).fetchone()
    if row is None:
        return None
    original = None
    if row["sha256"] is not None:
        original = StoredBlob(row["sha256"], row["size_bytes"], row["storage_key"])
    return ProcessingItem(
        id=row["id"],
        owner_id=row["owner_id"],
        kind=row["kind"],
        mime_type=row["mime_type"],
        content=row["content"],
        original=original,
    )


def finish(
    conn: psycopg.Connection[Row],
    settings: Settings,
    worker_id: str,
    job: ClaimedJob,
    result: Outcome | ProcessorError,
) -> bool:
    """Record a result; returns False when the lease was lost and nothing was written."""
    owned = conn.execute(
        "SELECT id FROM job WHERE id = %s AND worker_id = %s AND status = 'running' FOR UPDATE",
        (job.id, worker_id),
    ).fetchone()
    if owned is None:
        return False
    log: dict[str, object] = {
        "job_id": job.id,
        "item_id": job.item_id,
        "owner_id": job.owner_id,
        "processor": job.processor,
        "attempt": job.attempts,
    }
    if isinstance(result, Outcome):
        unknown = set(result.updates) - ENRICHMENT_COLUMNS
        if unknown:
            raise ValueError(f"Processor may not update columns: {sorted(unknown)}")
        if result.updates:
            assignments = ", ".join(f"{column} = %s" for column in result.updates)
            conn.execute(
                f"UPDATE item SET {assignments}, updated_at = now() "
                "WHERE owner_id = %s AND id = %s",
                (*result.updates.values(), job.owner_id, job.item_id),
            )
        if result.metadata:
            conn.execute(
                "UPDATE item SET metadata = metadata || %s, updated_at = now() "
                "WHERE owner_id = %s AND id = %s",
                (Jsonb({job.processor: result.metadata}), job.owner_id, job.item_id),
            )
        conn.execute(
            "UPDATE processing_run SET status = %s, last_error = NULL, finished_at = now() "
            "WHERE owner_id = %s AND item_id = %s AND processor = %s",
            (result.status, job.owner_id, job.item_id, job.processor),
        )
        conn.execute(
            "UPDATE job SET status = 'succeeded', lease_expires_at = NULL, updated_at = now() "
            "WHERE id = %s",
            (job.id,),
        )
        logger.info(f"job_{result.status}", extra=log)
    else:
        message = str(result)[:MAX_ERROR_LENGTH]
        if result.retryable and job.attempts < job.max_attempts:
            delay = min(settings.job_retry_seconds * 2 ** (job.attempts - 1), MAX_RETRY_SECONDS)
            conn.execute(
                "UPDATE job SET status = 'queued', lease_expires_at = NULL, "
                "run_after = now() + make_interval(secs => %s), updated_at = now() WHERE id = %s",
                (delay, job.id),
            )
            conn.execute(
                "UPDATE processing_run SET status = 'pending', last_error = %s, finished_at = NULL "
                "WHERE owner_id = %s AND item_id = %s AND processor = %s",
                (message, job.owner_id, job.item_id, job.processor),
            )
            logger.warning("job_retry", extra=log)
        else:
            conn.execute(
                "UPDATE job SET status = 'failed', lease_expires_at = NULL, updated_at = now() "
                "WHERE id = %s",
                (job.id,),
            )
            conn.execute(
                "UPDATE processing_run SET status = 'failed', last_error = %s, finished_at = now() "
                "WHERE owner_id = %s AND item_id = %s AND processor = %s",
                (message, job.owner_id, job.item_id, job.processor),
            )
            logger.error("job_failed", extra=log)
    derive_status(conn, job.owner_id, job.item_id)
    return True
