# ADR-0007: Server-side token administration and recovery

**Status:** accepted
**Date:** 2026-09-26

## Context

The initial token is shown once, and losing it must not require a new user or loss
of captures. The web UI and its session authentication do not exist yet.

## Decision

Add `bag token create/list/revoke/recover` as privileged server administration.
Discover the sole MVP owner or require `--owner UUID` for multiple users. Scope
every subsequent query by that owner. Recovery issues a new credential, preserving
users, data and existing credentials. Print new secrets only after commit and keep
secrets/hashes out of listing output. No public recovery endpoint is exposed.

## Consequences

Server access can recover from revocation/loss of every bearer token. Credentials
are 256-bit random secrets hashed with SHA-256; plaintext is not recoverable from
the database. Create/recover are intentionally non-idempotent, whereas revoke is
idempotent. Lost creation output requires explicit revocation/replacement. Web
management remains a later authenticated client of the API; CLI administration is
not an invitation for capture clients to access PostgreSQL directly.
