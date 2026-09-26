# Data model as built

Migration `0001_foundation` creates the tables below. All have server-generated
UUIDv7 primary keys. Timestamps use `timestamptz`; API connections use UTC. `user`
is the ownership root; every other table has `owner_id`.

| Table | Purpose and constraints |
| --- | --- |
| `user` | Ownership root: ID, display name, creation time |
| `api_token` | Owner, name, unique SHA-256 token hash, creation/use/revocation times |
| `item` | Original content, source, kind, note, capture key, hash, status, timestamps, deletion marker and reserved extraction fields |
| `blob` | Item, role, SHA-256, size, MIME and hash-derived path; filesystem originals |
| `processing_run` | Unique owner/item/processor, status, attempts, error and run times |
| `tag` | Owner-scoped unique tag name |
| `item_tag` | Unique owner/item/tag assignment, provenance and confidence |
| `collection` | Owner-scoped unique collection name |
| `item_collection` | Unique owner/item/collection membership |
| `relation` | Owner-scoped item pair and relation kind, provenance, confidence, metadata |

Composite `(owner_id, id)` keys and matching foreign keys prevent cross-owner
associations. Tag provenance lives on each assignment, allowing user/system/AI
assignments of the same vocabulary without confusing their origins.

`item.content` preserves original text separately from `extracted_text`. Whitespace,
newlines and Unicode are not normalized. NUL and invalid Unicode scalar values are
rejected because PostgreSQL UTF-8 text cannot represent them. `content_hash` is
SHA-256 of the original UTF-8 bytes. A new capture of equal text creates another
item and a system `duplicate_of` relation to the first live match.

File capture uses the existing schema without a new migration. `item.content` is
NULL, `original_filename` keeps the untrusted display name, `content_hash` hashes
the exact bytes, and one `blob` row has `role = original`. MIME derives from content
signatures; recognized images/PDFs use `kind = image/document`, others use `file`.
Multiple owner-scoped blob rows may reference one immutable physical object. Access
always requires an owned, live item; knowing a hash is not sufficient to download it.
Integrity failures do not replace or erase originals. The item API now includes
nullable `original_filename`, including NULL for existing text captures.

`(owner_id, client_capture_id)` is unique; multiple NULL keys are permitted. Replays
keep original content, notes and timestamps, including for trashed items. They never
restore or recreate an item. `captured_at` defaults to insertion time; supplied
values require a timezone. `created_at` is server-set. Future mutation endpoints
must maintain `updated_at`; none exist yet.

The generated GIN-indexed `search_vector` combines simple and German/English text
configurations over title, extracted text, URL and note. Extraction/index jobs and
search endpoints are pending. Capture does not populate derived extraction fields.

No deletion API exists yet. GET hides rows with `deleted_at` set. Foreign keys do
not cascade deletion: a future explicit purge must remove references in order and
collect shared storage only after the last live or trashed reference is gone.

`bag migrate` runs packaged migration assets, including in installed wheels. The
initial downgrade drops Bag tables and is destructive; use only on disposable test
databases. Back up real data before future migrations.
