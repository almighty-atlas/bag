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


## Phase 3 — Web / PWA

- [ ] Web: browser-driven end-to-end test (Playwright) for the share-target and preview flows,
      which curl cannot exercise.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
