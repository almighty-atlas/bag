# TODO — prioritized delivery plan

Only actionable outstanding work. Product direction:
[concept v0.2](docs/concepts/product-concept-v0.2.md).
Implementation contract: [spec §17–18](docs/spec.md#17-mvp-scope-and-release-gates).
Actual behavior: [architecture](docs/architecture.md).
The [Shared Shelf concept](docs/concepts/shared-shelf.md) and
[ADR-0020](docs/decisions/0020-shared-shelf.md) still apply:
Shelf is a view over persisted Bag items, not a second transport/store.

## Current baseline and rules

- Phases 0–2 (durable API capture, PostgreSQL job processing, initial search,
  soft delete, export/import) and a web/PWA shell are implemented.
- Independent source review on 2026-10-31 identified the gaps below.
  Browser-specific outcomes must be reproduced on real devices before claiming
  a fix. No application fix is performed by this documentation update.
- Work in order: Phase 3A (correctness) → 3B (safe cards/UX) →
  3C (neutral relations) → 3D (local metadata). Linux DMS feasibility can
  begin after the Phase 3A API contract is reliable; Phase 5 closes the
  complete cross-device experience. Android/macOS tests begin in **3A**.

## Phase 3A — Capture reliability and usable retrieval (first)

- [ ] **Android POST Share Target:** reproduce shared URL + title + text,
      plain text, image and file on a real Android browser/PWA. URL
      classification with extra title/text is fixed and covered by a vitest
      test that runs the real `sw.js` on a multipart POST in Node; still open:
      a real-device test and a test in an actual browser service worker.
- [ ] **Text excerpt in recent feed:** show meaningful bounded text
      previews when no title or note exists; preserve API auth, paging and
      sane truncation for large Unicode input.
- [ ] **Search upgrade:** include original filename and assigned tag names,
      title/name-aware ranking and a structured field-level match reason.
      Add migrations and DE/EN, owner-scoped and trashed-item regression
      tests. Keep existing tag/collection filters working.
- [ ] **Immediate real-device smoke test:** Android Share→Bag; macOS browser
      paste/save→Bag; refresh and retrieve from a second device. Cover
      interrupted upload, auth expiry, server restart and accurate success,
      including that retrying an unanswered text/URL/file save (stable client
      retry ID, built and unit-tested) creates no duplicate.
      Also: two shares in a row, one saved and one discarded, and an app update
      with unsaved shares (pending share batches are built and unit-tested
      in Node; no component or device test yet).

**3A done when:** no silent share loss, wrongly categorized URL, false
save acknowledgement or unintended same-action retry duplicate; text and
filename retrieval work on actual clients.

## Phase 3B — Secure previews and daily-use UI

- [ ] **Thumbnail ADR:** decide decoder and re-encoder, resource limits
      (bytes, dimensions, decompression bombs), a derived thumbnail blob
      role, restricted inline response and content-security-policy handling.
      Original and snapshot downloads must remain sandboxed/attachment.
- [ ] **Image preview pipeline:** worker, safe authenticated endpoint,
      responsive cards and details; corruption/oversized image tests and
      visible fallback if thumbnail generation fails.
- [ ] **Fast actions:** clipboard paste for text and supported images;
      context actions to open/copy/share/download; user-friendly
      processing status instead of raw processor names.
- [ ] **Compact phone Shelf and navigation:** recent items and search
      first; tags/collections remain in filters or secondary organization.
      Keep browser upload fallback where native share integration is absent.
- [ ] **Merkliste:** ADR for pinned_at versus a reserved collection,
      then cross-device pin/unpin and accessible list. No tasks/reminders
      or second item store.

**3B done when:** users can recognize, open and reuse saved text, images,
links and files on both macOS and Android; unsafe originals are never
served inline for cosmetic previews.

## Phase 3C — Neutral, accessible item links

- [ ] **Relation ADR + migration:** forbid self-links, ensure one neutral
      related_to relation per unordered pair, preserve owner constraints,
      define soft-delete/undo and trash/restore effects. Keep system
      duplicate_of separate. Do not assume migration semantics.
- [ ] **Relation API:** list/create/remove/undo with owner-scoped auth,
      concurrent duplicate safety and clear invalid/drop error responses.
- [ ] **Link dialog:** item search + multi-select on desktop/mobile,
      two-way item detail display, open/unlink and visible undo.
      Keyboard and touch paths must work without drag & drop.
- [ ] **Regression tests:** A↔B once in either direction; A↔A denied;
      no cross-owner or trashed-item links; restore preserves proper
      relationships; unlink never deletes either original.

**3C done when:** two existing items can be linked and unlinked safely,
independently of AI, semantic types, galleries or dragging gestures.

## Phase 3D — Useful local link cards and provenance

- [ ] Parse permitted Open Graph, canonical and JSON-LD hints without
      disabling existing SSRF, size, redirect or timeout restrictions.
      Missing/malformed metadata never prevents a saved URL from existing.
- [ ] Track per-field source/time/confidence and explicit user overrides,
      including deliberately cleared values. Reprocessing never silently
      restores a rejected or manually blanked title.
- [ ] Present useful link metadata with safe fallbacks and the structured
      search match reason. No external book provider, price scraping or
      derived entity migration is required for this phase.

**3D done when:** common saved links have helpful previews without
mandatory cloud providers or unwanted overwrite of user input.

## Phase 4 — Linux Shared Shelf: validate, then build minimal slice

- [ ] On Niri+DMS 2.0 validate popout, native drop/paste payloads,
      recent API items and supported OS open/copy/download. If DMS is
      unsuitable, record the result and use GTK4/libadwaita fallback.
- [ ] First functional API-only Shelf: stable retry ID, secure
      Secret Service credentials, uploading/saved/failed feedback,
      same durable item view and no new sync/store.
- [ ] Verify real Linux→macOS retrieval before optional Wayland
      drag-out. Only add a local retry queue if tests prove the need.

## Phase 5 — Mobile Shelf finish and three-device acceptance

- [ ] Retest Android system share and compact PWA after 3A/3B fixes.
      Keep iOS/unsupported phone browser-upload fallback.
- [ ] End-to-end Linux ↔ macOS ↔ Android: URL, text, file, search,
      original retrieval; token error, size/timeout/offline feedback,
      repeated shares and successful refresh on second device.

## Later — separate approvals, not current feature work

- [ ] **Entity/origin ADR:** compare derived/external Items with an
      origin field + identifiers against a distinct entity table.
      Define ownership, export, orphan handling and feed behavior;
      preserve each capture. No expensive automatic feed grouping yet.
- [ ] **Book/video intelligence:** optional ISBN/Open Library opt-in;
      safe click-to-load video player with CSP/provenance ADR.
      No mandatory Amazon scraping or Google Books for the core.
- [ ] **Drag/drop and galleries:** only after the accessible relation
      dialog, with explicit gallery action and evaluated ordered
      collections. Do not duplicate image originals.
- [ ] **DAV-0:** parse user-provided .ics/.vcf as ordinary file Items.
      Full read-only CalDAV/CardDAV sync needs a separate approved ADR
      for secrets, identity, discovery, recurrence, access and retention;
      bidirectional writes come later.
- [ ] **Text edits ADR:** determine if text remains immutable with
      notes or becomes explicitly versioned.
- [ ] **AI/semantic retrieval (historical Phase 6):** only after
      Phases 0–5 have stabilized and with separate approval.

## Excluded from the capture-first MVP

No LocalSend/P2P, universal clipboard sync, native macOS/Android
applications as prerequisites, temporary Shelf datastore, automatic
expiry, rich-text editor, graph UI, automatic playlist/price engine,
complex multi-capture grouping, full calendar/contact editor,
automatic merge of original Items, or obligatory AI/cloud services.
