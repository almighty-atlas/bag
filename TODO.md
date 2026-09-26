# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

Phases 0 and 1 are implemented: text/URL/file capture survives container recreation,
and server-side token creation, listing, revocation and lost-token recovery are available.
Phase 2 has a leased PostgreSQL job queue, a worker with bounded retries and recovery,
`mime_detect`/`text_extract`/`language` processors, a derived item status,
reprocessing through the API and `bag reprocess`, and list/search endpoints with
filters, ranking and snippets.

## Phase 2 — Processing and search

- [ ] Implement SSRF-safe URL fetching with DNS/IP validation, connection pinning,
      redirect revalidation and size/time limits before enabling a `url_fetch` processor.
- [ ] Add `metadata` and `image_meta` processors (basic file/image metadata) and
      decide on PDF text extraction in an ADR before adding a dependency.
- [ ] Add `PATCH /items/{id}` for title, note and language; a user-set language must
      write `metadata.language.user = true` so detection never overwrites it.
- [ ] Add tag and collection APIs and the corresponding `tag`/`collection` filters
      on list and search.
- [ ] Add soft delete, restore, explicit purge and reference-safe storage garbage collection;
      purge must remove jobs and runs before items.
- [ ] Reclaim crash-left `.upload-*` files and unreferenced published objects through
      an explicit job coordinated with active captures; never delete another owner's references.
- [ ] Prune completed `job` rows after a retention period once purge exists.
- [ ] Implement export of originals and JSONL metadata through the API.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
