# ADR-0013: Explicit purge and lock-coordinated storage garbage collection

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 5.8 wants trashed items purged after a retention period and storage
objects collected once unreferenced. Objects are shared across items and owners
through content addressing, captures publish an object before committing its blob
row, and crashes leave `.upload-*` temporaries. Nothing may delete bytes that a
concurrent capture is about to reference.

## Decision

`bag purge` hard-deletes trashed items whose `deleted_at` is older than
`BAG_TRASH_RETENTION_DAYS` (override with `--retention-days`, preview with
`--dry-run`), one transaction per item, re-checking the state under a row lock and
deleting jobs, runs, relations, tags, collections and blob rows before the item.
It never touches storage. `bag gc` scans the storage root without locks for
objects and temporaries older than `--min-age-hours` (default 1), then, per batch,
takes every owner's capture lock (`SELECT ... FOR UPDATE` on `user`), re-checks
`blob.storage_key` references across all owners and unlinks only unreferenced
files. Both are explicit CLI commands, not automatic background work.

## Consequences

Captures hold their owner lock while publishing, so GC waits for in-flight
captures (without a statement timeout, since a large upload may take a while)
and blocks new ones only for the short unlink batch; a capture that
starts afterwards recreates any object it needs. Trashed items keep their blob
rows, so their bytes survive until purge. A downgraded or restored database with
stale blob rows could reference missing objects; downloads detect that as 503.
Neither command runs on a schedule; operators or a future scheduler invoke them.
