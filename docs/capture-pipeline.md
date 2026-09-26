# Capture pipeline as built

Only JSON text capture is supported in this slice.

1. Limit the entire request body, including chunked input (default 1 MiB).
2. Resolve ownership using a hashed bearer token. Missing, invalid or revoked tokens receive 401.
3. Validate `content`, optional `kind: text`, `source` (default `api`), `user_note`,
   timezone-aware `captured_at` and UUID `client_capture_id`. Reject unknown fields,
   empty content, NUL and invalid Unicode. Preserve whitespace.
4. Reconcile the optional UUID `Idempotency-Key` header with the JSON key;
   conflicting values receive 422.
5. Lock the owner row inside the transaction and look up the capture key. Existing
   captures return their original ID and duplicate relation without changing data.
6. For new captures, check live owner-scoped content hashes; insert the original,
   SHA-256 hash and any system duplicate relation in one transaction.
7. Commit with `synchronous_commit=on` before returning or logging success. The
   response contains ID, `status: stored`, `processing_status: ready` and optional
   `duplicate_of`. Both initial capture and replay return HTTP 201.
8. `GET /api/v1/items/{id}` reads committed original text. Unknown, foreign-owned
   and soft-deleted rows return 404.

Zero scheduled processors means text is ready immediately. No fetching, extraction,
language detection or AI runs on capture. `text/plain` describes JSON text; it is
not a trusted client file-MIME claim. Strings remain untrusted and future clients
must escape them before rendering.

Database/commit failure returns 503 without acknowledgement. If the commit succeeds
but the HTTP response is lost, retrying the same key returns the saved item. Every
retry-capable client should generate its key before the first attempt. Without a
key the client cannot distinguish a lost response from a failed capture.

Integration tests inject a deferred commit failure and verify rollback, safe retry
and unchanged originals, along with concurrent replays and distinct same-content
captures. Oversize requests receive 413; validation errors receive 422 without
echoing private input. Transport timeouts belong at the reverse proxy. File/URL
capture, storage crash recovery, processing and search remain in `TODO.md`.
