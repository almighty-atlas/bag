import re
from datetime import datetime
from typing import Any
from uuid import UUID

from bag.config import Settings
from bag.db import connection
from bag.excerpt import SOURCE_CHARS, make_excerpt
from bag.organize import COLLECTIONS_SQL, TAGS_SQL
from bag.preview import THUMBNAIL_AVAILABLE_SQL
from bag.schemas import ItemPage, ItemSummary, SearchPage, SearchResult

SUMMARY_COLUMNS = (
    "id, kind, source, title, user_note, mime_type, original_filename, source_url, language, "
    f"processing_status, created_at, captured_at, updated_at, deleted_at, {TAGS_SQL}, "
    f"{COLLECTIONS_SQL}, {THUMBNAIL_AVAILABLE_SQL}, "
    # Only a bounded prefix of text originals leaves the database; see bag.excerpt.
    f"CASE WHEN kind = 'text' THEN substr(content, 1, {SOURCE_CHARS}) END AS excerpt_source"
)
NAMED_FILTER = {
    "tag": (
        "EXISTS (SELECT 1 FROM item_tag l JOIN tag n ON (n.owner_id = l.owner_id "
        "AND n.id = l.tag_id) WHERE l.owner_id = item.owner_id AND l.item_id = item.id "
        "AND n.name = %(tag)s)"
    ),
    "collection": (
        "EXISTS (SELECT 1 FROM item_collection l JOIN collection n ON (n.owner_id = l.owner_id "
        "AND n.id = l.collection_id) WHERE l.owner_id = item.owner_id AND l.item_id = item.id "
        "AND n.name = %(collection)s)"
    ),
}
# Query every configuration: a German query stems for German items, English for
# English ones, and simple matches exact tokens in items without a language. Hyphenated,
# underscored and dotted words are also tried split into their parts, because filenames and
# tags are indexed split ('kaffee-sorten' finds 'Kaffee_Sorten.pdf').
QUERY_SQL = (
    "(websearch_to_tsquery('simple', %(q)s) || websearch_to_tsquery('german', %(q)s) "
    "|| websearch_to_tsquery('english', %(q)s) "
    "|| websearch_to_tsquery('simple', %(q_split)s) "
    "|| websearch_to_tsquery('german', %(q_split)s) "
    "|| websearch_to_tsquery('english', %(q_split)s))"
)
# A joining separator sits between two letters or digits; a leading '-' stays an exclusion.
_JOINERS = re.compile(r"(?<=[^\W_])[-_.](?=[^\W_])")
HEADLINE_TEXT = (
    "coalesce(title, '') || E'\\n' || coalesce(user_note, '') || E'\\n' || "
    "coalesce(source_url, '') || E'\\n' || coalesce(extracted_text, '')"
)
HEADLINE_OPTIONS = "StartSel=«, StopSel=», MaxFragments=2, MaxWords=18, MinWords=6"
# The query is parsed once per request. any_q holds the positive words as alternatives; the
# single-weight variants match only lexemes of one weight (A title and filename, B tags,
# C note and URL, D extracted text; see migration 0005).
QUERIES_CTE = (
    "q AS MATERIALIZED (SELECT full_q, any_q, bag_restrict_query(any_q, 'A') AS a_q, "
    "bag_restrict_query(any_q, 'B') AS b_q, bag_restrict_query(any_q, 'C') AS c_q, "
    "bag_restrict_query(any_q, 'D') AS d_q FROM ("
    f"SELECT full_q, bag_any_term_query(full_q) AS any_q FROM (SELECT {QUERY_SQL} AS full_q) f"
    ") a)"
)
# Relevance is tiered, so a keyword-stuffed body never outranks a title or name hit: an exact
# title or filename (with or without its extension) beats any field hit, then a query word in
# title or filename beats one in a tag, which beats one in a note or URL. Within a tier the
# weighted term-frequency rank (0 to 1, damped by document length) orders results; the newest
# item breaks remaining ties. ts_rank_cd would add proximity, but costs up to 20 times more on
# long documents and the request has a statement timeout.
EXACT_SQL = (
    "lower(btrim(%(q)s)) IN (lower(btrim(coalesce(item.title, ''))), "
    "lower(btrim(coalesce(item.original_filename, ''))), "
    "lower(regexp_replace(coalesce(item.original_filename, ''), '\\.[^.]+$', '')))"
)
SCORE_SQL = (
    f"(CASE WHEN {EXACT_SQL} THEN 8 ELSE 0 END "
    "+ CASE WHEN item.search_vector @@ q.a_q THEN 4 ELSE 0 END "
    "+ CASE WHEN item.search_vector @@ q.b_q THEN 2 ELSE 0 END "
    "+ CASE WHEN item.search_vector @@ q.c_q THEN 1 ELSE 0 END "
    "+ ts_rank(item.search_vector, q.full_q, 33))"
)
# Fields holding at least one positive query word, checked for the result page only.
# Extracted text is checked on the stored vector (weight D) instead of being parsed again.
_CONFIG = "bag_language_config(item.language)"
_FIELD_HITS = (
    ("title", f"bag_text_vector(item.title, {_CONFIG}, 'A') @@ q.any_q"),
    ("filename", "bag_name_vector(item.original_filename, 'A') @@ q.any_q"),
    ("tag", "bag_item_tags_vector(item.owner_id, item.id) @@ q.any_q"),
    ("note", f"bag_text_vector(item.user_note, {_CONFIG}, 'C') @@ q.any_q"),
    ("url", f"bag_text_vector(item.source_url, {_CONFIG}, 'C') @@ q.any_q"),
    ("content", "item.search_vector @@ q.d_q"),
)
MATCHED_FIELDS_SQL = (
    "ARRAY(SELECT f.name FROM (VALUES "
    + ", ".join(f"({i}, '{name}', {hit})" for i, (name, hit) in enumerate(_FIELD_HITS))
    + ") AS f(position, name, hit) WHERE coalesce(f.hit, false) ORDER BY f.position)"
)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    source = row.pop("excerpt_source")
    return {**row, "excerpt": make_excerpt(source)}


