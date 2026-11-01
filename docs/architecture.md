# Architecture as built

One Python package under `apps/api/src/bag` targets Python 3.12+. FastAPI serves the
API; synchronous Psycopg connections execute parameterized SQL in short-lived
transactions. There is no ORM or Redis. Alembic uses SQLAlchemy only to apply
packaged SQL migrations. `apps/web` is a Preact/Vite/TypeScript app (ADR-0017)
that talks only to the API: login, feed with cursor paging and text excerpts, search with snippets,
text/URL/file capture with the post-capture note, item detail with original
download, editing of title, note, tags, collections and search language,
processing state, reprocess, trash with restore, client token management, feed
filters for kind, tag, collection and capture date (kept in the hash route), a
tags-and-collections page with rename and delete, and `/` to focus search.
A service worker caches only the shell and hashed assets (network first for
navigations, never `/api`; on activation it deletes only stale shell caches, never
the `bag-share` cache). The Web Share Target posts to `/share`: the worker stores
the whole share as one batch with a random batch ID in the `bag-share` cache:
`/shared/<batch>/<file-id>` per file (random file ID, bytes, MIME type) and
`/shared/<batch>/meta` (JSON with batch ID, time, every non-empty `url`, `title`
and `text` value, repeats included, and the file list with IDs, names, types and
sizes). Files are written first and meta last, so a batch without meta was never
acknowledged; if writing fails, the partial batch is removed and the request fails
rather than redirecting. Batches never share a key, so later or concurrent shares
cannot overwrite earlier ones. The worker redirects to `#/share?batch=<id>`.
The app opens that batch in the capture form: a valid HTTP(S) `url` field,
otherwise the only distinct link found in title/text, becomes the content and is
saved as a URL item; the remaining title and text, minus that link, prefill the
visible, editable note. Without a link, or with several different links in free
text, all values become one text capture. A legacy GET `/share?…` maps onto the same
form with its fields inline, without a stored batch. The feed lists all other
unsaved batches under "Noch nicht gespeichert" with "Öffnen und speichern" and
"Verwerfen" (asks for confirmation); the open batch also has "Verwerfen".
Saving ("In die Tasche") sends the content, then each remaining file in order. After
the server confirmed a file, a marker `/shared/<batch>/<file-id>.saved` is written
and only then the file bytes are deleted, so missing bytes alone never mean
"saved". A file with neither bytes nor marker was lost (e.g. cache eviction): it
is skipped, the other files are still saved, and the save ends with an error that
names it ("nicht mehr auf diesem Gerät vorhanden und nie gespeichert"); the batch
stays and the list shows how many files are gone. The batch (meta last, with all
markers) is deleted only after every file is confirmed or on an explicit discard;
any failure keeps the batch. Saves of one batch are serialized (Web Locks where
available, otherwise a queue within the page) and re-read the batch, so a
concurrent second save neither resends confirmed files nor reports a false
success: if the batch is already saved or discarded it ends with "nicht mehr
offen". The Web Locks branch itself is not covered by the Node tests. If the user clears the content field
and saves only the files, the original shared text is discarded with the batch.
Capture IDs of a batch are derived (SHA-256 of batch ID, file ID or content, kind
and note, formatted as a UUID), so they are identical after a page reload: an
unanswered request is replayed with the same key and creates no duplicate, while
edited content or note gets a different key. Other web captures keep one
`client_capture_id` per logical capture in memory (`retry.ts`): the key belongs to
the exact payload (kind, text or URL, note; for a picked file the `File` object and
note) and is dropped once the server confirmed it. Resending unchanged input after
a failed or unanswered request replays the same key, so the server returns the
stored item instead of a duplicate; edited input, a newly picked file or a repeat
after a confirmed save gets a new key, because the server would otherwise answer a
reused key with the first item. A batch of picked files stops at the first
unconfirmed upload and lists the rest with "Erneut versuchen" (same files, notes
and keys; confirmed files are not resent) or "Verwerfen"; these in-memory keys do
not survive a reload.
Fetched HTML snapshots can be previewed in an iframe
with an empty `sandbox` attribute and an injected CSP (`default-src 'none'`), so
no script runs and no request leaves the page. Routing, share classification and
CSP-injection helpers have vitest tests. Further vitest tests run the real `sw.js`
in Node against an in-memory CacheStorage with multipart `POST /share` requests:
consecutive and concurrent shares, a failing cache write, install/activate over
old caches, and saving batches against a fake server that replays known keys
(lost responses, network errors, partial failures, reopening). They do not cover
the Preact components (no DOM test environment) and are not a browser or Android
device test. Shares stashed by the previous worker version under `/shared/<n>`
and `/shared/count` are not migrated and do not appear in the list.

