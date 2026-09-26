# Architecture as built

One Python package under `apps/api/src/bag` targets Python 3.12+. FastAPI serves the
API; synchronous Psycopg connections execute parameterized SQL in short-lived
transactions. There is no ORM, Redis, frontend or client. Alembic uses SQLAlchemy
only to apply packaged SQL migrations.

Compose starts PostgreSQL 17, `bag-api` and `bag-worker`. API and worker share a
non-root image built from locked uv dependencies. `bag worker` runs the job loop in
a thread next to a health/readiness server; the worker is ready only when the schema
revision matches and the loop thread is alive. Shutdown stops the loop and waits a
bounded time for the current job; leases recover anything cut off.
Migrations never run automatically. `bag migrate` and `bag init` are explicit
server administration commands. Bootstrap uses a transaction-scoped advisory lock,
creates one user and prints one random token after commit. Repeating it changes nothing.

`bag token create/list/revoke/recover` administers credentials directly on the server.
Each operation resolves the sole user or requires an explicit `--owner` if multiple
users exist, then scopes token queries by that owner. Like bootstrap, owner discovery
is an explicit server-admin exception to normal client API access. No public recovery
endpoint exists. Creation/recovery generates 256 random bits, persists only SHA-256,
and returns the secret after commit. Listing excludes both secrets and hashes.
Recovery issues an additional token without changing existing users, captures or
credentials. Revocation sets `revoked_at` idempotently; it never deletes token rows.
Requests already authenticated may finish, while subsequent authentication rejects
revoked credentials. The CLI never prints SQL or connection errors containing secrets.

Bearer authentication hashes a token to resolve its owner. This credential lookup
and global bootstrap are ownership-discovery/admin exceptions; subsequent queries
use `owner_id`. Composite foreign keys prevent cross-owner associations. Revoked
tokens are rejected on subsequent authentication requests.

Capture locks the owner row to serialize idempotency and duplicate checks. A unique
owner/key constraint adds database enforcement. This trades per-owner throughput
for simple race semantics in the single-user MVP. Text is inserted unchanged and
acknowledged only after a synchronous PostgreSQL commit. Files first persist to the
content-addressed filesystem and then commit item/blob/relation rows together. The
same transaction inserts one `processing_run` and one `job` per registered processor,
so an acknowledged capture is always scheduled and a failed commit schedules nothing.
The capture response reports `processing_status: queued`.

URL capture validates absolute HTTP(S) syntax and preserves the original string in
PostgreSQL without normalization. It uses the same owner lock, idempotency namespace,
hash-based duplicate detection and commit boundary as text/files. Neither capture
nor validation performs DNS or HTTP requests. Private addresses can be saved; any
future fetch worker must independently validate destinations against SSRF.

## Processing

The worker claims one job per transaction: a `FOR UPDATE SKIP LOCKED` query selects
the oldest queued job whose `run_after` has passed, or a running job whose lease has
expired, increments `attempts`, records the worker ID and sets a lease
(`BAG_JOB_LEASE_SECONDS`). The matching run becomes `running` and the item
`processing` before the claim commits. The processor then runs outside any
transaction, reading originals through the verified storage handle.

Processors are pure functions returning `succeeded` or `skipped` with updates limited
to `mime_type`, `kind`, `extracted_text` and `language` plus per-processor metadata.
A finalize transaction re-locks the job, verifies that this worker still holds it,
writes the updates, marks run and job, and derives the item status from all runs in
SQL: `queued` before any start, `processing` while any run is pending or running,
`ready` when nothing failed, `failed` when nothing succeeded, otherwise `partial`.
Results arriving after a lost lease are discarded. Retryable failures requeue with
exponential backoff from `BAG_JOB_RETRY_SECONDS`, capped at one hour, until
`BAG_JOB_MAX_ATTEMPTS`; permanent failures and reclaims past the limit fail the run.
Unexpected exceptions record only the exception class. Deterministic database limits
such as the search vector size fail the run permanently instead of retrying.

Three processors exist. `mime_detect` re-verifies files from stored bytes (signature
library plus a strict UTF-8 plain-text check), sets `mime_type` and re-derives file
kinds; text items are `text/plain` and URLs are skipped. `text_extract` copies text
originals and decodes UTF-8 text files up to 1 MiB into `extracted_text`, noting
truncation in `metadata`; other content is skipped. `language` counts German and
English function words in the same plain text and sets `de` or `en` when the
evidence is clear, skipping otherwise and whenever `metadata.language.user` marks a
user choice. No PDF, image or URL extraction exists. Because `search_vector` is a
generated column, extraction and language immediately make items searchable with
the right stemmer once search endpoints exist. Every capture enqueues all processors
regardless of kind; the worker log records claim and outcome with job, item, owner
and processor IDs and the attempt number, never content or error details.

