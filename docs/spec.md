# Bag of Holding — Product and Architecture Specification

> A self-hosted, capture-first personal memory system. Drop anything in. Organize later. Retrieve by meaning.

**Status:** target specification. Existing behavior is recorded in `docs/architecture.md`; future work is tracked in `TODO.md`. Not every planned platform interaction is implemented.

This specification is written for the implementing agent (Claude Code, Codex or a human). Working rules, priorities and the definition of done live in [`AGENTS.md`](../AGENTS.md). Outstanding work lives in [`TODO.md`](../TODO.md).

## 1. Product vision

**Bag of Holding** is a personal capture and memory application centered on one interaction: **put anything into the bag with as little friction as possible**.

The initial target platform is Linux. The system is nevertheless designed around a server API so the same memory can later be accessed and populated from browsers and mobile devices.

Users should be able to capture:

- files and folders
- images
- PDFs and other documents
- URLs
- selected or pasted text
- code snippets
- arbitrary clipboard content
- later: audio, voice notes, email and app integrations

The application must not force the user to choose folders, tags, item types or destinations before saving. Capture comes first. Classification and enrichment happen afterwards.

The long-term goal is a private, self-hosted external memory that can answer not only **"Where did I save this?"** but **"What do I know about this?"**

The **Shared Shelf** is a quick view of the **same durably saved Items**, accessible
from Linux, macOS and phones. It is not a separate temporary staging area:
capturing to Shelf is capturing to the Bag archive. Details, constraints and
cross-device acceptance criteria: [`docs/concepts/shared-shelf.md`](concepts/shared-shelf.md)
and [ADR-0020](decisions/0020-shared-shelf.md).

## 2. Principles

1. **Capture first.** Saving must be fast and require no mandatory metadata.
2. **Preserve originals.** Extracted or generated data is never the canonical copy. The original captured content is kept whenever technically possible and treated as untrusted input.
3. **Universal item model.** Every captured object is an `Item`, regardless of source or media type.
4. **The server is the memory.** Desktop, browser, mobile and integrations are clients of the Bag API. No client is a canonical store.
5. **Organization is optional.** No folder hierarchy. Collections, tags and relations never force an item into exactly one place.
6. **Enrichment is asynchronous.** `Capture -> Store -> Enrich -> Index -> Retrieve`. Saving never waits for extraction, OCR, embeddings or AI.
7. **Self-hosted and privacy-friendly.** Usable without any third-party cloud service. AI providers are replaceable; local inference is a first-class future option.
8. **AI is not on the capture path.** The MVP must remain fully useful with every AI feature disabled.
9. **Data safety over everything.** Deletion is reversible, captures are idempotent, and enrichment failures never lose or hide originals.

### Non-goals and anti-patterns

Do not:

- turn capture into a metadata form or require folders before saving
- make AI mandatory, or store only embeddings/summaries instead of originals
- couple clients to database or storage internals
- let the Linux client become the canonical database
- make enrichment synchronous with capture
- introduce microservices, Redis or a separate vector database without a concrete need
- silently send personal content to third parties
- let generated metadata overwrite user input
- optimize visual polish before capture reliability
- implement LocalSend integration, a custom peer-to-peer transfer engine, device pairing, universal clipboard synchronization or cross-screen cursor/drag transfer as MVP features
- introduce a separate expiring Shelf datastore or automatically delete captures after another device opens them

## 3. User experience

### 3.1 Linux desktop

The **Bag Shelf** is a small, summonable view of recent saved items with a
low-friction drop/paste target. Capturing a file, image, URL or text stores it
through the existing Bag API; the Shelf does not own separate content or
auto-expiring transfers. A successful save is shown only after durable server
confirmation. An optional thought can be added after capture.

**First platform to validate:** a DMS 2.0 bar/dock widget with popout on
Niri/Wayland. It should expose a quick capture target, recent items and
open/download/copy actions. Evaluate DMS drag-in, popout lifecycle and native
drag-out with a small prototype **before** committing to DMS as the sole Linux
runtime. GTK4/libadwaita remains a fallback if needed, with the same API
contract and no duplicate store. A desktop helper is allowed only for a
demonstrated OS/Wayland limitation.

