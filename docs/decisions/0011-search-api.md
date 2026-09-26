# ADR-0011: Search across all text configurations with plain-text snippets

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 8 requires ranked lexical search with snippets over title, extracted
text, URL and note, filters, and queries that run against the simple and the
language configuration. Items may have no detected language, and clients must
render snippets safely.

## Decision

`GET /api/v1/search` parses `q` with `websearch_to_tsquery` in the `simple`,
`german` and `english` configurations and matches the generated vector against
their OR-combination. Results rank by `ts_rank_cd`, then newest ID, and page by a
bounded offset. Snippets come from `ts_headline` over note, URL and extracted text
using the item's own configuration, with `«` and `»` as match markers instead of
HTML tags. `GET /api/v1/items` pages by UUIDv7 keyset (`cursor` = last ID) in
descending order. Both accept `kind`, `status`, `from`, `to` (capture time) and
`trashed`; tag and collection filters wait for their APIs. Listings and results
return summaries without `content` or `extracted_text`.

## Consequences

A German query stems German items and matches exact tokens elsewhere without the
client choosing a language. Recall is broad; a word that only stems in the wrong
language can produce an occasional extra hit. Snippets are plain text: clients must
escape them and may style the markers. Offset pagination for search is bounded to
1000 rows; deep browsing should use the listing. Semantic results can later be merged
into the same response shape without changing the contract.
