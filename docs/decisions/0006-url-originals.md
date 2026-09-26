# ADR-0006: Preserve URL originals without fetching

**Status:** accepted
**Date:** 2026-09-26

## Context

URL capture must be durable without waiting for remote services. URL parsers may
normalize input, losing its original spelling or changing duplicate semantics.

## Decision

Validate absolute HTTP(S) URLs with an 8192-character bound and reject whitespace,
controls and backslashes. Keep unchanged input in `item.content` and initially in
`source_url`. Hash original UTF-8 bytes and use the shared capture transaction,
owner scoping and idempotency namespace. No DNS or page fetch runs during capture.

## Consequences

Unicode, escapes, query order and fragments survive exactly. Equivalent URLs with
different spellings remain distinct originals. MIME remains unknown and no snapshot
exists. Private-address URLs may be saved; this never authorizes a future fetch.
SSRF validation must happen independently when workers eventually fetch pages.
No schema migration or new dependency is required.