Compose starts PostgreSQL 17, `bag-api`, `bag-worker` and `bag-web`. API and worker
share a non-root image built from locked uv dependencies; `bag-web` is an nginx
image serving the static build and proxying `/api/` to the API on the same origin. `bag worker` runs the job loop in
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

Web login (ADR-0016): `bag password set --username` stores an argon2id hash and
signs out existing sessions. `POST /api/v1/session` opens a server-side session
(random token, SHA-256 stored, expiry from `BAG_SESSION_DAYS`) and sets the
`bag_session` cookie (`HttpOnly`, `Secure` unless `BAG_COOKIE_SECURE=false`,
`SameSite=Lax`, path `/api`). Every route accepts a bearer token first, then the
cookie; cookie-authenticated unsafe requests and login must carry `X-Bag-Csrf: 1`.
`GET /api/v1/session` reports the identity and `DELETE` signs out. Token
management (`/api/v1/tokens`) requires a session, so bearer tokens cannot create
or revoke tokens. An in-process sliding-window limiter answers 429 with
`Retry-After` after `BAG_LOGIN_MAX_FAILURES` failed logins per username or client
address within `BAG_LOGIN_WINDOW_SECONDS`; a successful login clears the count.

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
nor validation performs DNS or HTTP requests. Private addresses can be saved; the
worker's `url_fetch` processor validates destinations independently and skips them.

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
writes the updates, marks run and job, locks the item row and derives the item status
from all runs in SQL (the lock makes a concurrently committed finalize visible): `queued` before any start, `processing` while any run is pending or running,
`ready` when nothing failed, `failed` when nothing succeeded, otherwise `partial`.
Results arriving after a lost lease are discarded. Retryable failures requeue with
exponential backoff from `BAG_JOB_RETRY_SECONDS`, capped at one hour, until
`BAG_JOB_MAX_ATTEMPTS`; permanent failures and reclaims past the limit fail the run.
Unexpected exceptions record only the exception class. Deterministic database limits
such as the search vector size fail the run permanently instead of retrying.

Seven registered processors exist: `mime_detect`, `text_extract`, `language`, `url_fetch`, `image_meta`, `pdf_text` and `thumbnail`. `mime_detect` re-verifies files from stored bytes (signature
library plus a strict UTF-8 plain-text check), sets `mime_type` and re-derives file
kinds; text items are `text/plain` and URLs are skipped. `text_extract` copies text
originals and decodes UTF-8 text files up to 1 MiB into `extracted_text`, noting
truncation in `metadata`; other content is skipped. `language` counts German and
English function words in the same plain text and sets `de` or `en` when the
evidence is clear, skipping otherwise and whenever `metadata.language.user` marks a
user choice. `url_fetch` downloads `url` items under the SSRF policy of ADR-0015:
DNS first, every answer must be public, the socket is pinned to the checked
address, redirects are re-checked, size and time are bounded. It stores the page
as a `snapshot` blob, sets `mime_type` from the declared type, extracts HTML or
plain text into `extracted_text` and fills an empty `title` from the HTML title.
Outcomes may also carry `defaults` (applied only where NULL) and derived blobs
(one row per role, replaced on rerun). `image_meta` reads width and height from
PNG, GIF, JPEG and WebP headers into `metadata.image_meta` without decoding, and
`pdf_text` extracts the text layer of PDF originals with pypdf up to 1 MiB and
2000 pages (ADR-0018); `language` falls back to extracted text on reruns, so
`bag reprocess` after a PDF or page fetch also detects its language. `thumbnail`
decodes PNG, JPEG, WebP and GIF originals in a resource-limited child process and
stores a re-encoded, metadata-free WebP of at most 512 px as a `thumbnail` blob
(ADR-0023, see Thumbnails below). Because
`search_vector` is maintained by database triggers (ADR-0022), so extraction and
language immediately make items searchable with the right stemmer. Every capture enqueues all processors
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

