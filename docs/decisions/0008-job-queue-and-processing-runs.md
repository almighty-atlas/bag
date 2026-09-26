# ADR-0008: Leased job queue with pure processors

**Status:** accepted
**Date:** 2026-09-26

## Context

ADR-0002 chose a PostgreSQL queue but deferred leases, retries, recovery and
handlers. Spec sections 5.6 and 7 require one `processing_run` per processor and
item as the source of enrichment state, independent retryable processors and a
derived item status. Processors must never damage originals, and a worker crash
must not lose or duplicate work.

## Decision

Keep two tables: `job` rows are queue entries; `processing_run` rows are the
per-item state. A worker claims one job per transaction with
`FOR UPDATE SKIP LOCKED`, increments `attempts` and holds a lease. Jobs whose
lease expired are claimable again; a reclaim beyond `max_attempts` fails the run.
Failures retry with exponential backoff up to `BAG_JOB_MAX_ATTEMPTS`, unless the
processor marks the error permanent.

Processors are pure functions receiving an item snapshot and storage and returning
an outcome (`succeeded` or `skipped`) with updates limited to enrichment columns.
The worker writes results, run state, job state and the derived item status in one
finalize transaction, conditioned on still holding the lease; results from a lost
lease are discarded. Every capture enqueues every registered processor; processors
decide themselves whether to skip, and none depends on another having run.

## Consequences

Work is executed at least once, applied at most once per attempt, and bounded.
Originals, notes and titles are outside the writable column set. Duplicate
scheduling of trivially skipped processors costs two small rows per capture.
Results larger than database limits (search vector size) fail permanently instead
of looping. Reprocessing, per-processor ordering and global jobs such as purge or
garbage collection need additional job kinds and are listed in `TODO.md`.
