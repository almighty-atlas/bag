# ADR-0015: Pinned, policy-checked URL fetching enabled by default

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 7 lists `url_fetch` as a standard processor and section 10.2
requires SSRF protections: private and link-local ranges blocked, DNS resolved
before connecting, bounded redirects, size and time. Section 11 says no content
leaves the server unless a fetch provider is explicitly configured. A saved URL
is only useful in search once its page text exists.

## Decision

The worker fetches `url` items with the standard library: the host is resolved
first, every returned address must be globally routable unicast (IPv4-mapped
IPv6 is unwrapped, multicast and non-global ranges are refused), and the socket
is opened to that exact address while TLS verifies the original hostname. Each
redirect target is resolved and checked again, at most five times. Responses are
read in chunks up to `BAG_FETCH_MAX_BYTES`, bounded by a per-connection timeout
and an overall deadline. Refused destinations, credentials in URLs and disabled
fetching are recorded as `skipped` with a reason; HTTP 4xx, oversize bodies and
redirect loops fail permanently; 5xx, timeouts and connection errors retry.

The page is stored as a `snapshot` blob (replaced on rerun), `mime_type` comes
from the sanitized Content-Type, HTML and plain text yield `extracted_text`, and
an HTML title fills `title` only while it is NULL. Fetching is on by default and
documented in `.env.example` as the one place where the server contacts the URL's
own host; `BAG_FETCH_URLS=false` keeps the worker offline.

## Consequences

Saved links become searchable without a third-party service; only the destination
itself learns that the URL was fetched. Rebinding DNS cannot redirect a connection
after validation. Hosts behind NAT64 or with only private answers are never
fetched, and a public host that later serves private redirects stops there. No
JavaScript rendering, robots handling or content sniffing beyond the declared
type exists; snapshots are not downloadable yet.
