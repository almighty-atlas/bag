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

- [ ] Add a `pdf_text` processor with `pypdf` as decided in ADR-0018.
- [ ] Decide whether the worker should run `purge`/`gc` on a schedule or leave it to cron.
- [ ] Add `bag import <dir>` reading export format version 1, idempotent by item ID.

## Phase 3 — Web / PWA

- [ ] Web: kind/date filters, tag and collection browsing pages, snapshot preview,
      keyboard shortcut for capture.
- [ ] Web: an end-to-end smoke in CI against the running stack (login, capture, search).
- [ ] PWA: POST share target with a service worker so shared files reach capture.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
