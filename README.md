# Bag of Holding

> A self-hosted, capture-first personal memory system. Drop anything in. Organize later. Retrieve by meaning.

**Status:** foundation and first text-capture slice implemented. Bearer authentication,
durable text storage, idempotent retries and duplicate relations work. File/URL capture,
processing, search and clients remain pending. The worker exposes health/readiness;
it does not execute jobs yet.

## What it is

Bag of Holding lets you put files, images, documents, URLs, text and code into one place without choosing folders, tags or types first. Originals are always preserved. Enrichment such as text extraction and search indexing runs afterwards, and AI features are optional and replaceable. The server is the memory; the Linux drop target, web UI and mobile sharing are thin clients of one API.

## Documents

| File | Purpose |
|---|---|
| [`docs/spec.md`](docs/spec.md) | Product vision, architecture, domain model, API, security, roadmap |
| [`AGENTS.md`](AGENTS.md) | Working rules, priorities, conventions and definition of done for coding agents |
| [`TODO.md`](TODO.md) | Actionable outstanding work, starting with Phase 0 |
| [`docs/architecture.md`](docs/architecture.md) | The architecture as built |
| [`docs/data-model.md`](docs/data-model.md) | Schema and migration rules |
| [`docs/capture-pipeline.md`](docs/capture-pipeline.md) | Capture, durability and errors |
| `docs/decisions/` | Architecture decision records |

## Quickstart

Requirements: Docker with Compose. Run from the repository root:

```sh
cp .env.example .env
docker compose --env-file .env -f deploy/compose/compose.yaml up -d postgres
docker compose --env-file .env -f deploy/compose/compose.yaml build
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag migrate
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag init
docker compose --env-file .env -f deploy/compose/compose.yaml up -d bag-api bag-worker
curl --fail http://localhost:8000/ready
```

Save the printed token in your password manager. It is shown once; only its SHA-256
hash is stored. Re-running initialization leaves users and tokens unchanged.
Migrations always run explicitly, never on container startup.

Capture and read text (requires curl and Python 3). Enter the token after `read`;
input is hidden:

```sh
read -r -s BAG_TOKEN
export BAG_TOKEN
CAPTURE_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
CAPTURE_RESPONSE=$(curl --fail-with-body -sS http://localhost:8000/api/v1/capture/text \
  -H "Authorization: Bearer $BAG_TOKEN" \
  -H "Idempotency-Key: $CAPTURE_ID" \
  -H 'Content-Type: application/json' \
  --data '{"content":"Grüße aus dem Bag 💼","user_note":"Meine erste Notiz"}')
printf '%s\n' "$CAPTURE_RESPONSE"
ITEM_ID=$(printf '%s' "$CAPTURE_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl --fail-with-body -sS "http://localhost:8000/api/v1/items/$ITEM_ID" \
  -H "Authorization: Bearer $BAG_TOKEN"
unset BAG_TOKEN
```

Retries must reuse the same key. Alternatively send `client_capture_id` in JSON;
if both keys are supplied they must match. Reusing a key returns the original item
even if the payload changes. Equal text with a new key creates another item and
reports `duplicate_of`. Text is returned as JSON and must be escaped when displayed.

API docs: <http://localhost:8000/docs>; OpenAPI: <http://localhost:8000/openapi.json>.
`/health` checks liveness; `/ready` checks PostgreSQL and the expected schema revision.
Both API and worker expose these endpoints.

## Development and checks

Install Python 3.12+ and uv:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m 'not integration'
uv build
```

For the full suite, start PostgreSQL as above and create a separate test database once:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml exec postgres \
  sh -c 'createdb -U "$POSTGRES_USER" bag_test'
export BAG_TEST_DATABASE_URL=postgresql://bag:bag-local-dev@127.0.0.1:5432/bag_test
uv run pytest
```

Adjust the URL when credentials differ from `.env.example`. Tests clear Bag tables
and require the database name to end in `_test`. Never use the application database.
Without the variable integration tests skip. CI always runs the full PostgreSQL 17
suite, lint, strict type checking, Python packaging and the container build.

For host-based development, with PostgreSQL running and `.env` configured:

```sh
uv run bag migrate
uv run bag init
uv run uvicorn bag.api:create_app --factory --reload --no-access-log
```

## Deployment and backup

Configuration is documented in [`.env.example`](.env.example). Replace development
credentials before deployment; use URL-safe database passwords (or percent-encode
credentials in URLs). API and database ports bind to loopback. Remote access needs
a TLS reverse proxy with request size/time limits. No telemetry or external content
services are used.

Data lives in the `bag_postgres-data` volume. All current originals are PostgreSQL
text; a blob-storage volume will be added with file capture. Back up using a dump
and keep `.env` separately and securely:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > bag.dump
```

Restore into an empty replacement database with `pg_restore` and verify it before
switching services. `docker compose down` preserves data; `down -v` destroys volumes.
For updates: back up, build, explicitly run `bag migrate`, then recreate API/worker
with the quickstart commands. Retain the token across restarts; token recovery and
management are not implemented yet.

## Planned stack

Python 3.12+ with FastAPI, PostgreSQL 17 for data, full-text search and the job queue, content-addressed object storage (filesystem or S3-compatible), a small TypeScript PWA, and a GTK4 Linux client. Details and reasons are in the spec.

## License

MIT, see [`LICENSE`](LICENSE).
