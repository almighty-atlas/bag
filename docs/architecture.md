# Architecture as built

One Python package under `apps/api/src/bag` targets Python 3.12+. FastAPI serves the
API; synchronous Psycopg connections execute parameterized SQL in short-lived
transactions. There is no ORM, Redis, frontend or client. Alembic uses SQLAlchemy
only to apply packaged SQL migrations.

Compose starts PostgreSQL 17, `bag-api` and `bag-worker`. API and worker share a
non-root image built from locked uv dependencies. The worker is a foundation
service with health/readiness routes; job claiming and processors remain pending.
Migrations never run automatically. `bag migrate` and `bag init` are explicit
server administration commands. Bootstrap uses a transaction-scoped advisory lock,
creates one user and prints one random token after commit. Repeating it changes nothing.

Bearer authentication hashes a token to resolve its owner. This credential lookup
and global bootstrap are ownership-discovery/admin exceptions; subsequent queries
use `owner_id`. Composite foreign keys prevent cross-owner associations. Revoked
tokens are rejected on subsequent authentication requests.

Capture locks the owner row to serialize idempotency and duplicate checks. A unique
owner/key constraint adds database enforcement. This trades per-owner throughput
for simple race semantics in the single-user MVP. Text is inserted unchanged and
acknowledged only after a synchronous PostgreSQL commit. Files first persist to the
content-addressed filesystem and then commit item/blob/relation rows together. No
processors are scheduled yet, so text and file items are `ready` with no processing runs.

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
bounded prefix. Unknown formats use `application/octet-stream`. No archive extraction
or rendering happens. Downloads check SHA-256 and size before response, use the same
verified file handle, force attachment/octet-stream and close it even on disconnect.
Storage errors yield a generic 503; neither original bytes nor filenames enter logs.

Crashes may leave unpublished `.upload-*` files or published objects without a
database reference. They are deliberately retained: cleanup/garbage collection is
future explicit background work and must not race a capture. Retry can reuse a
published object. Never manually remove objects just because one request failed.

Foundational domain tables without HTTP APIs are reserved for later slices.
README documents exact startup, update and backup commands.

CI uses actual PostgreSQL 17 for persistence, parallel text/file capture, ownership,
commit failure, bootstrap and migration round trips. Storage tests exercise atomic
publication, fsync failure, corruption, size limits and racing writers. Download
disconnects are tested. AI and extractors are not implemented.
