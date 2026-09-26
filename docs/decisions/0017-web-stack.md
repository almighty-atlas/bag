# ADR-0017: Preact and Vite web app served by nginx with an API proxy

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 4.1 leaves the frontend choice between Preact and Svelte and asks
for TypeScript, Vite, a small footprint and PWA behavior. The web UI must share
the origin with the API so the session cookie and the CSRF header rule work
without CORS, and deployment must stay a plain Compose stack.

## Decision

`apps/web` is a Preact 10 application built with Vite and TypeScript in strict
mode (plus `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes`), without
a router or state library: hash routes (`#/`, `#/?q=`, `#/items/<id>`) keep the
static build free of server rewrites. A typed client module wraps `fetch`, adds
`X-Bag-Csrf: 1` and same-origin credentials. The `bag-web` Compose service builds
the app in a Node 22 stage and serves it from nginx, which proxies `/api/` to
`bag-api` with request buffering off and a 64 MiB body limit, and adds
`nosniff`, `DENY` framing and a no-referrer policy. Vite's dev server proxies
`/api` to a local API. CI type-checks, builds and images the app on Node 22.

## Consequences

The UI is one origin with the API, so no CORS and no token in browser storage.
Preact keeps the bundle small and the component model familiar. Hash routing
means links contain a `#`, which is acceptable for a personal tool; switching to
history routing would only require nginx rewrites. Text from the API is rendered
as text nodes, never as HTML, so snippets and originals cannot inject markup.
Frontend unit tests, the service worker and share-target handling are still open.
