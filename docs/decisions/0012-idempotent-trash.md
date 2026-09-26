# ADR-0012: Idempotent trash and restore

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 5.8 defines soft deletion, restore and a later purge, but not how
repeated or racing requests behave. Clients with retry queues may send the same
delete or restore twice.

## Decision

`DELETE /items/{id}` sets `deleted_at` once and answers 204 for live and already
trashed items alike; `POST /items/{id}/restore` clears it and answers 200 with the
item whether or not it was trashed. Only unknown or foreign items are 404. Trashed
items stay readable through listings and search with `trashed=true`, but detail,
download, processing and reprocess routes treat them as absent. Capture replays
keep returning a trashed item, and duplicate detection ignores trash so a recapture
becomes a new root. Storage objects are untouched until an explicit purge.

## Consequences

Retries are safe and need no state on the client. Restoring never recreates data
because nothing was removed. A trashed item's jobs still run, which keeps restore
instant at the cost of some wasted work. Purge with retention and storage garbage
collection remain separate explicit jobs.
