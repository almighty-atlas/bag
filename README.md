# Bag of Holding

> A self-hosted, capture-first personal memory system. Drop anything in. Organize later. Retrieve by meaning.

**Status:** Phase 2 complete. Text, URL and file capture, bearer and session
authentication, durable originals, a leased PostgreSQL job queue, MIME verification,
text extraction, language detection, SSRF-guarded page fetching, ranked multilingual
search, tags, collections, trash, purge, garbage collection and export all work. A
first Preact web UI (login, feed, search, capture, detail, trash) is available; the
PWA polish, Linux client and mobile sharing follow.

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

Every capture schedules the processors in the same transaction as the item:
`mime_detect` verifies the type from stored bytes, `text_extract` fills
`extracted_text` for text captures and UTF-8 text files (up to 1 MiB),
`language` sets `de` or `en` when the text clearly reads as German or English
(which selects the stemmer for search), `url_fetch` archives saved links,
`image_meta` records image dimensions from the file header and `pdf_text` makes
the text layer of PDFs searchable (scanned PDFs without text stay as they are; no
OCR). Other binaries are stored unchanged and skipped by extraction. The worker
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

## Editing title, note and language

Send only the fields to change; `null` clears a field. Setting `language` to `de`
or `en` fixes the search stemmer and stops automatic detection from overriding
it; setting it to `null` lets detection run again on the next reprocess:

```sh
curl --fail-with-body -sS -X PATCH "http://localhost:8000/api/v1/items/$ITEM_ID" \
  -H "Authorization: Bearer $BAG_TOKEN" -H 'Content-Type: application/json' \
  --data '{"title":"Umzugsnotizen","user_note":"Bücherkisten zuerst","language":"de"}'
```

Content, kind and extracted text cannot be edited; originals stay as captured.

Tags and collections are plain names. Send the complete list you want on the
item; unknown names are created, and an empty list removes your assignments:

```sh
curl --fail-with-body -sS -X PATCH "http://localhost:8000/api/v1/items/$ITEM_ID" \
  -H "Authorization: Bearer $BAG_TOKEN" -H 'Content-Type: application/json' \
  --data '{"tags":["wohnung","todo"],"collections":["Umzug 2026"]}'
curl --fail-with-body -sS "http://localhost:8000/api/v1/tags" -H "Authorization: Bearer $BAG_TOKEN"
curl --fail-with-body -sS "http://localhost:8000/api/v1/items?tag=todo" \
  -H "Authorization: Bearer $BAG_TOKEN"
```

`GET /api/v1/tags` and `/api/v1/collections` list names with item counts;
`POST` with `{"name": "..."}` creates one ahead of time, `PATCH /api/v1/tags/{id}`
renames and `DELETE` removes a name with its assignments (items stay). Both
filters also work on `/api/v1/search`.

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

Permanent removal happens in two steps. `bag purge` deletes items that have been
in the trash longer than `BAG_TRASH_RETENTION_DAYS` (default 30); `bag gc`
afterwards removes stored files no item references any more, plus temporary files
left by crashes. The worker runs both automatically every
`BAG_MAINTENANCE_INTERVAL_HOURS` (default 24; set `0` to leave it to cron). Preview
or trigger them by hand with the commands below (`--dry-run` shows what would go):

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag purge --dry-run
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag purge
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api bag gc
```

`bag gc` briefly pauses captures while it deletes a batch and skips files younger
than one hour (`--min-age-hours`). Purged items cannot be restored; take a backup
first if in doubt. `bag purge` also drops finished job rows older than
`BAG_JOB_RETENTION_DAYS` (default 7); processing state per item is unaffected.

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
URLs are stored exactly as entered and never normalized. The worker then fetches
the page: it resolves the host, refuses private, loopback and link-local addresses,
connects only to the checked address, follows up to five re-checked redirects and
reads at most `BAG_FETCH_MAX_BYTES` (5 MiB). The page is kept as a snapshot, its
title fills an empty `title`, and HTML or plain text becomes searchable
`extracted_text`. The stored page is available as a download at
`GET /api/v1/items/{id}/snapshot` (always an attachment, never rendered by the
API). A refused destination is reported as `skipped` in the `url_fetch` run, not
as an error. Set `BAG_FETCH_URLS=false` to keep the worker
offline; the only outbound request Bag ever makes is this GET to the saved URL's
own host.

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

## Web UI

The web app runs as the `bag-web` service on <http://localhost:8080> (`BAG_WEB_PORT`)
and proxies the API on the same origin. It offers login, a feed, search, capture
of text, links and files with an optional thought, item details with original
download, editing of title, note, tags, collections and search language,
processing state, the trash with restore, client token management, filters by
kind, tag, collection and date, and a page to rename or delete tags and
collections. Press `/` to jump to the search box. Build and start it after
setting a password:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build bag-web
```

