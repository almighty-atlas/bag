# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

Phases 0 and 1 are implemented: text/URL/file capture survives container recreation,
and server-side token creation, listing, revocation and lost-token recovery are available.
Phase 2 has a leased PostgreSQL job queue, a worker with bounded retries and recovery,
`mime_detect`/`text_extract`/`language`/`url_fetch` processors, a derived item status,
reprocessing through the API and `bag reprocess`, list/search endpoints with
filters, ranking and snippets, idempotent trash/restore, explicit `bag purge`
and `bag gc` maintenance commands, editing of title, note and language, tags and
collections with filters, and `bag export`.

## Phase 2 — Processing and search

- [ ] Serve `snapshot` blobs through the API (sanitized or sandboxed, never rendered inline).
- [ ] Add `metadata` and `image_meta` processors (basic file/image metadata) and
      decide on PDF text extraction in an ADR before adding a dependency.
- [ ] Add rename/delete for tags and collections (assignments must go with them).
- [ ] Prune completed `job` rows after a retention period.
- [ ] Decide whether the worker should run `purge`/`gc` on a schedule or leave it to cron.
- [ ] Add `bag import <dir>` reading export format version 1, idempotent by item ID.

## Phase 3 — Web / PWA

- [ ] Web: kind/date filters, tag and collection browsing pages, snapshot preview,
      keyboard shortcut for capture.
- [ ] Web: frontend unit tests (vitest) and an end-to-end smoke in CI against the stack.
- [ ] PWA: service worker for the offline shell and a share target for URLs/text/files.
- [ ] Rate-limit login attempts per username and address.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