`PATCH /api/v1/items/{id}` edits `title`, `user_note`, `language`, `tags` and
`collections` of a live item under a row lock; absent fields stay, null clears.
Tag and collection lists are set semantics over names: missing names are created
for the owner, the user's previous assignments not in the list are removed, and
system or AI tag assignments are never touched. `GET`/`POST /api/v1/tags` and
`/api/v1/collections` list names with live item counts or create a name
idempotently (201 new, 200 existing); `PATCH`/`DELETE /api/v1/{tags,collections}/{id}`
rename (409 on a clash) or delete a name together with its assignments, never
the items. Item responses and summaries carry sorted
`tags` and `collections`; listing and search accept `tag` and `collection` filters. Choosing a language sets
`metadata.language.user`, which the detector honors, and clearing it removes the
marker so the next run may detect again. Originals, kind and extraction stay
read-only. `DELETE /api/v1/items/{id}` sets `deleted_at` and `POST /api/v1/items/{id}/restore`
clears it; both are idempotent and owner-scoped. Trashed items disappear from
detail, download, processing and default listings but remain listable and
searchable with `trashed=true`. Nothing is removed until `bag purge` hard-deletes
items trashed longer than `BAG_TRASH_RETENTION_DAYS`, one owner-scoped transaction
per item with children removed before the item, and prunes finished `job` rows older
than `BAG_JOB_RETENTION_DAYS` (runs, the visible state, stay). `bag gc` then removes storage
objects no blob row references and crash-left `.upload-*` files: candidates are
scanned without locks, and each deletion batch holds every owner's capture lock
while re-checking references, so it waits for in-flight captures and can never
remove an object a capture is publishing. Both run as explicit commands and, per
ADR-0019, on a schedule inside the worker (`BAG_MAINTENANCE_INTERVAL_HOURS`,
default daily, `0` disables) in a second thread that readiness also watches.

`bag export <dir>` snapshots one owner's tables in a repeatable-read transaction
into JSONL files plus a manifest, then copies every referenced original through the
verified reader into `objects/<sha256>` with fsync. Trashed items are included;
the search vector is not (imports rebuild it through the triggers). The target must be empty, and any failed
verification aborts the run before the manifest is written. `bag import <dir>`
reverses it for one owner: it checks the manifest version, hashes every object
before touching the database, publishes objects through the content-addressed
store and inserts rows with `ON CONFLICT DO NOTHING` in a single transaction, so
reruns are no-ops and IDs owned by someone else are refused.

## Listing and search