Client states: idle, drag hover, uploading, saved, failed/retry. Never block
on a success modal, silently discard an unsuccessful upload, or claim an
offline capture was saved remotely. Retried captures use the same
`client_capture_id`. The existing server remains the single source of truth.

Wayland is the primary environment. Global shortcuts, dragging payloads into
a DMS surface, dragging files back out and always-on-top behavior must be
verified on the actual compositor; do not assume cross-desktop portability or
introduce insecure workarounds.

### 3.2 macOS desktop

Use the **existing Bag web/PWA interface** first for capture, recent items,
search, open and original download. This already offers cross-device access
without a native macOS daemon. A native menu-bar Shelf or native drag-out is
a deferred improvement and not an MVP dependency.

### 3.3 Web / PWA


A responsive interface with:

- global capture/paste action
- recent-items timeline (also presented as the compact **Shelf**)
- search with filters
- item detail view with original-content open/download
- notes, tags, collections
- processing state and per-processor errors
- trash view with restore

The PWA is also the initial mobile browsing interface.

### 3.4 Mobile

A phone uses its **system Share menu** to place content into Bag and a compact,
touch-friendly **Shelf screen** (recent items and search) to retrieve it.
Do not build a floating desktop Shelf or require a native Android app.

On Android, the installed Bag PWA already declares a Web Share Target for
URLs, text and files. The current implementation **prefills** the capture UI
and requires the user to confirm with **"In die Tasche"**; preserve that
explicit durable-save boundary and test the full flow on a real device. The
mobile Shelf prioritizes recent items, opening links, copying text and
downloading or sharing files. Browser upload/paste remains the fallback.

On other phones, including iOS, use the responsive web interface first.
Native system Share integration is not guaranteed by the PWA on every
platform; only consider a small native extension if a real usage need is
demonstrated. No platform maintains its own canonical Shelf database.

### 3.5 Browser extension (post-MVP)

Save page, link, selection or image to Bag, each with an optional thought. Uses the same public API as every other client.

## 4. Architecture

```text
 Linux (DMS feasibility) ─┐
 macOS (web/PWA) ──────────┼──────> Bag API ──> PostgreSQL + Blob Storage
 Android (PWA + Share) ───┘             │
                                       └──────> PostgreSQL Job Queue + Worker
```

All platforms access the same saved Bag Items through the existing
authenticated API. No direct device-to-device channel, duplicate Shelf store,
new sync infrastructure or extra service is required for Shared Shelf v1.
An item appears on another device when its feed is refreshed after the
capture response confirms storage; live push is not promised.

### 4.1 Technology decisions

These are defaults. The agent may replace a choice for a concrete technical reason and must record it as an ADR in `docs/decisions/`.

| Concern | Decision | Notes |
|---|---|---|
| Backend | Python 3.12+, FastAPI | Extraction/ML tooling integrates well with Python |
| Database | PostgreSQL 17 | Single store for data, full-text search and the job queue. `pgvector` is added later on the same instance |
| IDs | UUIDv7, native `uuid` column | Time-sortable, no extension needed. Generated server-side |
| Job queue | PostgreSQL-backed (jobs table with `FOR UPDATE SKIP LOCKED`, or a library such as `procrastinate`) | No Redis in the MVP. Jobs are idempotent and retryable |
| Object storage | Abstraction with filesystem backend (dev) and S3-compatible backend (MinIO, production) | Content-addressed layout, see 5.2 |
| Web frontend | TypeScript, Vite, Preact or Svelte, PWA | Pick one in an ADR. Prefer small over a large framework |
| Linux desktop | DMS 2.0 prototype on Niri first; GTK4 + libadwaita fallback | Validate Wayland drag/drop and API access before picking a final client implementation; ADR-0020 |
| Node | 22 LTS | Frontend tooling only |
| Containers | Docker Compose | See section 13 |

### 4.2 Timestamps

