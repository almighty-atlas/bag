# ADR-0020: One server-backed Shared Shelf across platforms

**Status:** accepted
**Date:** 2026-10-28

## Context

Bag already implements persistent text/URL/file capture, search, a web UI and an Android-oriented PWA Share Target. Linux integration is not implemented. An earlier idea combined a temporary shelf, device-to-device transfer, LocalSend integration, universal clipboard, and native apps. That would create overlapping storage and synchronization responsibilities and distract from the capture-first product.

## Decision

Use **Shared Shelf** as the name for a small **view of recent, durably saved Bag items**, backed exclusively by the existing Bag API. A new capture is a permanent archive item, not an expiring transfer; users can open/download it from any authenticated client.

Implement Linux first as a DMS 2.0 feasibility prototype on Niri, with GTK4/native fallback if required by Wayland limitations; use the existing web/PWA experience for macOS and Android. The Android capture interaction is `Share → Bag`, with clear explicit confirmation as currently implemented. LocalSend and all custom peer-to-peer / device-pairing / universal clipboard functionality are **out of scope**. No extra store or synchronization service is introduced.

The full UX and acceptance criteria are in [the Shared Shelf concept](../concepts/shared-shelf.md).

## Consequences

- Reuse current storage, API, processing, auth and web app without schema changes.
- A Shelf item persists until normal trash/purge; no ambiguous ephemeral retention policy.
- Cross-device availability depends on the Bag server and feed refresh; it is not live push or direct LAN transfer.
- Native drag-out, automatic discovery and Android system integration are not assumed to work until tested on the actual platform.
- Review the earlier planned GTK-only Linux-client approach in `docs/spec.md`/`TODO.md`; validate DMS rather than committing to a framework before a small proof of concept.
