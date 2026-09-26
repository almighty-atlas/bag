# Bag of Holding

> A self-hosted, capture-first personal memory system. Drop anything in. Organize later. Retrieve by meaning.

**Status:** text, URL and file capture implemented. Bearer authentication, durable original
storage, authenticated file downloads, idempotent retries and duplicate relations work.
The worker executes a PostgreSQL job queue with leases and bounded retries; processors
verify MIME types, extract plain text and detect German/English. Items can be listed,
filtered and searched with ranked snippets. Page fetching, deletion, export and
clients remain pending.

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

The capture response reports `processing_status: queued`. Within about a second the
worker runs the processors; `GET /api/v1/items/{id}` then shows `processing_status:
ready` and `extracted_text`. Per-processor state is available at
`GET /api/v1/items/{id}/processing`, see below.

API docs: <http://localhost:8000/docs>; OpenAPI: <http://localhost:8000/openapi.json>.
`/health` checks liveness; `/ready` checks PostgreSQL and the expected schema revision.
Both API and worker expose these endpoints.

## Processing and enrichment

Update existing installations. This version adds a migration for the job queue, so
run `bag migrate` before starting the new containers; until then `/ready` reports
`not_ready` on purpose. Tokens and data are preserved:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml build
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag migrate
docker compose --env-file .env -f deploy/compose/compose.yaml up -d bag-api bag-worker
```

Every capture schedules three processors in the same transaction as the item:
`mime_detect` verifies the type from stored bytes, `text_extract` fills
`extracted_text` for text captures and UTF-8 text files (up to 1 MiB), and
`language` sets `de` or `en` when the text clearly reads as German or English, which
selects the stemmer for search. PDFs, images, other binaries and URLs are stored
unchanged and skipped by extraction and detection. The worker
claims one job at a time, retries failures with exponential backoff up to
`BAG_JOB_MAX_ATTEMPTS`, and takes over jobs whose lease (`BAG_JOB_LEASE_SECONDS`)
expired after a crash. Originals are never modified by processing.

```sh
curl --fail-with-body -sS "http://localhost:8000/api/v1/items/$ITEM_ID/processing" \
  -H "Authorization: Bearer $BAG_TOKEN"
```

Each entry shows `processor`, `status` (`pending`, `running`, `succeeded`, `failed`
or `skipped`), `attempts`, a short `last_error` and timestamps. The item's
`processing_status` summarizes them: `queued`, `processing`, `ready`, `partial`
(some processor failed) or `failed`. Worker logs record job, item and processor IDs
but never content. If the worker is stopped, captures still succeed and remain
queued until it returns; `docker compose logs bag-worker` shows progress.

To run the processors again, for example after a failure or an update that adds
processors, call the reprocess endpoint. It returns the reset runs with HTTP 202 and
never duplicates a job that is still queued or running:

```sh
curl --fail-with-body -sS -X POST "http://localhost:8000/api/v1/items/$ITEM_ID/reprocess" \
  -H "Authorization: Bearer $BAG_TOKEN"
```

Items captured before this version are `ready` without runs. Schedule them, and any
processor added later, on the server; the default scope only touches processors that
never ran, `--scope failed` retries failed runs and `--scope all` reruns everything:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag reprocess
```

The command prints how many items and jobs were scheduled and requires `--owner UUID`
when several users exist. Reprocessing never changes originals or notes.

## Listing and search

List recent items newest first and page with the returned cursor; search ranks
matches in titles, notes, URLs and extracted text and returns a snippet with the
matched words between `«` and `»`. German and English words are stemmed for items
whose language was detected, so `Tasche` finds `Taschen` and `book` finds `books`.
Quotes, `OR` and a leading `-` work as in web search engines:

```sh
curl --fail-with-body -sS "http://localhost:8000/api/v1/items?limit=20" \
  -H "Authorization: Bearer $BAG_TOKEN"
curl --fail-with-body -sS -G "http://localhost:8000/api/v1/search" \
  --data-urlencode 'q=Tasche OR "local RAG"' --data-urlencode 'kind=text' \
  -H "Authorization: Bearer $BAG_TOKEN"
```

Both endpoints accept `kind`, `status`, `from` and `to` (timezone-aware capture
times) and `trashed=true`. Listings omit `content` and `extracted_text`; fetch the
item by ID for those. Snippets are plain text that clients must escape before
rendering as HTML.

## Trash and restore

Deleting moves an item to the trash: it vanishes from listings, search, detail and
download, but nothing is removed from the database or storage. Restore brings it
back unchanged. Both calls are safe to repeat:

```sh
curl --fail-with-body -sS -X DELETE "http://localhost:8000/api/v1/items/$ITEM_ID" \
  -H "Authorization: Bearer $BAG_TOKEN"
curl --fail-with-body -sS "http://localhost:8000/api/v1/items?trashed=true" \
  -H "Authorization: Bearer $BAG_TOKEN"
curl --fail-with-body -sS -X POST "http://localhost:8000/api/v1/items/$ITEM_ID/restore" \
  -H "Authorization: Bearer $BAG_TOKEN"
```

Permanent removal with a retention period and storage cleanup is not implemented
yet; trashed items keep their space until then.

## Try URL capture

In <http://localhost:8000/docs>, authorize and try `POST /api/v1/capture/url`:

```json
{
  "url": "https://example.org/article?topic=memory#notes",
  "user_note": "Read this later"
}
```

Use the returned ID with `GET /api/v1/items/{item_id}`. Both `content` and
`source_url` contain the exact URL as entered. Optional source, capture timestamp
and retry keys work as for text/file capture. Only absolute HTTP(S) URLs up to
8192 characters are accepted; whitespace, controls and backslashes are rejected.
URLs are not normalized or fetched, including private addresses. No title, page
snapshot or page MIME is available yet; both processors skip URLs, so the item
becomes `ready` without any network access. Future fetching must apply independent
SSRF checks; accepting a URL is not permission to fetch it.

