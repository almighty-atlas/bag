# ADR-0003: Content-addressed blob layout

**Status:** accepted
**Date:** 2026-09-26

## Context

Original files need integrity checks, safe paths and sharing of identical bytes
without merging item identity.

## Decision

Use SHA-256 keys `ab/cd/<full-lowercase-hash>`, enforced by the blob schema. Keep
filenames as display metadata. Text originals live in `item.content`; filesystem
and S3 backends are deferred to binary capture work.

## Consequences

Future writes must be atomic and durable before acknowledgement. Garbage collection
must check live and trashed blob references. No blob volume is required in this slice.
