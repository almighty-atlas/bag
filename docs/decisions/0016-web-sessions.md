# ADR-0016: Server-side sessions with a custom-header CSRF rule

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 10.1 requires a session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`)
with CSRF protection for the web UI, password login for the seeded user with
argon2, and token management through the web UI. Bearer clients must keep
working unchanged.

## Decision

Migration `0004_sessions` adds `username` and `password_hash` to `user` and a
`session` table storing only SHA-256 hashes of random session tokens with an
expiry. `bag password set` hashes with argon2id and signs out existing sessions.
`POST /api/v1/session` verifies the password (against a dummy hash for unknown
names, so timing does not reveal accounts) and sets the `bag_session` cookie
scoped to `/api`. Requests authenticate by bearer token first, then by cookie.
Cookie-authenticated requests with an unsafe method, including login itself,
must send `X-Bag-Csrf: 1`; browsers cannot attach custom headers cross-site
without CORS, and none is configured. Token listing, creation and revocation
require a session, never a bearer token, so a leaked client token cannot mint
others. `BAG_COOKIE_SECURE=false` exists only for plain-http development.

## Consequences

No CSRF token storage or rotation is needed; the web client adds one static
header. Sessions can be revoked server-side and expire after `BAG_SESSION_DAYS`.
Login has no rate limit yet beyond argon2's cost; adding one is listed in
`TODO.md`. The cookie's `/api` path keeps it away from static assets. Usernames
are unique across users and case-sensitive.
