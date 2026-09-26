# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

Phase 0, text capture and filesystem file capture/download are implemented.
Phase 1 is not complete.

## Phase 1 — Remaining capture backend

- [ ] Add URL capture preserving input without fetching on the capture path.
- [ ] Extend shared capture idempotency/duplicate semantics to URL input.
- [ ] Add administrative token creation/revocation and explicit lost-token recovery
      without replacing the user or invalidating existing tokens.
- [ ] Verify URL originals survive full Compose restarts alongside text/files.

## Phase 2 — After capture is stable

- [ ] Implement PostgreSQL jobs and worker claim/retry/recovery (`FOR UPDATE SKIP LOCKED`,
      leases and bounded retries); replace the idle worker foundation.
- [ ] Enqueue processing transactionally with capture, add initial processors and
      derive item status from processing runs. Preserve originals on failure.
- [ ] Implement SSRF-safe URL fetching with DNS/IP validation, connection pinning,
      redirect revalidation and size/time limits before enabling fetch jobs.
- [ ] Add multilingual search and item-list/filter APIs using the generated vector.
- [ ] Add soft delete, restore, explicit purge and reference-safe storage garbage collection.
- [ ] Reclaim crash-left `.upload-*` files and unreferenced published objects through
      an explicit job coordinated with active captures; never delete another owner's references.
- [ ] Implement export of originals and JSONL metadata through the API.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
