# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

Phases 0 and 1 are implemented: text/URL/file capture survives container recreation,
and server-side token creation, listing, revocation and lost-token recovery are available.
Phase 2 has a leased PostgreSQL job queue, a worker with bounded retries and recovery,
`mime_detect`/`text_extract` processors and a derived item status.

## Phase 2 — Processing and search

- [ ] Add `POST /items/{id}/reprocess` and a `bag reprocess` command that reset runs
      and enqueue jobs, including for items captured before migration `0002_jobs`.
- [ ] Add a lightweight language detector processor that sets `item.language`
      (`de`/`en`) so the German/English search configuration applies.
- [ ] Implement SSRF-safe URL fetching with DNS/IP validation, connection pinning,
      redirect revalidation and size/time limits before enabling a `url_fetch` processor.
- [ ] Add `metadata` and `image_meta` processors (basic file/image metadata) and
      decide on PDF text extraction in an ADR before adding a dependency.
- [ ] Add multilingual search and item-list/filter APIs using the generated vector.
- [ ] Add soft delete, restore, explicit purge and reference-safe storage garbage collection;
      purge must remove jobs and runs before items.
- [ ] Reclaim crash-left `.upload-*` files and unreferenced published objects through
      an explicit job coordinated with active captures; never delete another owner's references.
- [ ] Prune completed `job` rows after a retention period once purge exists.
- [ ] Implement export of originals and JSONL metadata through the API.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