All timestamps are `timestamptz`, stored in UTC.

- `created_at`: set by the server when the row is inserted. Authoritative.
- `captured_at`: the moment the user performed the capture, as reported by the client. Optional; defaults to `created_at`. Allows offline queues to preserve the real capture time.
- `updated_at`: server-maintained.

## 5. Domain model

Every entity that a user owns carries `owner_id` from day one. The MVP has exactly one user, seeded on first start, but the schema must never assume that.

### 5.1 Item

```text
id                  uuid (v7)
owner_id            uuid -> user
kind                text          file | image | document | url | text | code | unknown (extensible)
source              text          client that captured: linux-client | web | api | share | extension | ...
source_application  text nullable application the content came from, if known
source_url          text nullable
mime_type           text nullable verified server-side, never trusted from the client alone
language            text nullable ISO 639-1, detected or user-set
original_filename   text nullable display only, never used for storage paths
title               text nullable
user_note           text nullable
extracted_text      text nullable
content_hash        text nullable sha256 of the primary blob or of the text content
processing_status   text          queued | processing | ready | partial | failed (derived, see section 7)
metadata            jsonb
search_vector       tsvector      generated column, see section 8
created_at          timestamptz
captured_at         timestamptz
updated_at          timestamptz
deleted_at          timestamptz nullable   soft delete
```

Keep strongly typed columns for frequently queried fields. Put uncommon extractor or source metadata into `metadata`. Clients must not fail on an unknown `kind`.

### 5.2 Blob

An item owns zero or more binary objects: original file, fetched page snapshot, preview, thumbnail, future OCR artifact.

```text
id            uuid
item_id       uuid
role          text   original | snapshot | preview | thumbnail | ...
storage_key   text   derived from content hash, see below
sha256        text
size_bytes    bigint
mime_type     text
created_at    timestamptz
```

**Storage is content-addressed.** The storage key is derived from the SHA-256 of the content, for example `ab/cd/abcd…`. This gives deduplication for free, guarantees that user-supplied filenames never influence paths, and allows integrity checks on backup and restore. Two items may reference the same stored object; the object is only removed when no live or trashed blob references it.

### 5.3 Tag

Many-to-many with items, scoped by `owner_id`. Automated tags must be distinguishable from user tags and carry `created_by` (user | system | ai) and nullable `confidence`.

### 5.4 Collection

User-controlled groupings, many-to-many with items. Collections are not folders: they never change item identity or storage paths.

### 5.5 Relation

```text
source_item_id
target_item_id
relation_type    related_to | derived_from | references | duplicate_of | part_of
created_by       user | system | ai
confidence       nullable
metadata         jsonb
```

### 5.6 ProcessingRun

One row per processor per item. This is the source of truth for enrichment state; the item's `processing_status` is derived from it.

```text
id            uuid
item_id       uuid
processor     text   mime_detect | metadata | text_extract | url_fetch | image_meta | index | ...
status        text   pending | running | succeeded | failed | skipped
attempts      int
last_error    text nullable
started_at    timestamptz nullable
finished_at   timestamptz nullable
```

### 5.7 User and ApiToken

```text
user:      id, display_name, created_at
api_token: id, owner_id, name, token_hash, created_at, last_used_at, revoked_at
```

Tokens are stored hashed. The plaintext is shown exactly once at creation.

### 5.8 Deletion semantics

- `DELETE /items/{id}` sets `deleted_at`. The item disappears from feeds and search but stays in the trash view.
- `POST /items/{id}/restore` clears it.
- A background job purges trashed items after a configurable retention period (default 30 days), then garbage-collects storage objects with no remaining references.
- Hard delete is only available as an explicit purge action.

### 5.9 Duplicate semantics

Capture is never rejected because of a duplicate. When a new item's `content_hash` matches an existing live item of the same owner:

- the item is still created (the user may have a new thought or context for it)
- the stored object is shared through content addressing
- a `duplicate_of` relation with `created_by = system` is added
- the capture response reports `duplicate_of` so clients may show "already in your Bag"