def _filters(
    owner_id: UUID,
    *,
    kind: str | None,
    status: str | None,
    captured_from: datetime | None,
    captured_to: datetime | None,
    trashed: bool,
    tag: str | None = None,
    collection: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses = ["item.owner_id = %(owner_id)s"]
    params: dict[str, Any] = {"owner_id": owner_id}
    clauses.append("deleted_at IS NOT NULL" if trashed else "deleted_at IS NULL")
    for name, value in (("tag", tag), ("collection", collection)):
        if value is not None:
            clauses.append(NAMED_FILTER[name])
            params[name] = value
    if kind is not None:
        clauses.append("kind = %(kind)s")
        params["kind"] = kind
    if status is not None:
        clauses.append("processing_status = %(status)s")
        params["status"] = status
    if captured_from is not None:
        clauses.append("captured_at >= %(captured_from)s")
        params["captured_from"] = captured_from
    if captured_to is not None:
        clauses.append("captured_at < %(captured_to)s")
        params["captured_to"] = captured_to
    return clauses, params


def list_items(
    settings: Settings,
    owner_id: UUID,
    *,
    limit: int,
    cursor: UUID | None,
    kind: str | None = None,
    status: str | None = None,
    captured_from: datetime | None = None,
    captured_to: datetime | None = None,
    trashed: bool = False,
    tag: str | None = None,
    collection: str | None = None,
) -> ItemPage:
    clauses, params = _filters(
        owner_id,
        kind=kind,
        status=status,
        captured_from=captured_from,
        captured_to=captured_to,
        trashed=trashed,
        tag=tag,
        collection=collection,
    )
    if cursor is not None:
        # UUIDv7 keys are time-ordered, so keyset pagination follows capture order.
        clauses.append("id < %(cursor)s")
        params["cursor"] = cursor
    params["limit"] = limit + 1
    with connection(settings) as conn:
        rows = conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM item WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
    items = [ItemSummary.model_validate(_summary(row)) for row in rows[:limit]]
    next_cursor = items[-1].id if len(rows) > limit else None
    return ItemPage(items=items, next_cursor=next_cursor)


def search_items(
    settings: Settings,
    owner_id: UUID,
    *,
    q: str,
    limit: int,
    offset: int,
    kind: str | None = None,
    status: str | None = None,
    captured_from: datetime | None = None,
    captured_to: datetime | None = None,
    trashed: bool = False,
    tag: str | None = None,
    collection: str | None = None,
) -> SearchPage:
    clauses, params = _filters(
        owner_id,
        kind=kind,
        status=status,
        captured_from=captured_from,
        captured_to=captured_to,
        trashed=trashed,
        tag=tag,
        collection=collection,
    )
    clauses.append("item.search_vector @@ q.full_q")
    params.update(
        {
            "q": q,
            "q_split": _JOINERS.sub(" ", q),
            "limit": limit + 1,
            "offset": offset,
            "options": HEADLINE_OPTIONS,
        }
    )
    # Rank and page first, then build snippets and match reasons for the page's rows only.
    with connection(settings) as conn:
        rows = conn.execute(
            f"WITH {QUERIES_CTE}, hit AS (SELECT item.id AS hit_id, {SCORE_SQL} AS hit_rank "
            f"FROM item CROSS JOIN q WHERE {' AND '.join(clauses)} "
            "ORDER BY hit_rank DESC, item.id DESC LIMIT %(limit)s OFFSET %(offset)s) "
            f"SELECT {SUMMARY_COLUMNS}, hit.hit_rank AS rank, "
            f"ts_headline(CASE language WHEN 'de' THEN 'german'::regconfig "
            f"WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END, "
            f"{HEADLINE_TEXT}, q.full_q, %(options)s) AS snippet, "
            f"{MATCHED_FIELDS_SQL} AS matched_fields "
            "FROM hit JOIN item ON (item.owner_id = %(owner_id)s AND item.id = hit.hit_id) "
            "CROSS JOIN q ORDER BY hit.hit_rank DESC, item.id DESC",
            params,
        ).fetchall()
    results = [SearchResult.model_validate(_summary(row)) for row in rows[:limit]]
    return SearchPage(
        results=results,
        next_offset=offset + limit if len(rows) > limit else None,
    )
