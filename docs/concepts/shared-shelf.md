# Shared Shelf — product concept

**Status:** accepted concept (2026-10-28); implementation milestones are separate.
**Goal:** *Drop it into Bag on one device; pick it up from Bag on another.*

This document defines the cross-device user experience. The product and API contracts live in [`docs/spec.md`](../spec.md), as-built behavior in [`docs/architecture.md`](../architecture.md), concrete upcoming tasks in [`TODO.md`](../../TODO.md), and the decision in [ADR-0020](../decisions/0020-shared-shelf.md).

## 1. Product boundary

The **Shelf** is the small, immediately accessible view of recent Bag items. It is **not** a second store, transfer queue, clipboard database, or disposable holding area. Dropping something into the Shelf calls the existing Bag capture API and means **save it durably**. After the API confirms persistence, every authenticated client can retrieve the same item. The item stays until the user trashes it; it is never auto-deleted merely because another device opened or downloaded it.

This keeps the core product unchanged: capture first, organize later, retrieve by meaning. Tags, collections, notes, full-text search, original files and export remain available through the existing API and web UI.

"Cross-device" in v1 means **shared availability via the Bag server**. It does not mean directly pushing content from one device to another, synchronizing every local clipboard change, or granting access to other users. Another client sees a new item when its feed is refreshed; push notifications and immediate live updates are not required.

## 2. Platform-specific surfaces, one mental model

| Platform | Capture ("put into Bag") | Retrieve ("take from Bag") | First implementation |
|---|---|---|---|
| Linux (Niri + DMS 2.0) | DMS Bag widget/popout as a drop/paste target | Recent-items Shelf, open/download/copy actions | **DMS feasibility prototype** against existing API; optional helper only if technically necessary |
| macOS | Existing browser/PWA capture and file upload | Existing web feed, search, open/download/copy | **Reuse the web app**; a native menu-bar Shelf is not a prerequisite |
| Android | System Share menu → Bag (supported installed PWA) or upload/paste | Home-screen PWA with recent-items Shelf, search and contextual actions | **Polish/test existing PWA**, not a native Android app |
| Other mobile browsers / iOS | Browser upload/paste as baseline | Responsive Bag web view | Native Share extension only if a proven need arises |

Use the platform's own interaction pattern instead of forcing identical desktop UI on a phone. Mobile has no floating desktop-style drop target; its capture affordance is **Share → Bag**, and retrieval is a normal installed/web app.

### Linux example

Drag a PDF from the file manager onto the DMS Bag target. The client uploads through the existing `/api/v1/capture/file` endpoint with a stable `client_capture_id`, shows a success state only after server confirmation, and then shows the item in its recent-items Shelf. From the same Shelf it can open/download the stored original. Confirm that DMS/Wayland can accept this drag payload before choosing a particular plugin architecture. Dragging an item **out** into arbitrary native applications is desirable but technically unproven and not a v1 gate; use download/open/copy as a reliable fallback.

### macOS example

Open Bag in a browser or install its web app, select a newly saved Linux item in the feed, and open or download it. To send something in the opposite direction, use the existing Bag web upload/capture form. A native macOS Shelf can be evaluated after the end-to-end workflow works; do not start with a separate Swift app or duplicate sync logic.

### Android example

In a browser, gallery, or file manager choose **Share → Bag** if the installed PWA's Share Target is offered by the browser/OS. The current web implementation opens the capture view with shared text/link or staged files and still requires tapping **"In die Tasche"** to commit. Make this confirmation brief and clear; never imply a capture succeeded before the API confirmed it. Then the item appears in the same Bag archive and can be opened on Linux or macOS. For incoming items, the PWA's initial screen prioritizes recent items, search, opening links, copying text, and downloading/sharing files.

The PWA Share Target exists in code, but Android device-level end-to-end behavior and file handling must be verified; no promise is made for iOS system Share integration in v1.

## 3. Design rules

1. **One server, one truth.** The existing Bag API and persisted `Item` are authoritative. No parallel Shelf database, new sync protocol or extra backend service.
2. **Capture is durable.** "Add to Bag" is a save, not an expiring transfer. Removal follows existing trash/restore semantics. Never silently delete an item after opening it.
3. **No required metadata.** Drag/drop or Share Target should not force a form, device selection, tags or categories; optional notes remain possible.
4. **Visible success and errors.** Show stored/sending/failure states honestly. Preserve capture idempotency and ensure retries cannot silently duplicate an upload.
5. **Do not broadcast private content.** Only authenticated clients request Bag items; no automatic clipboard surveillance or peer discovery. Follow existing token/session, HTTPS and upload safeguards.
6. **Small surfaces.** The Shelf shows recent items and capture, not every advanced archive setting. Full organization remains in the web UI.
7. **Platform capabilities must be verified.** If DMS/Wayland drag-in or drag-out limitations appear, keep the API design and choose a narrow native helper or fallback instead of a large rewrite.

The existing default per-file upload limit is 50 MiB. That limit and network connectivity apply to server-backed cross-device access; Bag is not a bulk LAN transfer application.

## 4. Explicit non-goals for the first release

- No LocalSend dependency, integration, CLI wrapper or replacement.
- No AirDrop clone, custom P2P transport, peer/device pairing or device selector.
- No automatic cross-device clipboard, mouse/cursor or cross-screen drag-and-drop.
- No temporary Shelf TTL, separate server transfer queue, or automatic deletion after pickup.
- No forced native apps for macOS/Android, no always-on-top overlay requirement, no WebSocket/push service.
- No mandatory AI, embeddings or new infrastructure to make a capture or retrieval work.

These are not unimplemented promises of v1. Reconsider only when measured use shows a concrete limitation.

## 5. Incremental delivery and acceptance

**Slice A — Linux integration feasibility.** On the target Niri/DMS environment, prove a minimal DMS surface can take a real file/text/URL into the existing Bag API and display recent captures. Validate Wayland drag/drop, shortcuts/popout lifecycle, tokens, and failure states. Record platform limitations in an ADR before committing to DMS-only packaging. Keep GTK4 as an independent fallback if the integration fails. No new schema.

**Slice B — Mac↔Linux workflow.** Save on Linux; find and open/download the same item in the existing macOS web UI. Reverse the flow with a macOS upload. A feed refresh is acceptable; do not describe it as live push.

**Slice C — Android polish.** Install and test the supported PWA, test Android Share Target for text/URL/image/file on an actual phone, ensure an explicit successful capture is visible, optimize a compact recent-items/search experience, and validate open/copy/download/share-out. Retain browser upload fallback if native sharing is not offered.

**Release checks:** originals stay intact; failed/offline captures are never reported as saved; permissions and auth are enforced; a repeated request with the same `client_capture_id` does not duplicate an item; no device is required to be online except the Bag server when accessing remote content.

## 6. Deferred questions, not immediate features

Native macOS integration, drag-out from a DMS popout, optional offline capture retries, real-time freshness and native mobile extensions can be reconsidered **after** the three core workflows are tested. Favor the smallest improvement that removes an observed point of friction. Do not add platform-specific infrastructure speculatively.