## 6. Capture contract

One conceptual capture operation, even though JSON and multipart transports differ.

### 6.1 Idempotency

Every capture request may carry a `client_capture_id` (JSON field) or `Idempotency-Key` header: a UUID generated by the client at capture time. It is unique per owner. Replaying a request with the same key returns the originally created item and does not create a second one. Clients with retry or offline queues (see 3.1, 12) must always set it.

### 6.2 Requests

Text:

```json
{
  "kind": "text",
  "content": "Interesting idea about local RAG...",
  "user_note": "Could be useful for Bag later",
  "client_capture_id": "018f5e2a-...",
  "captured_at": "2026-09-26T11:02:00Z"
}
```

URL:

```json
{
  "kind": "url",
  "url": "https://example.org/article",
  "user_note": null,
  "client_capture_id": "018f5e2a-..."
}
```

File: multipart upload with the file part plus an optional JSON metadata part using the same fields.

`kind` may be omitted; the server then detects it. `source` is derived from the token or set explicitly by the client.

### 6.3 Response

Return as soon as the original input has been durably persisted (row committed, blob fsynced or acknowledged by object storage). Never wait for URL fetching, OCR, previews or AI.

```json
{
  "id": "018f5e2b-...",
  "status": "stored",
  "processing_status": "queued",
  "duplicate_of": null
}
```

## 7. Processing pipeline

Each processor is an independently retryable job that reads the item and its blobs, writes its results, and records a `ProcessingRun`. Processors do not depend on a single linear state machine; a failed text extraction does not prevent indexing of the title and note.

Initial processors, in dependency order:

1. `mime_detect`: verify type from content, set `mime_type` and `kind` if unknown
2. `metadata`: basic file metadata
3. `text_extract`: text from supported documents
4. `url_fetch`: metadata and content snapshot for URLs, with SSRF protections
5. `image_meta`: dimensions and basic image metadata
6. `index`: refresh search data

Derived item status:

- `queued`: no run has started
- `processing`: at least one run is pending or running
- `ready`: all runs succeeded or were skipped
- `partial`: some succeeded, some failed after final attempt
- `failed`: every meaningful processor failed

The UI must always show that the item itself is saved, and which processors failed and why. OCR follows after the basic pipeline is stable.

## 8. Search and retrieval

The MVP provides lexical search over title, extracted text, URL and note, with filters for tag, collection, kind and date range. Results are ranked and return snippets that show why an item matched.

### 8.1 Multilingual full-text search

Content will be stored in at least German and English. PostgreSQL text search configurations are language-specific, so:

- `search_vector` is a generated column combining `to_tsvector('simple', …)` with `to_tsvector(config, …)`, where `config` is chosen from the item's `language` (`german`, `english`, fallback `simple`)
- queries run against both the `simple` and the user-selected or detected language configuration
- the `language` field is set by a lightweight detector during processing and may be corrected by the user

### 8.2 Future hybrid retrieval

```text
query
  ├─ full-text search
  ├─ embedding similarity (pgvector)
  └─ structured filters
          ↓
      rank/merge
          ↓
       results
```

The search API is designed so semantic results can be merged with lexical results without changing the client contract.

## 9. API

Versioned under `/api/v1`. Minimum operations:

```text
POST   /capture/text
POST   /capture/url
POST   /capture/file
GET    /items                      (filters: kind, tag, collection, from, to, status, trashed)
GET    /items/{id}
PATCH  /items/{id}                 (title, user_note, language, tags, collections)
DELETE /items/{id}                 (soft delete)
POST   /items/{id}/restore
GET    /items/{id}/content         (original blob)
GET    /items/{id}/processing      (processing runs)
POST   /items/{id}/reprocess
GET    /search
GET    /tags
POST   /tags
GET    /collections
POST   /collections
GET    /health
GET    /ready
```

Capture must remain trivial to call from tiny scripts (`curl` with a bearer token and one JSON field). Generate and maintain an OpenAPI description.

## 10. Authentication and security

### 10.1 Authentication

