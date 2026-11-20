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
- [x] **Prefix and substring search** ([ADR-0025](docs/decisions/0025-prefix-and-substring-search.md),
      migration `0006_search_partial`): `urlaub` finds the tag `Urlaubstest`, `31` and `109`
      find `IMG_3109.webp`; prefixes in title, filename, tags, note and URL, substrings of
      three or more characters in filenames, whole words before partial hits, measured on
      30,000 items.
- [x] **Search follow-ups** ([ADR-0026](docs/decisions/0026-search-follow-ups.md),
      migration `0007_search_followups`): URL host/path words, accent folding in titles,
      names, tags, notes, URLs and the query, name substrings in titles and tags
      (`regal` finds `Bücherregal`, `müller` finds `mueller.pdf`), name hits in the snippet,
      "Teiltreffer" label in the feed, rank cap for common words (283 → 94 ms at 12,000
      matches), tag rename/delete measured (5,000 items of 10 KB ≈ 15 s, within the 120 s
      re-index timeout) and cheaper to rebuild.
- [ ] **Search, still open:**
      - Check the "Gefunden in"/"Teiltreffer in" line, name snippets and filename/tag
        retrieval (`urlaub`, `31`, `regal`) on real devices; only API integration tests
        and vitest cover them.
      - Run `scripts/search_bench.py`-style timings (read-only queries) and a tag rename on
        the real Bag once it is large; move tag re-indexing to a background job only if a
        tag approaches about 40,000 large items (ADR-0026).
      - Extracted text is neither accent-folded nor substring-searched; decide only with a
        measured need and cost.
- [ ] **Immediate real-device smoke test:** Android Share→Bag; macOS browser
      paste/save→Bag; refresh and retrieve from a second device. Check that
      saved text shows its excerpt in the feed (API and line selection are
      tested; the rendered card and its two-line clamp are not). Cover
      interrupted upload, auth expiry, server restart and accurate success,
      including that retrying an unanswered text/URL/file save (stable client
      retry ID, built and unit-tested) creates no duplicate.
      Also: two shares in a row, one saved and one discarded, and an app update
      with unsaved shares (pending share batches are built and unit-tested
      in Node; no component or device test yet).

**3A done when:** no silent share loss, wrongly categorized URL, false
save acknowledgement or unintended same-action retry duplicate; text and
filename retrieval work on actual clients (the API side of filename and tag
retrieval is done and tested; the client check is the open item above).

## Phase 3B — Secure previews and daily-use UI

- [x] **Thumbnail ADR** ([ADR-0023](docs/decisions/0023-safe-image-thumbnails.md)):
      Pillow in an isolated child process, `thumbnail` blob role, restricted
      inline response. Original and snapshot downloads stay attachments.
- [x] **Image preview pipeline, backend:** `thumbnail` processor, authenticated
      owner-scoped `GET /api/v1/items/{id}/thumbnail`, corruption, bomb and
      tamper tests.
- [x] **Image preview UI:** responsive image cards in feed and search and a
      larger preview in the detail view, using only the thumbnail endpoint, with
      readable fallbacks for loading, `404`, damaged, skipped, failed and
      still-processing thumbnails; `thumbnail_available` on summaries and
      details (ADR-0023). Component tests and one headless Chromium pass.
- [ ] **Image preview follow-ups:**
      - Check the cards and detail on real macOS and Android browsers (only
        headless Chromium on Linux at 1280 and 390 px was used).
      - Check the feed's self-refresh (queued → ready, new thumbnails, tab
        hidden/visible) on real macOS and Android browsers; it is covered by
        vitest with fake timers only.
      - Fallback wording cannot say why a skipped preview is missing: the
        API does not expose the `skipped` reason.
- [ ] **Thumbnail follow-ups:** measure worker memory on the production host
      against ADR-0023's 1 GiB child limit; decide on HEIC/AVIF and color
      profile handling if real captures need them.
- [x] **Fast actions** ([ADR-0027](docs/decisions/0027-fast-actions.md)): paste of
      images and files (field or page) uploads at once, pasted text fills the form,
      "Einfügen" button; "⋯" menu per feed row and detail actions to open, download,
      copy and share; German labels for kinds, states and processors.
- [ ] **Fast actions on real devices:** paste on Android/iOS (long press), Safari and
      Firefox clipboard permission prompts, sharing files from the installed PWA, copy
      of a long text in Safari (pending ClipboardItem), screen reader pass of the menu.
- [x] **Mobile-first layout, navigation and status messages**
      ([ADR-0024](docs/decisions/0024-mobile-first-web-ui.md)): menu button on
      phones, "Tags & Sammlungen", capture and search first, labelled collapsible
      filters, no horizontal overflow from 320 to 1280 px (headless Chromium without mobile emulation, mocked API),
      success/warning/error tones, compact image cards.
- [ ] **Mobile UX on real devices:** repeat the iPhone PWA and CachyOS desktop
      passes after ADR-0024: menu, file picker, date fields, status tones, image
      cards, safe areas. Also open: no keyboard test in a real Safari/Firefox,
      no screen-reader pass, and the layout check script is not part of CI.
- [ ] **Shelf follow-ups:** tags/collections remain in filters or secondary
      organization; keep the browser upload fallback where native share
      integration is absent.
- [x] **Merkliste** ([ADR-0028](docs/decisions/0028-merkliste-pinned-at.md), migration
      `0008_pinned`): `item.pinned_at`, `PUT`/`DELETE /items/{id}/pin`, `GET /pins`,
      `pinned` filter, export/import; "Merken" in menu and item view, Merkliste page
      with undo.
- [ ] **Merkliste on real devices:** pin on one device, see it on the second after a
      reload (macOS, Android, iPhone PWA); screen reader pass of the toggle and undo.

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
