# ADR-0004: Original text and serialized owner capture

**Status:** accepted
**Date:** 2026-09-26

## Context

The text slice needs persistence, parallel retry safety and atomic duplicate
detection. The spec does not explicitly locate original text separately from
extracted or generated data.

## Decision

Store originals in `item.content` with a UTF-8 SHA-256 hash. Use explicit Psycopg
transactions, lock the owner row during capture and enforce unique owner/key pairs.
Commit items and duplicate relations together. Use Alembic for schema changes.

## Consequences

Enrichment cannot overwrite the canonical original. Per-owner throughput is
serialized, acceptable for the single-user MVP. Reused keys return the original
item despite payload changes or deletion. Revisit locking only after measuring load.
