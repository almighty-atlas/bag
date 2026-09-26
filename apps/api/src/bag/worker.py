import logging
import os
import secrets
import socket
import threading

import psycopg

from bag.config import Settings
from bag.db import connection
from bag.fetch import UrlFetcher
from bag.jobs import ClaimedJob, claim, finish, load_item
from bag.processors import PROCESSORS, Outcome, Processor, ProcessorError
from bag.storage import BlobStorage

logger = logging.getLogger("bag.worker")


class Worker:
    def __init__(
        self,
        settings: Settings,
        storage: BlobStorage,
        processors: dict[str, Processor] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.processors = dict(PROCESSORS if processors is None else processors)
        fetcher = self.processors.get("url_fetch")
        if isinstance(fetcher, UrlFetcher):
            self.processors["url_fetch"] = fetcher.with_settings(settings)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"

    def run_once(self) -> bool:
        """Claim and complete at most one job; returns False when the queue is idle."""
        with connection(self.settings) as conn:
            job = claim(conn, self.worker_id, self.settings.job_lease_seconds)
            if job is None:
                return False
            item = None if job.attempts > job.max_attempts else load_item(conn, job)
        # The claim is committed; the lease bounds how long this attempt may run.
        logger.info(
            "job_claimed",
            extra={
                "job_id": job.id,
                "item_id": job.item_id,
                "owner_id": job.owner_id,
                "processor": job.processor,
                "attempt": job.attempts,
            },
        )
        result: Outcome | ProcessorError
        processor = self.processors.get(job.processor)
        if job.attempts > job.max_attempts:
            result = ProcessorError("Lease expired after the final attempt", retryable=False)
        elif item is None:
            result = ProcessorError("Item not found", retryable=False)
        elif processor is None:
            result = ProcessorError("Unknown processor", retryable=False)
        else:
            try:
                result = processor(item, self.storage)
            except ProcessorError as exc:
                result = exc
            except Exception as exc:  # noqa: BLE001 - bounded retries, never lose the run
                result = ProcessorError(f"Unexpected {type(exc).__name__}")
        self.finish(job, result)
        return True

    def finish(self, job: ClaimedJob, result: Outcome | ProcessorError) -> None:
        try:
            with connection(self.settings) as conn:
                if not finish(conn, self.settings, self.worker_id, job, result):
                    logger.warning("job_lease_lost", extra={"job_id": job.id})
                    return
        except (psycopg.errors.DataError, psycopg.errors.ProgramLimitExceeded):
            # Deterministic storage limits (e.g. search vector size) would fail every retry.
            with connection(self.settings) as conn:
                finish(
                    conn,
                    self.settings,
                    self.worker_id,
                    job,
                    ProcessorError("Result exceeds a database limit", retryable=False),
                )

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                worked = self.run_once()
            except psycopg.Error:
                logger.error("worker_database_unavailable")
                worked = False
            except Exception:  # noqa: BLE001 - keep the loop alive, leases recover the job
                logger.error("worker_loop_error")
                worked = False
            if not worked:
                stop.wait(self.settings.worker_poll_seconds)
