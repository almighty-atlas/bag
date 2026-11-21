# ADR-0028: Merkliste as `item.pinned_at`

**Status:** accepted
**Date:** 2026-11-21

## Context

The spec (§17 and Phase 3B) asks for a "simple optional Merkliste/pinning", available on every
device, with its storage chosen in a small ADR. Concept v0.2 §16.4 adds that pinning is not a
task, not a new item and not a required collection; feed, search and originals stay as they
are. The list can be sorted by pin time, or by last use where that is known. AGENTS.md rules out
a second item store.

Two options were open:

1. **A reserved collection** ("Merkliste") using the existing `collection` and `item_collection`
   tables. Export, filters and the counting UI would work unchanged, but the name could clash
   with a user's own collection, and rename, delete and translation would need special cases. A
   collection also has no visible pin order apart from the assignment's `created_at`, and the
   Merkliste would show up among the user's own collections.
2. **A nullable `item.pinned_at`.** It is one column, set while the item is pinned, and its time
   orders the list.

## Decision

Option 2. Migration `0008_pinned` adds `item.pinned_at timestamptz NULL` and a partial index
`item_pinned_idx (owner_id, pinned_at DESC, id DESC) WHERE pinned_at IS NOT NULL`.

- `PUT /api/v1/items/{id}/pin` sets it to `coalesce(pinned_at, now())`, so pinning again keeps
  the first pin time. `DELETE /api/v1/items/{id}/pin` clears it. Both are idempotent,
  owner-scoped and return the item. Like every other edit they are 404 for trashed or unknown
  items, and with a web session they need the CSRF header. `updated_at` changes only when the
  state changes.
- `GET /api/v1/pins?limit=&offset=` lists pinned, non-trashed items newest pin first, as item
  summaries. It uses offset paging because the list stays small.
- Item responses and summaries carry `pinned_at`. Listing and search accept `pinned=true|false`
  as a filter.
- Trash hides the pin and restore brings it back with its time; purge removes it with the item.
  Export writes `pinned_at` in `items.jsonl` and import restores it. Exports without the field
  (before 0008) import as not pinned, so the export format version stays 1.
- Web: "Merken"/"Gemerkt" (`aria-pressed`) in each item's "⋯" menu and in the item view. Pinned
  rows show "★ gemerkt", and a "Merkliste" page (`#/merkliste`, a main navigation entry) offers
  undo after an unpin. The shown state changes only after the server confirmed it.

"Last use" is not tracked: the Bag records no opens, and recording them would write on every
read. The list is ordered by pin time only.

## Consequences

Pinning is a single-row update without new tables, and every client (web, Linux Shelf, scripts)
gets it through the API. The Merkliste is a view over persisted items, as ADR-0020 requires for
the Shelf. A future ordering by last use, or several lists, would need a separate table. That
is out of scope until a real need appears. The migration only adds a nullable column and an
empty partial index (no table rewrite). Its downgrade drops both, and with them every pin.
