# ADR-0024: Prefix and substring search with a head vector and a staged query

**Status:** accepted; extends [ADR-0022](0022-search-fields-and-match-reasons.md)
**Date:** 2026-11-01

## Context

Search matched whole words only (stemmed): the tag `Urlaubstest` was found by `urlaubstest`
but not by `urlaub`, and the file `IMG_3109.webp` by `IMG_3109` but not by `31`. For a personal
Bag the start of a word, or a piece of a filename, is often all the user remembers. The
existing tests and ranking (ADR-0022) must keep working, owner scoping and the trash filter
stay as they are, and a query must not turn into an unbounded scan: `ILIKE '%…%'` over the
table, or a prefix that makes the GIN index visit and recheck every large document.

Measured on a synthetic Bag of 30,000 items (about 10 KB of extracted text per item, 300 tags,
PostgreSQL 17, a loaded single core, so read the figures as ratios):

| Candidate lookup | `rech` | `da` | `31` | `urlaub` |
| --- | --- | --- | --- | --- |
| prefix `:*` on the large `search_vector` | 701 ms (seq. scan) | 707 ms | 700 ms | 428 ms |
| prefix `:*` on the small head vector (below) | 5 ms | 6 ms | 3 ms | 5 ms |

A prefix scan of a GIN index visits every lexeme with that start; weight restrictions and the
`:*` flag make the index lossy, so each candidate's whole vector is fetched and rechecked.
On the head vector that is a few hundred bytes per row instead of a PDF's text layer.

A single statement `whole-word OR prefix OR substring` was prototyped first. The planner built a
bitmap OR, but had to repeat the full condition as a filter, which tested the large vectors of
every candidate again: common-word queries became 1.5 to 1.8 times slower and `IMG_3109`
went from 28 ms to 186 ms, although nothing about those queries had changed.

## Decision

**Head vector.** Migration `0006_search_partial` adds `item.search_head`, a second `tsvector`
with the title and filename (A), tag (B), note and URL (C) lexemes of `search_vector` and no
extracted text, built by `bag_head_vector`, and a GIN index on it. It is maintained exactly like
`search_vector`: the same trigger functions of ADR-0022 write both in the same statement (the
`BEFORE` trigger on `item`, `bag_refresh_item_search` for tag changes and renames). The
migration backfills every item, trashed ones too.

