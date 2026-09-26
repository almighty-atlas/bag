# Data model as built

Migration `0001_foundation` creates the tables below; `0002_jobs` adds `job` and
`0003_active_job` its unique active-job index. All
have server-generated UUIDv7 primary keys. Timestamps use `timestamptz`; API
connections use UTC. `user` is the ownership root; every other table has `owner_id`.

| Table | Purpose and constraints |
| --- | --- |
| `user` | Ownership root: ID, display name, creation time |
| `api_token` | Owner, name, unique SHA-256 token hash, creation/use/revocation times |
| `item` | Original content, source, kind, note, capture key, hash, status, timestamps, deletion marker and extraction fields |
| `blob` | Item, role, SHA-256, size, MIME and hash-derived path; filesystem originals |
| `processing_run` | Unique owner/item/processor, status, attempts, error and run times |
| `job` | Queue entry per owner/item/processor: status, attempts, limit, `run_after`, lease expiry and worker ID |
| `tag` | Owner-scoped unique tag name |
| `item_tag` | Unique owner/item/tag assignment, provenance and confidence |
| `collection` | Owner-scoped unique collection name |
| `item_collection` | Unique owner/item/collection membership |
| `relation` | Owner-scoped item pair and relation kind, provenance, confidence, metadata |

Composite `(owner_id, id)` keys and matching foreign keys prevent cross-owner
associations. Tag provenance lives on each assignment, allowing user/system/AI
assignments of the same vocabulary without confusing their origins. Names are
trimmed, 1–100 characters without control characters, unique per owner and
case-sensitive; they are created on first use and never deleted yet. API edits
write `created_by = user` and replace only user assignments.

Token administration uses the existing `api_token` table, without migration. New
tokens carry UUIDv7 IDs and names of 1–200 characters without ASCII control characters.
Names need not be unique. Secrets contain 256 random bits and are stored only as
SHA-256 hashes. `last_used_at` updates during authentication; `revoked_at` records
the first revocation and is preserved on repeats. Revoked rows remain visible to
server administrators. Recovery creates another row for the same owner, including
when all old tokens are revoked; it never changes item ownership or old token hashes.

`item.content` preserves original text separately from `extracted_text`. Whitespace,
newlines and Unicode are not normalized. NUL and invalid Unicode scalar values are
rejected because PostgreSQL UTF-8 text cannot represent them. `content_hash` is
SHA-256 of the original UTF-8 bytes. A new capture of equal text creates another
item and a system `duplicate_of` relation to the first live match.

URL capture uses the existing schema: `kind = url`, `content` preserves the original
URL, and `source_url` initially contains the same unchanged string. The hash is of
the original UTF-8 bytes, not a normalized URL or fetched page. MIME stays NULL,
and no blob is created. GET includes nullable `source_url` for all kinds. Future
enrichment must not overwrite `content`. Equal hashes across capture kinds can
produce duplicate relations under the common capture contract.

File capture uses the existing schema. `item.content` is NULL, `original_filename`
keeps the untrusted display name, `content_hash` hashes the exact bytes, and one
`blob` row has `role = original`. MIME derives from content signatures at capture
and again during processing; recognized images/PDFs use `kind = image/document`,
others use `file`, including UTF-8 text files (`mime_type = text/plain`, kind `file`).
Multiple owner-scoped blob rows may reference one immutable physical object. Access
always requires an owned, live item; knowing a hash is not sufficient to download it.
Integrity failures do not replace or erase originals. The item API includes
nullable `original_filename`, including NULL for existing text captures.

`(owner_id, client_capture_id)` is unique; multiple NULL keys are permitted. Replays
keep original content, notes and timestamps, including for trashed items. They never
restore or recreate an item. `captured_at` defaults to insertion time; supplied
values require a timezone. `created_at` is server-set. Processing updates
`updated_at` whenever it changes enrichment columns or the derived status; user
edits of `title` (up to 500 characters), `user_note` and `language` do too.

## Processing rows

Capture inserts, in its own transaction, one `processing_run` (`pending`, zero
attempts) and one `job` (`queued`, `max_attempts` from `BAG_JOB_MAX_ATTEMPTS`) for
each registered processor, and sets `item.processing_status = queued`. `job` rows
are queue entries: `run_after` delays retries, `lease_expires_at` is set exactly while
`running` (a CHECK constraint enforces this) and `worker_id` names the claimant.
Partial indexes cover queued jobs by `run_after` and running jobs by lease expiry,
and a partial unique index allows at most one queued or running job per owner, item
and processor. Job rows are kept after completion for auditing; nothing deletes them yet.
Reprocessing upserts the run back to `pending` with zero attempts and adds a job
unless an active one exists.

`processing_run` is the state clients read: `status`, `attempts` (mirrors the job),
`last_error` (bounded to 500 characters, only processor-chosen messages or exception
class names), `started_at` of the latest attempt and `finished_at` of the final
outcome. A retry returns the run to `pending` with `last_error` kept and
`finished_at` cleared. `item.processing_status` is recomputed from the runs in SQL
after every claim and finalize; the CHECK values `queued`, `processing`, `ready`,
`partial` and `failed` follow spec section 7.

Processors may write only `mime_type`, `kind`, `extracted_text` and `language`,
plus a per-processor object merged into `item.metadata` (`text_extract.truncated`
and `extracted_bytes`; `language.detected` as `de`, `en` or null). They never modify
`content`, `title`, `user_note`, blobs or relations. `extracted_text` is bounded to
1 MiB; when its generated search vector still exceeds PostgreSQL's tsvector limit,
the run fails permanently and `extracted_text` stays NULL. `language` holds ISO
639-1 `de` or `en` and selects the stemming configuration of `search_vector`; the
reserved marker `metadata.language.user = true` means the value was chosen by the
user and detection must leave it alone.

Duplicate relations always target a root item, one that is not itself the source
of a `duplicate_of` relation, because `created_at` is the transaction start time
and concurrent captures could otherwise pick a duplicate as the earliest row.

Items captured before `0002_jobs` keep `processing_status = ready` and have no runs
or jobs until `bag reprocess` schedules the missing processors.

The generated GIN-indexed `search_vector` combines simple and German/English text
configurations over title, extracted text, URL and note. Search matches it against
the OR of the query parsed in all three configurations. Listing pages by `id` in
descending order, relying on the `(owner_id, id)` unique index and the UUIDv7 time
ordering; `item_recent_idx` remains for future `created_at` ordering.

Deletion is soft: `DELETE` sets `deleted_at` (kept on repeats), restore clears it,
and both bump `updated_at` only when the state changes. Detail routes hide rows
with `deleted_at` set; listings and search expose them with `trashed=true`. The
worker still processes jobs of trashed items, replays still resolve them, and
duplicate detection ignores them. Foreign keys do not cascade deletion: `bag purge`
removes jobs, runs, relations (either direction), tag and collection assignments
and blob rows before the item, after the retention period. Purged items are gone
for good, including their capture keys, so a later replay of that key creates a
new item. Physical objects outlive blob rows until `bag gc` finds no reference in
any owner's blob rows.

`bag migrate` runs packaged migration assets, including in installed wheels.
Downgrades drop tables and are destructive; use only on disposable test databases.
Back up real data before migrations. Updating to `0002_jobs` or later requires running
`bag migrate` before starting the new API and worker, which otherwise report not ready.
