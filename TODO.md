# TODO

Actionable outstanding work only. The [Shared Shelf concept](docs/concepts/shared-shelf.md)
and [ADR-0020](docs/decisions/0020-shared-shelf.md) define the next product direction.
Do not conflate the persisted Shelf view with a separate temporary transfer service.

## Current baseline

Phases 0–2 (durable capture, processing, search and maintenance) are implemented.
Phase 3's Preact web UI and Android-oriented PWA Share Target exist, but the
browser-driven share/preview end-to-end flow has not been validated on devices.
Phase 4 (Linux desktop integration) has not started.

## Phase 3 — Finish validating the existing web/PWA

- [ ] Add browser-driven end-to-end tests for Share Target and sandboxed preview flows.
- [ ] Test that an authenticated macOS browser can upload and retrieve content
      captured from another client; document any browser-specific limitations.

## Phase 4 — Linux Shared Shelf, smallest vertical slice first

- [ ] **Feasibility/ADR:** On Niri + DMS 2.0 validate a small Bag dock/bar surface,
      popout, receiving real file/text/URL drag/paste payloads and refresh of recent
      items through the existing API. Test Wayland limitations and whether drag-out
      is possible. If DMS is unsuitable, retain GTK4/libadwaita as fallback.
- [ ] **First working prototype:** Capture files/text/URLs through existing
      `/api/v1/capture/*` with stable `client_capture_id`; list recent items;
      open/download/copy them using supported OS actions. Do not create a new
      server table, temporary Shelf or sync channel.
- [ ] **Safety/UX:** Client credential storage in Secret Service (or approved
      platform keyring), explicit confirmed/failed/uploading states, duplicate-safe
      retry behavior, and tests for failed/offline captures. Only add a local retry
      queue if needed to meet the reliability requirement.
- [ ] **Delivery:** Document DMS setup and a fallback launch path, package the
      integration if the prototype validates, and update as-built docs.

## Phase 5 — Smartphone Shared Shelf UX

- [ ] Validate install/launch and Android system Share → Bag for URL, text, image
      and file on a real device; preserve the explicit "In die Tasche" save
      confirmation, or document any later change separately.
- [ ] Make the PWA's mobile start view a compact recent-items Shelf with search
      and open/copy/download/share-out actions; keep all full organization in
      existing web views and a browser-upload fallback.
- [ ] Verify cross-device Linux ↔ macOS ↔ Android workflows, visibility after a
      refresh, authentication failures, file upload limits and accurate success
      feedback.

## Not planned for these phases

No LocalSend integration, device-to-device transfer, new sync server, automatic
clipboard sync, native macOS/Android app, cross-screen drag/drop or automatic
expiration of Shelf items. Reassess only after the core end-to-end experience
is proven useful.
