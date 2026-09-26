# Capture pipeline as built

JSON text capture and multipart file capture are supported.

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
echoing private input. Transport timeouts belong at the reverse proxy. URL capture,
processing, garbage collection and search remain in `TODO.md`.

## File capture

`POST /api/v1/capture/file` accepts one `file` and an optional `metadata` JSON-string
form field. Metadata accepts `source`, `user_note`, timezone-aware `captured_at`,
`client_capture_id` and optional `kind: file`. Stored kind is derived from content.
The `Idempotency-Key` header works as for text; keys are unique across both routes.

The aggregate body is limited to `BAG_MAX_UPLOAD_BYTES + BAG_MAX_REQUEST_BYTES`;
the latter provides multipart/metadata overhead. The actual file is separately
limited to `BAG_MAX_UPLOAD_BYTES` (default 50 MiB), metadata to the JSON limit and
the multipart parser's 1 MiB part limit, whichever is lower. Empty files are valid.
Bodies spool to disk in bounded chunks. Authentication, metadata and file-size
validation complete before any permanent object write.

Under the owner capture lock, replay is resolved first. A new capture reads a
bounded signature prefix, ignores client MIME/extension, and copies the file in
64 KiB chunks into a unique storage temporary file while computing SHA-256. The
temporary file is flushed/fsynced, then hard-linked atomically to `ab/cd/<hash>`.
Existing objects must match hash and size; no object is overwritten. Directories
are fsynced before inserting item, blob and duplicate relation and committing.
Temporary upload paths never depend on the original filename.

Storage/commit failures cannot acknowledge a new capture. A failure after publication
may leave an unreferenced durable object; retry reuses it. Removing it immediately
would race other owners' captures, so orphan cleanup is deferred to explicit future
jobs. Hard crashes can also leave `.upload-*` files. Both are part of future GC work.

`GET /api/v1/items/{id}/content` scopes item and blob by owner, excludes trash and
checks the complete object's size and SHA-256 before sending any bytes. It returns
an attachment as `application/octet-stream`, with `nosniff`, CSP sandbox and private
no-store caching. The download filename removes path/control characters and is
percent-encoded; the item retains the original display name. The verified handle
is closed on success or disconnection. Missing/corrupt physical originals return
503; missing, foreign or trashed items return 404.