## Try file capture

Compose creates a persistent `bag-storage` volume automatically. New environment
options have defaults, so an existing `.env` continues to work. In
<http://localhost:8000/docs>, authorize with
your token, open `POST /api/v1/capture/file`, choose **Try it out**, select a file
and leave `metadata` as `{}`. Execute, then use the returned ID in
`GET /api/v1/items/{item_id}/content` to download the original.

The multipart fields are `file` and optional `metadata` (a JSON string):

```sh
read -r -s BAG_TOKEN
export BAG_TOKEN
CAPTURE_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -sS http://localhost:8000/api/v1/capture/file \
  -H "Authorization: Bearer $BAG_TOKEN" \
  -H "Idempotency-Key: $CAPTURE_ID" \
  -F 'file=@/path/to/document.pdf' \
  -F 'metadata={"user_note":"For later","source":"api"}'
# Replace ITEM_ID with the returned ID and choose a new output filename.
curl --fail-with-body http://localhost:8000/api/v1/items/ITEM_ID/content \
  -H "Authorization: Bearer $BAG_TOKEN" --output downloaded-original.pdf
unset BAG_TOKEN
```

Files are limited to 50 MiB by default (`BAG_MAX_UPLOAD_BYTES`); empty files are
allowed. Metadata supports the same note, source, capture time and retry key as text.
File signatures determine the stored MIME type; unrecognized formats fall back to
`application/octet-stream`, and the worker later reclassifies strict UTF-8 text as
`text/plain` with its content in `extracted_text`. The client MIME and filename
extension are ignored. Downloads always use an attachment with `nosniff`, even for
recognized images or HTML.

Files with equal bytes share physical storage, while separate captures keep their
own notes and filenames. Keys are shared across text/URL/file routes: a reused key
returns the original capture. Text items still expose originals in their JSON
`content`; the download endpoint currently serves file blobs only.

To verify persistence, capture a file, run the following, then download the same
ID and compare it to the original with `cmp original.pdf downloaded-original.pdf`:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml restart postgres bag-api bag-worker
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --wait
```

## Token administration and recovery

Run these commands on the server, using access to the Compose deployment. They do
not require an existing bearer token. After updating the image with `up -d --build`:

```sh
# Create a separate token for a client; plaintext appears once in the JSON output.
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api \
  bag token create --name laptop

# List IDs, names and creation/use/revocation timestamps, never secrets or hashes.
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api \
  bag token list

# Replace TOKEN_ID with an ID from the list, not the bearer secret.
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api \
  bag token revoke TOKEN_ID

# Lost all tokens? Create a new credential for the existing user and data.
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api \
  bag token recover
```

Save each newly printed `token` in a password manager. Recovery does not replace
the user, alter captures or revoke other tokens. If a token might be compromised,
explicitly revoke its ID after recovering access. Revoking the last active token
is allowed because server-side recovery remains available. Repeating revocation
is harmless; repeating create/recover creates another token, so these commands are
not idempotent. If output was lost, list the tokens, revoke the unused ID and create
another. Re-running `bag init` does not recover a token.

The sole MVP user is selected automatically. If multiple users exist, all four
commands require `--owner UUID`; a mismatched owner cannot revoke another user's
token. With no user, run `bag init` first. These are privileged server administration
commands, not public recovery endpoints or client-side database access. Web token
management comes with the future web UI. Existing deployments need no new migration.

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

For host-based development, with PostgreSQL running and `.env` configured, run the
API and, in a second terminal, the worker:

```sh
uv run bag migrate
uv run bag init
uv run uvicorn bag.api:create_app --factory --reload --no-access-log
uv run bag worker
```

## Deployment and backup

Configuration is documented in [`.env.example`](.env.example). Replace development
credentials before deployment; use URL-safe database passwords (or percent-encode
credentials in URLs). API and database ports bind to loopback. Remote access needs
a TLS reverse proxy with request size/time limits. No telemetry or external content
services are used.

Data lives in `bag_postgres-data` (database/text) and `bag_bag-storage` (original
files). Custom Compose project names change these volume prefixes. Stop API and
worker writes during backup and copy **both** the dump and storage archive. Keep
`.env` separately and securely. Use new backup filenames for each snapshot:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml stop bag-api bag-worker
docker compose --env-file .env -f deploy/compose/compose.yaml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > bag.dump
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm --no-deps -T bag-api \
  tar -C /data/storage -cf - . > bag-storage.tar
docker compose --env-file .env -f deploy/compose/compose.yaml up -d bag-api bag-worker
```

Restore into an empty replacement database with `pg_restore`, and extract the matching
archive into an empty replacement storage volume, owned by UID 10001. Preserve paths
and bytes; verify downloads before switching services. Neither component alone is a
complete backup. `docker compose down` preserves data; `down -v` destroys volumes.
For updates: back up, build, explicitly run `bag migrate`, then recreate API/worker
with the quickstart commands. Tokens remain valid across restarts; use the server-side
recovery command above if the plaintext credential was lost. Stopping the worker
during a backup is safe: a job interrupted mid-run is retried after its lease expires,
and queued jobs wait.

## Planned stack

Python 3.12+ with FastAPI, PostgreSQL 17 for data, full-text search and the job queue, content-addressed object storage (filesystem or S3-compatible), a small TypeScript PWA, and a GTK4 Linux client. Details and reasons are in the spec.

## License

MIT, see [`LICENSE`](LICENSE).
