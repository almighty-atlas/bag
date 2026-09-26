# ADR-0002: PostgreSQL-backed processing queue

**Status:** accepted
**Date:** 2026-09-26

## Context

Enrichment needs asynchronous retries without another stateful service or a
transaction gap between item persistence and scheduling.

## Decision

Use a PostgreSQL jobs table with `FOR UPDATE SKIP LOCKED` when processing arrives.
Insert jobs transactionally with items and record outcomes in `processing_run`.
This foundation has an idle worker and no queue execution yet.

## Consequences

No Redis dependency. Add leases, retry limits, recovery and idempotent handlers
before scheduling work. Text with no scheduled processors is ready on commit;
worker health does not imply that jobs are being processed.