**Filename trigram index.** `bag_filename_key(original_filename)` is the lowercased name, a
newline and the lowercased name with `ä ö ü ß` transliterated; a GIN `gin_trgm_ops` index
(`pg_trgm`, created by the migration) over that expression supports `LIKE '%…%'`. Substring
queries are only ever issued for terms of at least three characters (a shorter pattern has no
trigram, which makes PostgreSQL read the whole index) and with `\`, `%` and `_` escaped.

**Staged search.** `search_items` runs the whole-word statement of ADR-0022 unchanged (stage 1).
Only when it does not fill the page does it run stage 2 for prefix and substring hits, so
common queries pay nothing and whole-word hits always precede partial ones, also across pages.
If the offset lies beyond the whole-word hits, a bounded count (`LIMIT offset`) tells where stage 2
continues. Stage 2 excludes whole-word hits by a short list (`NOT IN` over stage 1's candidates,
at most about `offset + limit` rows because stage 2 only runs when they ran out), not by testing
every candidate's large vector again.

**Prefix rules (stage 2).**

- A word of at least **2 characters** also matches longer words that start with it in title,
  filename, tag, note and URL. One character matches whole words only (as before).
- The prefix applies to the word as typed (the `simple` parse), never to its stem: `uebers`
  stems to `ueb` in German, and `bücher` to `buch`, which would match `Buchhaltung`. Stemmed
  whole-word matching (`Rechnungen` finds `Rechnung`) is unchanged and combines with prefixes in one query.
- Extracted text matches whole words only. A prefix there would visit and rank far too much
  text (table above); `rechnungs` does not find a PDF containing `Rechnungsnummer`.
- Exclusions stay exact: `foto -urlaub` removes items that contain the word `urlaub` in any
  field, including the extracted text, not words that start with it.
- Words of one query may be found in different fields of one item, and a word may be a whole
  word of the text while another is a prefix of the tag (`rechnungsnummer telek`). Queries
  with a single plain word and no operators use the head vector alone; every other query
  looks up any word in the head index and lets the large vector verify the whole query, so the
  large vector is read only for rows the small index found.
- Phrases (`"foto urlaubs"`), `OR` and `-` work with prefixes; they get no substring matching.

**Substring rules (stage 2).** Plain word queries of at most five words (no quotes, `OR` or
`-`) are also searched as substrings of the filename if any word has at least **3 characters**;
all words must occur in the same filename. So `109` and `mg_31` find `IMG_3109.webp`, `abrech`
finds `Jahresabrechnungsentwurf.pdf`, `üller` and `ueller` find `Müller.pdf`. `31` finds
`IMG_3109.webp` through the prefix rule (`3109` starts with `31`); `09` finds nothing. Titles,
tags, notes and URLs have prefix matching but no substring matching.

**Ranking.** Stage 1 is ADR-0022's tiered score. Stage 2 orders prefix hits by the same field
tiers (title/filename, then tag, then note/URL; read from the head vector, as is the rank inside a tier),
then hits found only as a filename substring, then newest first. `rank` is `1 + score` for
whole-word results and below 1 for partial ones, so it never increases along a result list.

**Match reasons and snippet.** `matched_fields` keeps its meaning: a field is listed if its own
text contains a query word, here as a whole word for stage 1 results and as a prefix, or for
`filename` a substring, for stage 2 results; `content` always means a whole word of the text.
Snippets highlight prefix hits in note, URL and text; filenames and tags are still not part
of the snippet (reasons only).

## Consequences

Measured on the same 30,000 items: stage 1 queries are unchanged (`rechnung` 1,388 ms against
1,396 ms before, `IMG_3109` unchanged by design apart from stage 2 running when fewer than a
page matched: 28 to 46 ms). Partial hits cost 40 to 140 ms (`rech` 89 ms, `abrech` 76 ms, `da`
138 ms, `109` 121 ms, `mg_310` 47 ms, `rechnung telek` 68 ms); all candidate lookups are bitmap
index scans (checked by test with sequential scans disabled, and by `EXPLAIN` at this size).
A prefix that many items share (a very common title word, or `da`) is bounded by the number of
title, filename, tag, note and URL hits, not by the size of the texts. Whole-word queries of very common words
still rank every match (about 1.4 s for roughly 12,000 matches here); that is old behavior and
tracked in TODO.md.

The head vector and the two indexes add 6 MB, 3.6 MB and 2.3 MB at 30,000 items (the large vector and its
index: 213 MB and 36 MB). Computing the head costs about 0.35 ms per item write, against about
2.8 ms for the large vector at 10 KB of text. The migration took 63 s here (backfill and both
index builds, in one transaction that rewrites `item`); run it like `0005` with API and worker idle, then `VACUUM
ANALYZE item`. It needs `pg_trgm` (a trusted extension; the `postgres:17` image ships it and its superuser
owns the database; elsewhere the migration user needs the `CREATE` privilege on the database). The downgrade restores
the functions of 0005, drops the column, indexes and new functions and leaves the extension installed.
Case folding in substrings and prefixes follows the database locale (a UTF-8 locale that lower-cases
non-ASCII letters, as before).

Limits: no substring inside titles, tags, notes, URLs or extracted text; below three characters
only the start of a word matches in filenames, tags, titles, notes and URLs (`09` does not find
`IMG_3109.webp`); one-character words match whole words only; the host `budget.example.org` is
found by `budget` (a prefix of the whole host token) but not by `example`; accents other than `ä ö ü ß` are not folded
(`cafe` does not find `Café`); the query side still does not fold `ae/oe/ue` for stored
transliterated names in the substring path (`müller` does not find `mueller.pdf`; the reverse works);
a query word and a stem can still collide (`urlaubst` stems to `urlaub` and finds it as a whole word, which is
existing behavior); a word that is a stopword in one configuration only is searched as typed.
Search is lexical; typo tolerance and semantic matching remain out of scope.
