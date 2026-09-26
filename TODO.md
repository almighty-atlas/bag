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

## Phase 4 — Linux experience (not started; confirm the GTK4 choice in an ADR first)

Phases 2 and 3 are complete: the API, worker and web app cover MVP items 1, 3 and 5 to 12.
Start Phase 4 only after the ADR below is accepted.

- [ ] ADR: GTK4 + libadwaita via PyGObject for the summonable drop target; document the
      Wayland limits (no layer-shell on GNOME, portal-based global shortcut).
- [ ] `apps/linux-client`: keyring (Secret Service) token storage, summonable window,
      drag-and-drop of files/images/URLs/text, clipboard capture.
- [ ] Immediate upload with `client_capture_id`, non-modal success/failure feedback, and the
      post-capture "Add a thought" note (PATCH `user_note`) only after the server confirmed.
- [ ] Local retry queue that replays with the same `client_capture_id` while offline.
- [ ] Packaging (Flatpak or a `.desktop` entry plus `pipx`) and README instructions.

## Later phases

See the roadmap in `docs/spec.md` section 18. Phase 5 (mobile sharing) is partly covered by the
PWA share target; break the rest into items here when Phase 4 is done.
