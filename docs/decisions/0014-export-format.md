# ADR-0014: Export as JSONL plus content-addressed objects

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 11 makes export an MVP feature: every original object plus JSONL
files for items, tags, collections and relations, stable enough for a later
import. Blob rows reference shared objects, tags carry per-assignment
provenance, and trashed items must not vanish from a backup-grade export.

## Decision

`bag export <dir>` writes into an empty directory: `manifest.json` (format name,
version 1, owner, timestamp, counts), `items.jsonl` (all item columns except the
generated search vector, including trashed items), `blobs.jsonl`, `tags.jsonl`
and `collections.jsonl` (each name with its item assignments and, for tags,
provenance and confidence), `relations.jsonl`, `processing_runs.jsonl`, and
`objects/<sha256>` for every referenced original, copied through the verified
reader and fsynced. All tables are read in one repeatable-read transaction.
IDs and timestamps are strings (UUID, ISO 8601 with offset). One owner per export.

## Consequences

The export is a complete, offline-readable copy for one owner and the input
contract for a future `bag import`. Objects are deduplicated by hash, so sizes
match storage, not the sum of items. A corrupt or missing original fails the
export instead of silently omitting it. Text originals live only in
`items.jsonl`; readers must treat every string as untrusted content.
