# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

Phase 0 and the first text-capture slice are implemented. Phase 1 is not complete.

## Phase 1 — Remaining capture backend

- [ ] Implement content-addressed filesystem storage with atomic writes, file/directory
      fsync, integrity checks and an abstraction for a later S3 backend.
- [ ] Add multipart file capture with streaming limits, content-based MIME detection,
      untrusted display filenames and original download as an attachment.
- [ ] Add URL capture preserving input without fetching on the capture path.
- [ ] Extend idempotency and duplicate detection to file/URL capture; test concurrent
      uploads and crashes between blob persistence and database commit.
- [ ] Add the `bag-storage` volume and document backup/restore of database plus blobs.
- [ ] Add administrative token creation/revocation and explicit lost-token recovery
      without replacing the user or invalidating existing tokens.
- [ ] Verify file, URL and text originals survive full Compose restarts.

## Phase 2 — After capture is stable

- [ ] Implement PostgreSQL jobs and worker claim/retry/recovery (`FOR UPDATE SKIP LOCKED`,
      leases and bounded retries); replace the idle worker foundation.
- [ ] Enqueue processing transactionally with capture, add initial processors and
      derive item status from processing runs. Preserve originals on failure.
- [ ] Implement SSRF-safe URL fetching with DNS/IP validation, connection pinning,
      redirect revalidation and size/time limits before enabling fetch jobs.
- [ ] Add multilingual search and item-list/filter APIs using the generated vector.
- [ ] Add soft delete, restore, explicit purge and reference-safe storage garbage collection.
- [ ] Implement export of originals and JSONL metadata through the API.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