`GET /api/v1/items/{id}/processing` lists runs with status, attempts, last error and
timing for owned, live items. `GET /api/v1/items/{id}` includes `extracted_text` and
`language`.
`POST /api/v1/items/{id}/reprocess` resets the runs of every registered processor
and enqueues jobs, skipping processors with a queued or running job; a partial unique
index guarantees one active job per item and processor. `bag reprocess` does the same
for all live items of an owner, by default only for processors without a run (items
from before the queue existed or newly registered processors), optionally for failed
runs or everything.

`PATCH /api/v1/items/{id}` edits `title`, `user_note` and `language` of a live item
under a row lock; absent fields stay, null clears. Choosing a language sets
`metadata.language.user`, which the detector honors, and clearing it removes the
marker so the next run may detect again. Originals, kind and extraction stay
read-only. `DELETE /api/v1/items/{id}` sets `deleted_at` and `POST /api/v1/items/{id}/restore`
clears it; both are idempotent and owner-scoped. Trashed items disappear from
detail, download, processing and default listings but remain listable and
searchable with `trashed=true`. Nothing is removed until `bag purge` hard-deletes
items trashed longer than `BAG_TRASH_RETENTION_DAYS`, one owner-scoped transaction
per item with children removed before the item. `bag gc` then removes storage
objects no blob row references and crash-left `.upload-*` files: candidates are
scanned without locks, and each deletion batch holds every owner's capture lock
while re-checking references, so it waits for in-flight captures and can never
remove an object a capture is publishing. Both are explicit commands, not
scheduled background jobs.

## Listing and search

`GET /api/v1/items` returns owner-scoped summaries (no `content` or
`extracted_text`) newest first, paged by keyset on the UUIDv7 ID (`cursor` is the
last ID of the previous page, `limit` 1–100). `GET /api/v1/search` parses `q` with
`websearch_to_tsquery` (quotes, `OR`, `-`) in the `simple`, `german` and `english`
configurations and matches the generated vector against their OR-combination, so
stemming applies to items with a detected language while exact tokens match
everywhere. Results carry `ts_rank_cd` and a plain-text `ts_headline` snippet over
note, URL and extracted text with `«`/`»` markers; ordering is rank, then newest,
paged by a bounded offset. Both endpoints accept `kind`, `status`, `from`/`to`
(capture time, timezone required) and `trashed`. Queries that reduce to nothing
return no results rather than an error. Tag and collection filters wait for those
APIs; there is no semantic search.

`/health` needs no database; `/ready` requires the expected migration revision.
FastAPI generates OpenAPI. Application logs are JSON with fixed event names and
allowlisted IDs. Deployment disables access logs. Database errors return generic
503 responses without SQL or content; validation errors omit input values. JSON
bodies are bounded, including chunked requests. Before multipart parsing, the body
is spooled to temporary disk above 64 KiB, then replayed in 64 KiB chunks. Multipart
parsing also spools uploads to disk. This uses additional temporary disk space but
avoids buffering entire uploads in memory. A reverse proxy should enforce connection
timeouts and deployment-level concurrency limits.

Text originals live in PostgreSQL; file originals live in the persistent
`bag-storage` volume. The `BlobStorage` protocol separates capture/download from
the filesystem backend. Files are hashed while copying to a private temporary file,
fsynced, and atomically hard-linked to a hash-only path without replacing existing
objects. Parent directories are fsynced before the database transaction commits.
The store requires a POSIX filesystem with hard links and directory fsync; remote
or S3 storage is not implemented. Duplicate objects are integrity-checked before reuse.

Content signatures are detected with the pure-Python `filetype` library using a
bounded prefix. Unknown formats use `application/octet-stream` at capture and may
become `text/plain` after processing. No archive extraction or rendering happens.
Downloads check SHA-256 and size before response, use the same verified file handle,
force attachment/octet-stream and close it even on disconnect. Storage errors yield
a generic 503; neither original bytes nor filenames enter logs.

Crashes may leave unpublished `.upload-*` files or published objects without a
database reference. They are retained until `bag gc` removes them under the
capture locks; retry can reuse a published object in the meantime. Never manually
remove objects just because one request failed.

Foundational domain tables without HTTP APIs are reserved for later slices.
README documents exact startup, update and backup commands.

CI uses actual PostgreSQL 17 for persistence, parallel text/file capture, ownership,
commit failure, bootstrap and migration round trips. Storage tests exercise atomic
publication, fsync failure, corruption, size limits and racing writers. Download
disconnects are tested. Processing tests cover transactional enqueue, enrichment of
text, files and URLs, bounded retries, backoff, lease expiry and stale results,
concurrent workers executing every job exactly once, and permanent failure on the
search vector limit. AI and complex extractors are not implemented.
