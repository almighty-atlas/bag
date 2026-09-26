# ADR-0009: Reprocessing resets runs but never duplicates active jobs

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 9 lists `POST /items/{id}/reprocess` without defining its semantics.
Items captured before the job queue existed have no runs, failed runs need a
manual retry after their attempt budget is spent, and new processors must be
applicable to existing items. Concurrent workers may hold a job for the same item.

## Decision

Reprocessing an item resets the `processing_run` of each selected processor to
`pending` with zero attempts and inserts a new `job`, unless a queued or running
job for that processor already exists; those are left untouched. A partial unique
index enforces at most one active job per owner, item and processor. The API
endpoint always selects every registered processor and returns the runs with 202.
`bag reprocess` iterates an owner's live items in separate transactions with a
scope of `missing` (default), `failed` or `all`. Locks are taken in job-then-item
order, matching claim and finalize.

## Consequences

Failed or legacy items can be enriched without recapture, and adding a processor
only requires `bag reprocess`. Reprocessing is idempotent while jobs are pending,
so clients may retry it freely. A permanently failing result fails again after each
request, which is visible in the runs. Bulk reprocessing loads only item IDs but
runs one transaction per item, so very large bags take proportionally long.
Job history grows with every reprocess until pruning exists.