The app installs as a PWA: a service worker caches the shell and hashed assets so
the UI opens offline (API data is never cached), and on Android the installed app
appears in the share sheet. A shared link or text lands in the capture box; shared
files are parked by the service worker and uploaded when you press "In die
Tasche", so you can add a thought first. Fetched pages can be previewed inside the
item view in a sandboxed frame that blocks scripts and every network request.

For frontend development run `npm ci && npm run dev` in `apps/web` (Node 22); the
dev server proxies `/api` to <http://127.0.0.1:8000>. With plain http on
localhost set `BAG_COOKIE_SECURE=false`, otherwise the browser drops the cookie.

## Web login

The web UI signs in with a username and password instead of a bearer token. Set
them once on the server (the prompt hides input; `--stdin` reads one line for
automation with a secret manager). Passwords need at least 10 characters:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm bag-api \
  bag password set --username atlas
```

`POST /api/v1/session` with `{"username": ..., "password": ...}` and the header
`X-Bag-Csrf: 1` sets an `HttpOnly` session cookie; every later state-changing
request from the browser must send that header as well. `GET /api/v1/session`
shows who is signed in, `DELETE` signs out. Tokens for clients are managed at
`/api/v1/tokens` and only with a session, never with another token. Behind a
plain-http development setup set `BAG_COOKIE_SECURE=false`; in production keep the
default and terminate TLS at the reverse proxy. Setting a new password signs out
all sessions.

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
suite, lint, strict type checking, Python packaging, the container builds, the web
tests and build, and `scripts/e2e.sh`: a smoke test that starts a disposable
Compose stack under its own project name and ports, signs in through the web
proxy, captures text, a private URL and a file, waits for processing, searches,
downloads, trashes, restores, exports and tears everything down. Run it locally
with Docker available; it never touches the `bag` project's volumes.

For host-based development, with PostgreSQL running and `.env` configured, run the
API and, in a second terminal, the worker:

```sh
uv run bag migrate
uv run bag init
uv run uvicorn bag.api:create_app --factory --reload --no-access-log
uv run bag worker
```

## Export

`bag export <dir>` writes a complete, offline-readable copy of one user's Bag into
an empty directory: `manifest.json`, `items.jsonl` (including trashed items and
text originals), `blobs.jsonl`, `tags.jsonl`, `collections.jsonl`,
`relations.jsonl`, `processing_runs.jsonl` and `objects/<sha256>` for every stored
original. Every object is verified against its hash while copying; a corrupt
original aborts the export. With Compose, export into a mounted host directory:

```sh
mkdir -p ./export && docker compose --env-file .env -f deploy/compose/compose.yaml run --rm \
  -v "$PWD/export:/export" bag-api bag export /export/$(date +%Y-%m-%d)
```

The format is versioned (`version: 1`) and documented in
`docs/decisions/0014-export-format.md`. `bag import <dir>` reads it back into the
current user: every object is verified against its hash before anything is
written, rows are inserted only where their ID is missing (so repeating an import
changes nothing), and an ID that belongs to another user aborts the import.
Trashed items stay trashed. Run `bag reprocess` afterwards if the export came from
an older version with fewer processors. The export is not a substitute for the
database dump plus storage archive described below, but it is the portable copy
you can read without Bag of Holding:

```sh
docker compose --env-file .env -f deploy/compose/compose.yaml run --rm \
  -v "$PWD/export:/export" bag-api bag import /export/2026-09-26
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
