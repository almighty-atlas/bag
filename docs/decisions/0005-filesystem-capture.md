# ADR-0005: Durable filesystem capture and conservative MIME detection

**Status:** accepted
**Date:** 2026-09-26

## Context

File capture must acknowledge only durable originals, preserve duplicate captures,
ignore untrusted filenames/MIME claims, and survive publication/commit failures.

## Decision

Use a `BlobStorage` protocol and a POSIX filesystem backend with bounded chunk
copying, SHA-256, file fsync, atomic no-replace hard-link publication and directory
fsync before committing item/blob/relation rows. Verify existing objects before
reuse and verify originals before downloads. Keep failed-publication/commit orphans
for a future explicit GC job, rather than deleting possibly shared objects.

Detect a bounded signature prefix with pure-Python `filetype`; unknown formats use
octet-stream. Download every file as an attachment/octet-stream, never inline.
Basic type verification is on capture; complex extraction stays in future workers.

## Consequences

No system libmagic dependency or filename-based storage paths. Signature detection
is deliberately incomplete and is not a security validation of a file's contents.
The backend requires hard links and directory fsync; S3 remains a separate future
implementation. Downloads incur a full verification read before streaming. Temporary
spooling trades disk I/O/space for bounded memory. Backups require both database and
storage; orphan cleanup must coordinate with in-flight captures and all owners.