- **API clients** (Linux client, share integrations, scripts): bearer tokens from the `api_token` table, created and revoked through the web UI. One token per client.
- **Web UI**: server-side session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`) with CSRF protection on state-changing requests. Login with username and password for the single seeded user; passwords hashed with argon2.
- Every request resolves to an `owner_id`; every query is scoped by it.

### 10.2 Security requirements

- file size limits configurable per deployment
- MIME type verified from content, never trusted solely from the client
- filenames are display metadata only; storage paths come from content hashes
- uploaded content is never executed
- URL fetching runs with SSRF protections: block private and link-local ranges, resolve DNS before connecting, limit redirects and response size, set timeouts
- stored HTML and text are rendered safely (sanitized or sandboxed)
- originals are preserved but treated as untrusted
- secrets come from environment variables or a secret store, never from committed files

## 11. Privacy, export and backup

- no telemetry
- no mandatory external service
- no content leaves the server unless an AI or fetch provider is explicitly configured, and the configuration states what leaves
- local inference remains possible as a provider
- **Export is an MVP feature:** `bag export <dir>` writes every original object plus `items.jsonl`, `tags.jsonl`, `collections.jsonl` and `relations.jsonl`. The format is documented and stable enough to be imported again by a later `bag import`.
- persistent data locations are documented for backup; a backup is the PostgreSQL dump plus the storage directory or bucket

## 12. Linux capture client requirements

The client, whether a validated DMS integration or GTK4 fallback, must:

1. authenticate securely with a per-client bearer token stored in the desktop keyring (Secret Service), not plain config
2. expose a summonable Shelf surface with a drop/paste target and recent-items view
3. accept file/image/URL/text input to the extent validated by the target compositor
4. upload via existing Bag capture endpoints with stable `client_capture_id` values
5. show accurate uploading, confirmed-save and error states without success modals
6. provide open/download/copy access to the stored original via the API; native file drag-out is optional after feasibility validation
7. offer optional post-capture notes without blocking the initial durable capture
8. protect unsent captures from silent loss (explicit retry at minimum; add a local retry queue only if the reliability test demonstrates a need)

The first prototype is a small **vertical slice**, not a commitment to build
every planned feature or a second archive. Behavior must not diverge between
desktop and web clients. See [Shared Shelf](concepts/shared-shelf.md).

## 13. Deployment

Docker Compose for development and self-hosting. Services:

```text
bag-api
bag-web
bag-worker
postgres
minio        # optional, only for the S3 storage mode
```

- configuration is environment-variable driven and documented in `.env.example`
- health and readiness endpoints for every service
- database migrations run explicitly (`bag migrate`), not implicitly on container start, so a failed migration is visible
- persistent volumes: `postgres-data`, `bag-storage`; documented for backup
- restart and image update must never lose data

## 14. Repository structure and documentation

```text
bag/
├── apps/
│   ├── api/
│   ├── web/
│   └── linux-client/
├── packages/
│   └── shared/
├── docs/
│   ├── spec.md                 this document
│   ├── architecture.md         the architecture as built
│   ├── data-model.md           entities, fields, migration notes
│   ├── capture-pipeline.md     how each input becomes an item
│   └── decisions/              ADRs
├── deploy/
│   └── compose/
├── scripts/
├── tests/
├── .env.example
├── AGENTS.md
├── CLAUDE.md                   points to AGENTS.md
├── LICENSE
├── README.md                   setup, quickstart, status
└── TODO.md
```

Adapt if the chosen tooling has a better convention. `docs/architecture.md` records the actual architecture, not the aspirational one; this spec is not updated to match reality, ADRs are.

## 15. Testing

Prioritize data safety and capture reliability. Minimum coverage:

- text, URL and file capture
- binary and original preservation, byte for byte
- **durability before response:** a crash between storage and response must not leave an acknowledged capture missing
- idempotent replay with the same `client_capture_id`
- concurrent uploads of the same content (dedupe under a race)
- duplicate filenames and unusual Unicode filenames and text
- invalid or malicious MIME claims
- extraction failure without data loss, and correct `partial` status
- soft delete, restore and purge with storage garbage collection
- database migrations up and, where feasible, down
- authentication and owner scoping
- search in German and English
- export round trip
- API error behavior

Integration tests cover the complete path: `capture -> persisted item -> processors -> searchable result`.

## 16. Observability

Structured logs. Every capture and processing job is traceable through stable IDs (item id, run id, `client_capture_id`) without logging private content. Basic health and readiness endpoints. No heavyweight observability stack in the MVP.

## 17. MVP scope

The first useful release is complete when a user can:

1. run Bag of Holding through the documented self-hosted deployment
2. open the Linux capture target
3. drop a file, image, URL or text into the Bag
4. see immediate confirmation after durable storage
5. open the web UI and see captured items
6. inspect an item, its original content, metadata and processing state
7. search extracted text, titles, URLs and notes in German and English
8. add or edit a note, tags and collections
9. delete an item and restore it from the trash
10. use the responsive UI from a phone
11. restart and update the stack without losing stored data
12. export the whole Bag to a directory
13. retrieve a saved item on macOS or a phone after capturing it on Linux (and vice versa), using the existing server API

**Not required for MVP:** LLM chat, embeddings, semantic search, automated AI organization, native macOS/Android apps, custom P2P transfer, LocalSend integration, a synchronized universal clipboard, live device push, a temporary Shelf datastore, browser extension or elaborate visual effects.

## 18. Roadmap

### Phase 0 — Foundation

Repository and tooling, `AGENTS.md`, architecture and data model docs, Compose environment, migrations, CI for lint, tests and build. See `TODO.md`.

### Phase 1 — Capture backend

Item model, blob storage abstraction, text, file and URL capture, durable response semantics, idempotency, token authentication.

**Exit:** inputs can be captured through the API, replayed safely and survive a restart.

### Phase 2 — Processing and search

Job queue and worker, processors 1 to 6, processing runs, multilingual full-text search, search and filter API, soft delete and purge, export.

**Exit:** captured content becomes searchable automatically without risking originals.

### Phase 3 — Web / PWA

Feed, search, item detail, upload and paste, notes, tags, collections, processing and error state, trash, token management, PWA behavior.

**Exit:** Bag is independently useful from a browser and a phone.

### Phase 4 — Linux Shared Shelf

Validate a DMS 2.0 drop/popout prototype on Niri before choosing the final Linux
client approach; fall back to GTK4 if necessary. Use the existing Bag API for
capture and recent-items retrieval, with correct success/failure feedback and
secure token storage. A local retry queue or native drag-out follows measured
reliability/Wayland needs, not speculation.

**Exit:** dropping an item into Bag on Linux is reliably persisted and the same
item can be opened/downloaded on macOS through the existing web UI.

### Phase 5 — Mobile Shared Shelf

Test the existing Android PWA Share Target on a real device, improve its compact
recent-items/search experience and contextual open/copy/download/share actions,
and verify the complete Linux ↔ macOS ↔ Android capture/retrieval flows. Retain
browser-upload fallback on unsupported platforms.

**Exit:** common phone content reaches Bag through native Share → Bag where
supported, and stored items can be retrieved across devices without a new sync
or peer-transfer service.

### Phase 6 — Intelligence

Only after the previous phases are stable: `pgvector`, embeddings provider abstraction, hybrid search, AI enrichment provider abstraction, summaries, tags, entities, relations, retrieval API for RAG and agents. Local and remote providers stay interchangeable.

## 19. Future AI layer

Not a prerequisite for anything above. The data model and pipeline must make these possible later: summaries, automatic tags, entity extraction, image understanding, OCR cleanup, embeddings, semantic search, related-item suggestions, automatic relations, question answering over personal memory, agent and RAG access.

Example future query:

> Show me everything I collected about authentication during the last three months.

The answer may combine URLs, notes, screenshots, PDFs and files because all are items.

AI-generated information always carries provenance (`created_by = ai`, provider, model, confidence). It never silently overwrites user-entered metadata or extracted source content.