`GET /api/v1/items` returns owner-scoped summaries (no `content` or
`extracted_text`) newest first, paged by keyset on the UUIDv7 ID (`cursor` is the
last ID of the previous page, `limit` 1–100). `GET /api/v1/search` parses `q` with
`websearch_to_tsquery` (quotes, `OR`, `-`) in the `simple`, `german` and `english`
configurations (hyphenated, underscored and dotted words also split into their
parts) and matches the stored vector against their OR-combination, so stemming
applies to items with a detected language while exact tokens match everywhere. The
vector (ADR-0022, migration `0005_search_fields`) is an ordinary column that
triggers keep exact in the transaction of every change: `BEFORE` on `item` for title,
original filename, note, URL, extracted text and language, and statement-level
`AFTER` triggers on `item_tag` (insert, delete, and updates that re-point an
assignment: the previous and the new item are refreshed) and `tag` (rename), which
refresh only the affected items of that owner under the item's row lock (`FOR NO KEY
UPDATE`, in `(owner_id, item_id)` order). Weights: A title and
filename, B tag names, C note and URL, D extracted text. Filenames and tags are
indexed in all three configurations as written, split at separators and camelCase,
and with umlauts transliterated, so `rechnung` finds `rechnung-oktober-2026.pdf`.
Results are ordered by a tiered score (exact title or filename; query word in title
or filename; in a tag; in note or URL), then the weighted `ts_rank`, then newest,
paged by a bounded offset. This whole-word search is stage 1. Prefix and substring hits
(ADR-0025, migration `0006_search_partial`) form stage 2, run only when stage 1 does not
fill the page, so they always follow whole-word hits, also across pages; a bounded count
tells where stage 2 continues when the offset lies beyond stage 1. A word of two or more
characters, as typed (never its stem), also matches longer words that start with it in
title, filename, tags, note and URL: a second, small vector `item.search_head` (the A to C
lexemes of `search_vector`, maintained by the same triggers, own GIN index) answers those
lookups, because a prefix scan of the large vector would visit and recheck every document
with a matching word. Extracted text matches whole words only. Exclusions stay exact. A
plain word query of at most five words is also searched as a substring of the filename
(all words in one name; at least one of three characters, because pg_trgm cannot index
less) through a trigram index over `bag_filename_key(original_filename)` (lowercased, plus
an `ä ö ü ß` transliteration), with `\`, `%` and `_` escaped. The two statements do not share a snapshot, partial hits are out of reach behind more than about
1,000 whole-word hits (the `offset` cap), and substrings need a plain word query. Stage 2 orders prefix hits by the
same field tiers using the head vector, then filename-substring-only hits, then newest;
`rank` is `1 + score` for whole-word results and below 1 for partial ones, and
`matched_fields` names the fields containing a whole word (stage 1) or a prefix and, for
filenames, a substring (stage 2). Each result carries `rank`, a plain-text `ts_headline`
snippet over title, note, URL and extracted text with `«`/`»` markers, and
`matched_fields`: the subset of `title`, `filename`, `tag`, `note`, `url` and
`content` (in that order) whose own text contains a positive query word, computed for
the result page only; it is empty for results found by exclusion alone. Summaries of both endpoints carry `excerpt` (ADR-0021): for `kind = text` only, the
query selects `substr(content, 1, 4096)` and `bag.excerpt.make_excerpt` reduces it to at
most 240 characters of plain text (whitespace and line breaks collapsed, invisible and
bidirectional control characters and common Markdown markers removed, cut on a word or
grapheme-cluster boundary with `…` when the text continues); every other kind gets
`null`. No migration; `content` is untouched and a multi-megabyte original never leaves
the database. The excerpt is part of the same owner-scoped, trash-aware query as the rest of
the summary. The web feed (`feed-text.ts`) shows title, else excerpt, else file name,
link, note or kind as the headline, and keeps a note or excerpt that is not the headline
below it; search results show the match snippet. Both endpoints accept `kind`, `status`, `from`/`to`
(capture time, timezone required) and `trashed`. Queries that reduce to nothing
return no results rather than an error. Tag and collection filters exist in both listing and search; semantic search is not implemented. The snippet does not cover filenames or tags (the reasons do), URL host and path words are indexed only as the parser tokenizes them (the first label of a host is found as a prefix of the host token, `example` in `budget.example.org` is not), and compound words are not decomposed (a compound is found by its first part as a prefix in titles, filenames, tags, notes and URLs). The web feed shows `matched_fields` as a muted "Gefunden in: …" line (`match-reasons.ts`).

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
Downloads (`/content` for originals, `/snapshot` for fetched pages) check SHA-256 and
size before response, use the same verified file handle, force attachment with
`application/octet-stream`, `nosniff` and a CSP sandbox, and close it even on
disconnect; fetched HTML is never rendered inline from the API origin. Storage errors yield
a generic 503; neither original bytes nor filenames enter logs.

### Thumbnails

`GET /api/v1/items/{id}/thumbnail` is the only inline image response. It is
authenticated (token or session), owner-scoped and returns `404` with
`Cache-Control: no-store` for trashed, foreign, missing, damaged or tampered
thumbnails, so a client shows its fallback. The body always comes from the
derived `thumbnail` blob. On every request, a `304` included, the object is read
through the integrity-checked handle (SHA-256 and size) and its WebP structure is
validated from the headers: one still image (`VP8 `, `VP8L`, or `VP8X` with
optional `ALPH`), consistent chunk, frame and canvas headers, 1 to 512 px per
side, at most 256 KiB, and no EXIF, ICC, XMP or animation. A missing, damaged or
invalid blob is `404` even when `If-None-Match` matches. Responses carry
`image/webp`, `nosniff`, a
`default-src 'none'; sandbox` CSP, `Cross-Origin-Resource-Policy: same-origin`,
`Cache-Control: private, no-cache` and a content-hash `ETag` (`304` on a matching
`If-None-Match`). Item details, listings and search results carry a boolean
`thumbnail_available` (ADR-0023), computed in the same owner-scoped query by an
`EXISTS` over the item's `thumbnail` blob row (indexed by owner and item; no extra
request per card, also for search pages). It applies the endpoint's row checks (role,
`image/webp`, at most 256 KiB) but not the object, integrity or trash checks, so it
is a hint: it can be `true` for a trashed item or a missing object, and clients must
still handle `404`. The `thumbnail` processing run tells more (`skipped` runs carry
`metadata.reason`, which the API does not expose). Rendering limits and the child process
are described in ADR-0023; neither the original nor a stored thumbnail is ever
decoded in the API process. That a thumbnail's pixels decode is established when it
is written: the renderer decodes its own output in the child, and `bag import`
checks every imported thumbnail in limited child processes and leaves out invalid
ones (`invalid_thumbnails` in the counts).
A rerun replaces the row; the previous object stays until `bag gc`.

Web previews (`apps/web/src/thumbnail.tsx`): image cards in the feed and search
results, and the item detail, show `<img src="/api/v1/items/{id}/thumbnail">` with
`loading="lazy"`; the session cookie authenticates it, nothing else is ever an image
source (no original, no external URL, no URL token). The state comes from the summary
alone: `thumbnail_available` leads to a request; otherwise a queued or processing
image says "Vorschau wird erstellt…" and any other says "Keine Vorschau verfügbar",
both without a request. Trashed rows (decided per row from `deleted_at`, so a stale
list under a new route cannot leak a request) and non-image kinds never request one.
A failed load swaps the image for "Vorschau konnte nicht geladen werden" in that
card only; the detail view adds that the original is stored unchanged, still offers
"Original herunterladen" and the whole edit form, and reads the item's `thumbnail` run
to tell "wird erstellt", "konnte nicht erstellt werden" and "keine Vorschau". While a
detail view waits for a preview it checks again every 4 s, at most 15 times, updating
only the preview's inputs (never the form fields).
The feed refreshes itself (`feed-refresh.ts`): while the loaded rows include `queued` or
`processing` items (never in the trash) it calls the existing `GET /api/v1/items/{id}`
for up to 10 of them per round, one round at a time, first after 3 s, then at growing
intervals up to 30 s while nothing changes. It merges only `processing_status`,
`thumbnail_available`, `title` and `language` into the matching rows, so scroll
position, pages, search, filters, finished thumbnails and the capture form are
untouched. A hidden tab pauses it and a return refreshes at once; failed rounds back
off (6 s, 12 s, ...) and after 5 in a row it stops with a "Jetzt aktualisieren" hint;
answers that began before the list was replaced are discarded; items that answer 404
are dropped from polling.
Feed frames sit beside the text on every width, 96 px square on phones and
168 x 126 px from 560 px, with `object-fit: contain`; the detail shows the picture at its
own size, never enlarged, up to 512 px.

The UI is mobile-first (ADR-0024). Below 900 px the navigation is a "Menü" disclosure
button; from 900 px it is inline, with a long user name cut by an ellipsis. The entry formerly named "Tags" is "Tags & Sammlungen".
Status messages (`notice.tsx`) are success (server-confirmed saves only), warning
(unanswered saves and HTTP 5xx or 408 answers, unconfirmed files, a share no longer
open) or error (clearly refused saves such as 400 or 413, lost share files, load failures), distinguished by color, glyph and a
spoken label; errors use `role="alert"`. The filters sit in a "Filter" disclosure that
is open on wide screens or whenever a filter is active (also when one is set later), with visible labels (the dates
are "Erfasst von" / "Erfasst bis"). The native file input is visually hidden but
focusable, and its label is the touch-sized "Dateien hochladen" button; picking files
still uploads at once.

Crashes may leave unpublished `.upload-*` files or published objects without a
database reference. They are retained until `bag gc` removes them under the
capture locks; retry can reuse a published object in the meantime. Never manually
remove objects just because one request failed.

Foundational domain tables without HTTP APIs are reserved for later slices.
README documents exact startup, update and backup commands.

`scripts/e2e.sh` boots a throwaway Compose stack (own project, ports and volumes),
exercises login, capture, processing, search, download, trash, restore, tokens and
export through the web proxy, prints service logs on failure and always tears
down. CI uses actual PostgreSQL 17 for persistence, parallel text/file capture, ownership,
commit failure, bootstrap and migration round trips. Storage tests exercise atomic
publication, fsync failure, corruption, size limits and racing writers. Download
disconnects are tested. Processing tests cover transactional enqueue, enrichment of
text, files and URLs, bounded retries, backoff, lease expiry and stale results,
concurrent workers executing every job exactly once, and permanent failure on the
search vector limit. Search tests (`tests/test_search_partial.py`) cover prefixes in tags, titles, notes and
filenames (umlauts, transliteration, Cyrillic, CJK), substrings and their minimum lengths,
LIKE wildcards, whole-word before prefix before substring across page boundaries and
offsets, owner isolation, trash and every filter on partial hits, tag rename, deletion and
direct SQL changes (the head is compared with its function for every row), exclusions,
phrases and `OR`, PDF text with prefixes in other fields, invalid and one-character
queries, the SQL helpers, backfill and downgrade of `0006`, and a 4,000-item bag whose
statements must be index scans. Search tests (`tests/test_search_fields.py`) cover filename
spellings, tags added, removed, renamed and deleted (also through direct SQL, re-pointed
assignments and concurrent transactions, including opposite moves without deadlock), German and English terms, tier ordering with a keyword-stuffed
body, match reasons, owner isolation, trash, filters and pagination, a backfill across a
real downgrade/upgrade of `0005`, import, and tag changes on a near-limit vector. AI and complex extractors are not implemented.

## Unimplemented product requirements and code-review checks (2026-10-31)

The [consolidated concept](concepts/product-concept-v0.2.md) and
[revised delivery plan](spec.md#18-roadmap-and-implementation-order) are **not**
evidence of implemented features. The following are source-level observations
from the independent review, not verified runtime or device outcomes.
See [TODO](../TODO.md) for actionable fixes and acceptance tests.

- Fixed and covered by the Node-based POST test above: the worker used to join
  URL, title and text with newlines, so a link shared with a title became a
  text capture. Which fields real Android browsers and apps send is still
  unverified on a device.
- Fixed: shared files used to be staged in fixed cache keys (/shared/0,
  /shared/count) and the activate handler deleted every cache but the shell, so a
  second share or an app update could destroy an unsaved one. Batches and the
  activate handler are verified by the Node tests above, not on a device; whether
  Android/Chrome keeps the cache under storage pressure is unverified.
- Fixed: the web capture methods used to generate a new client_capture_id for
  each request. Retries of the same payload now reuse it (see above), verified
  by vitest tests against a fake idempotent server, not on a real network or
  device. Keys do not survive a page reload.
- Fixed: feed summaries used to carry no text and labelled untitled text
  captures simply "text". They now carry a server-bounded `excerpt` (see Listing
  and search), verified by Python unit tests of the excerpt function, PostgreSQL
  integration tests (paging, kind filter, search, trash, owner scoping,
  authentication, multi-megabyte originals) and vitest tests of the line selection.
  The image cards and detail view have vitest component tests (happy-dom, mocked
  fetch); the text-card two-line clamp has none and was not checked in a browser.
  There is still no image paste handler, contextual
  copy/share/pin function, or compact mobile-first Shelf surface.
- Original and fetched content are delivered as protected downloads or
  sandboxed snapshots. Images additionally get a re-encoded WebP thumbnail and
  an authenticated inline endpoint (ADR-0023). The web feed, search results and item
  detail show it (see Thumbnails; vitest component tests and Python integration tests
  for `thumbnail_available`). The layout was checked once in headless Chromium 155
  (Linux/musl) at 1280 and 390 px width against a seeded local Bag; no macOS, Android
  or other real-device check has happened. The six concept mockups are design exploration, not built UI screenshots.
- The relation table enforces owner-scoped foreign keys and directional
  uniqueness but not a symmetric, no-self related_to constraint. There is
  no relation API or UI.
- URL fetching extracts HTML title and page text, not Open Graph, JSON-LD
  or canonical identity. General field provenance and explicit user-empty
  overrides are not implemented; reprocessing NULL-only defaults may
  repopulate a user-cleared title.
- Derived book/video entities, merkliste, galleries, CalDAV/CardDAV,
  click-to-load video players, and multi-capture feed grouping are not
  built. Their proposed schemas and semantics require separate ADRs
  before implementation.

These review observations must be retested against real clients and amended
when code changes. Do not mark a product requirement "built" because it
appears in the target spec.
