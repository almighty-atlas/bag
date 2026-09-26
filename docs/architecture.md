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
acknowledged only after a synchronous PostgreSQL commit. No processors are scheduled
yet, so text items are `ready` with no processing runs.

`/health` needs no database; `/ready` requires the expected migration revision.
FastAPI generates OpenAPI. Application logs are JSON with fixed event names and
allowlisted IDs. Deployment disables access logs. Database errors return generic
503 responses without SQL or content; validation errors omit input values. JSON
bodies are bounded, including chunked requests. A reverse proxy should enforce
connection timeouts.

All current originals live in PostgreSQL. The blob schema enforces the future
content-addressed path shape; no storage backend or blob volume exists yet.
Foundational domain tables without HTTP APIs are reserved for later slices.
README documents exact startup, update and backup commands.

CI uses actual PostgreSQL 17 for persistence, parallel capture, ownership, commit
failure, bootstrap and migration round trips. Unit tests cover IDs, validation,
body limits, health/readiness and logging privacy. AI and extractors are not implemented.
