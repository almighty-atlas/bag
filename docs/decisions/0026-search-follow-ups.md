# ADR-0026: Search follow-ups: URL words, accent folding, name substrings, bounded ranking

**Status:** accepted; extends [ADR-0022](0022-search-fields-and-match-reasons.md) and
[ADR-0025](0025-prefix-and-substring-search.md)
**Date:** 2026-11-21

## Context

ADR-0025 left a list of known gaps, tracked in TODO.md:

- `example` in `https://budget.example.org/x-y` was not searchable; the parser keeps a URL as
  one URL, host and path token.
- `cafe` did not find `Café`; only `ä ö ü ß` were transliterated, and only in filenames and tags.
- The substring path did not fold the query: `müller` did not find a stored `mueller.pdf`.
- Compound words were only found by their first part (`Bücherregal` by `bücher`, not `regal`).
- Filename and tag hits never showed in the snippet, only in `matched_fields`.
- A whole-word query for a very common word ranked every match (about 1.4 s for 12,000
  matches of 10 KB text in ADR-0025's measurement).
- It was unknown whether renaming a tag on thousands of large items fits into a request.

`scripts/search_bench.py` seeds a synthetic Bag (30,000 items of about 10 KB text, files,
texts and URLs, 300 tags, one tag on 5,000 items) and times searches and a tag rename. Figures
below are from that script on one development host with PostgreSQL 17 in Docker; read them as
ratios.

## Decision

Migration `0007_search_followups` and `bag.search`:

**URL words.** The URL vector (`bag_url_vector`, weight C) also holds every word of host, path and
query, without the scheme and a leading `www.`: `example`, `org`, `x`, `y` and `budget` are whole
words, as are `italien` and `2026` in `/reisen/italien-2026`. The URL, host and path tokens of the
parser stay, so `budget.example.org` and its prefixes still match. Scheme words (`https`) are not
indexed. Percent-encoded words are indexed as encoded.

**Accent folding.** `bag_fold` (the trusted `unaccent` extension with its default dictionary,
wrapped as `IMMUTABLE` because the stored vectors and the trigram index depend on it) adds the
accent-free form of every word of titles, notes, URLs (`bag_field_vector`), filenames and tags
(`bag_name_text`) when it differs. Only new lexemes are added, so term frequencies, and with
them the ranking of unaccented text, stay the same. The query is also parsed folded, so `café`
and `cafe` find `Café` and `Cafe`. Extracted text is **not** folded: a second parse of every
document would cost about as much as the first and enlarge the large vector; `cafe` does not find
a PDF that says `Café`.

**Name substrings.** `item.search_names` holds one key per name of the item (filename, title,
each tag), each the lowercased folded name and, after a newline, its folded German
transliteration (`bag_name_key`). It is written by the same triggers as the vectors. A trigram
index `item_names_trgm_idx` over it replaces the filename-only `item_filename_trgm_idx`. The query
word is compared in the second form (`bag_like_key`: lowercased, transliterated, folded), so
`müller`, `mueller` and `muller` find `Müller.pdf`, `müller` finds `mueller.pdf`, `regal` finds the
tag `Bücherregal` and `markt` the title `Wochenmarkt`. Rules of ADR-0025 stay: plain word queries
of at most five words, at least one word of three characters, `\ % _` escaped; all words must occur
in the names of one item, now possibly in different names of it. Notes, URLs and extracted text get
no substring matching: notes can be long and are prefix-matched, URLs now have their words, and
the text would need a trigram index over megabytes.

**Snippet for name hits.** When the `ts_headline` snippet highlights nothing and the result
matched in its title, filename or tags, the API returns those names with the query words marked
(`IMG_3«109».webp`, `Bücher«regal»`), folded like the search (`bag.search.highlight`). Several names
are joined with ` · `.

**Bounded ranking.** The score tiers (exact name, title/filename, tag, note/URL) are read from the
small head vector instead of the large one. Within each tier only the newest `RANK_CAP` (500)
matches are ranked by `ts_rank` on the large vector; older matches of that tier follow newest
first with no frequency rank. Whole-word `rank` stays `1 + score`, partial ranks stay below 1, and
ranks never increase along a result list.

**Tag changes.** `search_vector` now stores the extracted text (D) first and the short fields
after it, so the text's positions never depend on the other fields. After a tag change
`bag_refresh_item_search` keeps the stored text part (`ts_filter(search_vector, '{d}')`) and joins
the new head to it (`bag_join_vectors`), which equals a full rebuild (tested for every row) without
parsing the text again. If the stored vector has no text part although the item has text (it was
too large before), the text is parsed again, so removing tags can restore it. Rename and delete
stay synchronous in one transaction under the existing 120 s `REINDEX_TIMEOUT`.

## Consequences

Measured with `scripts/search_bench.py` (30,000 items, before → after):

| Query | 0006 | 0007 |
| --- | --- | --- |
| `rechnung` (12,000 text matches) | 283 ms | 94 ms |
| `projekt` / `invoice` | 167 / 128 ms | 85 / 80 ms |
| `rech`, `da`, `31`, `109` (partial) | 54–67 ms | 78–90 ms |
| `IMG_3109`, `mg_310`, no-hit queries | 10–12 ms | 13–18 ms |
| `example` | 0 hits | 50 hits, 94 ms |
| `regal` | 0 hits | 4 hits, 21 ms |

Partial queries pay about 20 ms more for the folded field checks of the match reasons on the
result page; most of their time is the `ts_headline` snippet, as before. Common whole-word queries
are about three times faster, and the ranking work is now bounded by `RANK_CAP` per tier instead of
the number of matches. The cost of the cap: among more than 500 text-only matches, an older but
more relevant document can follow newer ones.

Renaming or deleting a tag carried by 5,000 items of 10 KB took 14.6 s with 0006 and 14.8 s with
0007. Rebuilding the vector itself is 15 times cheaper now (0.13 against 1.96 ms per item without
indexes), but maintaining the GIN index of the large vector dominates (about 70 % of the time per
item). That is about 3 ms per item, so the 120 s timeout covers a tag on roughly 40,000 such items,
and the nginx proxy waits 120 s as well. A background re-index job is not needed at that scale. It
would make search inconsistent while it runs, and the job table is per item and processor, which
shows in `processing_status`. Revisit it if a real Bag gets near that size. The test suite checks
that every vector equals a full rebuild after tag changes, renames and deletes.

Storage at 30,000 items: `search_names` 0.9 MB, its trigram index 5.3 MB (the filename index it
replaces was 2.3 MB at ADR-0025's size). The migration rebuilds `search_vector`, `search_head` and
`search_names` of every item in one transaction (1 min 47 s here, about 3.5 ms per item) and
rewrites the table. Procedure as for 0006: back up, stop or idle API and worker, `bag migrate`,
`VACUUM ANALYZE item`, start the new containers. It needs `unaccent` (trusted, shipped with
`postgres:17`) next to `pg_trgm`. The downgrade restores the functions of 0006, rebuilds every
vector with them, drops `search_names` and its index, recreates `item_filename_trgm_idx`, and
leaves `unaccent` installed.

Still not covered: substrings in notes, URLs and extracted text; folding of extracted text;
decomposition of compounds in the text (`regal` does not find `Bücherregal` inside a PDF); typo
tolerance and semantic matching.
