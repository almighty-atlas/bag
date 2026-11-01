# ADR-0022: Trigger-maintained weighted search vector with filenames, tags and match reasons

**Status:** accepted; prefix and substring matching are added by
[ADR-0025](0025-prefix-and-substring-search.md), which leaves the whole-word search below
unchanged as its first stage
**Date:** 2026-10-31

## Context

ADR-0011 matched a generated `search_vector` over title, extracted text, URL and
note. Spec section 8 and concept v0.2 also require original filenames and assigned
tags, ranking that puts titles and names above incidental body matches, and a
structured reason for each hit. A generated column can only read its own row, while
tags live in `tag` and `item_tag`. Search is owner-scoped and must stay correct the
moment a tag is added, removed, renamed or deleted, whichever code path does it (API,
import, maintenance, a future system or AI tagger).

## Decision

This deviates from spec sections 6 and 8.1, which describe `search_vector` as a generated
column; the section 8 requirement for tag search cannot be met that way.

**Index maintenance.** Migration `0005_search_fields` turns `item.search_vector` into
an ordinary column (`ALTER COLUMN … DROP EXPRESSION`, same GIN index) that the
database maintains itself, in the transaction of the change:

- a `BEFORE INSERT OR UPDATE OF title, original_filename, user_note, source_url,
  extracted_text, language` trigger on `item` recomputes the vector;
- statement-level `AFTER INSERT`, `AFTER DELETE` and `AFTER UPDATE` triggers on
  `item_tag` and an `AFTER UPDATE` trigger on `tag` (transition tables) refresh each
  affected item once per statement; a rename only refreshes items of the renamed tag's
  own owner. The `item_tag` update trigger compares the old and new
  `(owner_id, item_id, tag_id)` triples of the statement (ignoring row identity) and
  refreshes the previous and the new item of every assignment that changed; updates of
  provenance, confidence or timestamps refresh nothing, nor does a statement that
  changes no row. Triggers on `item_tag` cannot take a column list together with
  transition tables, so the comparison does that filtering.

Every lookup of tags and items is keyed by `(owner_id, id)`. The refresh takes the item
row lock first and reads the tag list afterwards, so two concurrent tag changes on one
item queue up instead of overwriting each other. The lock is `FOR NO KEY UPDATE`: the
foreign-key checks of concurrent `item_tag` writes hold `FOR KEY SHARE` on the item
until commit, and `FOR UPDATE` deadlocks with them (reproduced by the swap test). Items
of one statement are locked in `(owner_id, item_id)` order, so statements that touch
the same items in opposite directions cannot deadlock either. The migration backfills every existing
item (trashed ones too) in the same transaction. The application never writes the
column. Exports do not contain it; imports rebuild it through the triggers.

**Fields and weights.** A title and filename, B tag names, C note and URL, D extracted
text. Titles, notes, URLs and extracted text keep the `simple` plus language
configuration of the item. Filenames and tags are not tied to the item's language: they
are indexed in `simple`, `german` and `english`, in three forms (as written, split at
separators such as `-`, `_`, `.`, spaces, camelCase and letter-digit boundaries, and
with `ä ö ü ß` transliterated to `ae oe ue ss`). `rechnung` finds
`rechnung-oktober-2026.pdf`, `mueller` and `muller` find `Müller.pdf`, `bescheid` finds
`SteuerBescheid2025.pdf`. Fields are joined with a position gap, so a phrase never spans
two fields, two tags or two configurations.

**Ranking.** Results sort by a tiered score, then newest first: an exact title or
filename (with or without extension) beats any field hit; a query word in title or
filename beats one in a tag; that beats one in note or URL; the weighted `ts_rank`
(normalized 0 to 1) orders results inside a tier. Each tier is one match of the stored
vector against the query restricted to a single weight, which is cheap; `ts_rank_cd` was dropped for
the in-tier order because it cost up to 20 times more on long documents. The query is
parsed once per request. Hyphenated, underscored and dotted words are also tried split
(`kaffee-sorten` finds `Kaffee_Sorten.pdf`).

**Match reasons.** Each search result carries `matched_fields`, a list of `title`,
`filename`, `tag`, `note`, `url` and `content` in that fixed order. A field is listed
only if its own text contains at least one positive query word, determined after paging
for the rows of the page only, by testing the field's own vector against the query
without negated terms. `content` tests the stored vector's weight-D lexemes, so a
document of up to 1 MiB is not parsed again. The list is empty for results found by
exclusion only (`-word`). The snippet is unchanged and does not cover filenames or tags.

**Oversized vectors.** PostgreSQL rejects a tsvector above 1 MiB. New extracted text
that does not fit still fails the processor permanently (unchanged). A change that does
not touch the extracted text, such as a tag assignment, edit or rename, never fails for
that reason: the vector is then built without the extracted text, so the item stays
findable by everything else. A later reprocess or tag removal does not need special
handling because the next full recomputation restores it when it fits.

## Consequences

Tag and filename changes cost one re-tokenization per affected item inside the
request: about 2.5 ms for a typical 5 KB text and proportionally more for large
documents. Renaming or deleting a tag used by thousands of large items can take
seconds; those two requests therefore run with a 120 s statement timeout instead of 10 s.
Downgrading drops all triggers and functions, restores a generated column and rewrites
the table. Direct SQL that re-points an assignment (`item_tag.item_id` or `tag_id`) is
tracked like insert and delete; `TRUNCATE` of `item_tag` or `tag` is not (only tests do
it). URL host and path words are still indexed only as
the parser tokenizes them (`example.org` is one token; the word `budget` in
`budget.example.org` is not separately searchable). Negation across the three
configurations is as loose as in ADR-0011: `-word` can still return an item whose
other-language stem differs. The mixed German and English query configuration means a
stem can occasionally match in the wrong language. Compound words are not decomposed
(`Bücherregal` is found by `Bücher` since ADR-0025 only as a prefix in titles, filenames, tags, notes and
URLs, not in extracted text, and `rank` is shifted by one for whole-word results). Semantic or typo-tolerant search is not part
of this change and can merge into the same response shape later.
