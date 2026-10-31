# ADR-0021: Bounded text excerpt in item summaries

**Status:** accepted
**Date:** 2026-10-31

## Context

Feed cards labelled text captures without title or note simply "text", because item
summaries carried no content (concept v0.2 §16.8 asks for meaningful previews).
Clients must not fetch every item separately or receive complete large texts
(`item.content` may be up to the request limit, and database rows can be edited to be
larger).

## Decision

`ItemSummary` (and therefore search results) gains a nullable `excerpt`, computed on
the server for `kind = text` only and `null` for every other kind. No migration: the
query selects `substr(content, 1, 4096)` and `bag.excerpt.make_excerpt` turns that
prefix into plain text of at most 240 characters. It collapses whitespace and line
breaks to single spaces, drops invisible and bidirectional control characters, removes
common Markdown markers (headings, quotes, list bullets, fences, rules, emphasis, code
spans, link and image syntax) while keeping their words, and cuts on a word or
grapheme-cluster boundary with a trailing `…` when the original continues. Nothing is
rendered as markup. The original `content` is never modified. The excerpt comes from
`content`, not `extracted_text`, so it exists as soon as the capture is acknowledged.

The web feed chooses the lines: title first, otherwise the excerpt (text items), then
file name, link, note and finally the kind. A note or excerpt that is not the headline
stays visible below it; search results keep showing the match snippet.

## Consequences

Listings read at most 4 KiB of text per item with no extra requests and no schema
change. Excerpts are subject to the same owner scoping, authentication and trash
filters as the rest of the summary. Source prefixes that consist of more than about
4096 characters of leading whitespace or markup yield no excerpt. Grapheme handling is
a conservative approximation (combining marks, joiners, variation selectors, emoji
modifiers, flag pairs) rather than a full Unicode segmentation. Other kinds (links,
documents) keep their current labels; previews for them belong to the later
card/preview work.
