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
# The words as typed, without stemming; only these get a prefix (see bag_prefix_query).
TYPED_SQL = "(websearch_to_tsquery('simple', %(q)s) || websearch_to_tsquery('simple', %(q_split)s))"
# A joining separator sits between two letters or digits; a leading '-' stays an exclusion.
_JOINERS = re.compile(r"(?<=[^\W_])[-_.](?=[^\W_])")
# Partial matching (ADR-0025), run only when whole-word hits do not fill the page. A word of at
# least MIN_PREFIX characters also matches longer words that start with it in title, filename,
# tags, note and URL, never in extracted text. In a plain word query of at most
# MAX_SUBSTRING_WORDS words, a word of at least MIN_SUBSTRING characters is also searched inside
# filenames, which a trigram index supports; shorter words cannot use it.
MIN_PREFIX = 2
MIN_SUBSTRING = 3
MAX_SUBSTRING_WORDS = 5
_LIKE_SPECIAL = re.compile(r"([\\%_])")
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


def _matched_fields_sql(words: str, filename_extra: str = "") -> str:
    """Fields whose own text contains a word of the query column `words` of the q CTE."""
    hits = (
        ("title", f"bag_text_vector(item.title, {_CONFIG}, 'A') @@ q.{words}"),
        ("filename", f"bag_name_vector(item.original_filename, 'A') @@ q.{words}{filename_extra}"),
        ("tag", f"bag_item_tags_vector(item.owner_id, item.id) @@ q.{words}"),
        ("note", f"bag_text_vector(item.user_note, {_CONFIG}, 'C') @@ q.{words}"),
        ("url", f"bag_text_vector(item.source_url, {_CONFIG}, 'C') @@ q.{words}"),
        ("content", "item.search_vector @@ q.d_q"),
    )
    return (
        "ARRAY(SELECT f.name FROM (VALUES "
        + ", ".join(f"({i}, '{name}', {hit})" for i, (name, hit) in enumerate(hits))
        + ") AS f(position, name, hit) WHERE coalesce(f.hit, false) ORDER BY f.position)"
    )


# Partial matches (migration 0006; ADR-0025). prefix_q is full_q with every positive word also a
# prefix of a title, filename, tag, note or URL word; head_q and guard_q state the prefixes for the
# small search_head vector (all words, or any word); ap_q, bp_q and cp_q are single-weight forms
# for ranking; field_q matches one field's own vector for the match reasons.
PARTIAL_QUERIES_CTE = (
    "q AS MATERIALIZED (SELECT full_q, any_q, bag_restrict_query(any_q, 'D') AS d_q, "
    f"bag_prefix_query(full_q, typed_q, {MIN_PREFIX}, 'ABC', true) AS prefix_q, "
    f"bag_prefix_query(full_q, typed_q, {MIN_PREFIX}, '', false) AS head_q, "
    f"bag_prefix_query(any_q, typed_q, {MIN_PREFIX}, '', false) AS guard_q, "
    f"bag_prefix_query(any_q, typed_q, {MIN_PREFIX}, 'A', false) AS ap_q, "
    f"bag_prefix_query(any_q, typed_q, {MIN_PREFIX}, 'B', false) AS bp_q, "
    f"bag_prefix_query(any_q, typed_q, {MIN_PREFIX}, 'C', false) AS cp_q, "
    f"bag_prefix_query(any_q, typed_q, {MIN_PREFIX}, '', false) AS field_q FROM ("
    f"SELECT full_q, typed_q, bag_any_term_query(full_q) AS any_q FROM ("
    f"SELECT {QUERY_SQL} AS full_q, {TYPED_SQL} AS typed_q) f"
    ") a)"
)
FILENAME_KEY = "bag_filename_key(item.original_filename)"


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


def _plain_words(q: str) -> list[str] | None:
    """The words of a query without operators, else None.

    Quotes, exclusions and OR change what a word means; the shortcuts and the filename index
    know nothing of that, so such queries only get the general prefix check.
    """
    words = q.split()
    if '"' in q or any(w.startswith("-") or w.lower() == "or" for w in words):
        return None
    return [w for w in words if any(c.isalnum() for c in w)]


def substring_patterns(words: list[str] | None) -> list[str]:
    """LIKE patterns for a plain word query, or none when substring matching does not apply."""
    if not words or len(words) > MAX_SUBSTRING_WORDS:
        return []
    if max(len(w) for w in words) < MIN_SUBSTRING:
        return []
    return ["%" + _LIKE_SPECIAL.sub(r"\\\1", w) + "%" for w in words]


def _result_sql(hit: str, *, queries: str, rank: str, headline_query: str, fields: str) -> str:
    # Rank and page first, then build snippets and match reasons for the page's rows only.
    return (
        f"WITH {queries}, hit AS ({hit}) "
        f"SELECT {SUMMARY_COLUMNS}, {rank} AS rank, "
        "ts_headline(CASE language WHEN 'de' THEN 'german'::regconfig "
        "WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END, "
        f"{HEADLINE_TEXT}, {headline_query}, %(options)s) AS snippet, "
        f"{fields} AS matched_fields "
        "FROM hit JOIN item ON (item.owner_id = %(owner_id)s AND item.id = hit.hit_id) "
        "CROSS JOIN q ORDER BY hit.hit_rank DESC, item.id DESC"
    )


def _whole_word_sql(clauses: list[str]) -> str:
    hit = (
        f"SELECT item.id AS hit_id, {SCORE_SQL} AS hit_rank "
        f"FROM item CROSS JOIN q WHERE {' AND '.join(clauses)} "
        "ORDER BY hit_rank DESC, item.id DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    return _result_sql(
        hit,
        queries=QUERIES_CTE,
        # Whole-word results rank 1 and above, partial ones below 1, so ranks never increase.
        rank="1 + hit.hit_rank",
        headline_query="q.full_q",
        fields=_matched_fields_sql("any_q"),
    )


def _partial_sql(
    clauses: list[str], whole: list[str], words: list[str] | None, likes: list[str]
) -> str:
    # A prefix hit is a row whose head vector has every word as a prefix, which needs nothing but
    # the small head index and vector, or, if several words may split between the fields and the
    # extracted text or the query has operators, a row with any word as a head prefix whose large
    # vector satisfies the whole query. Whole-word hits are excluded through their own, short
    # list instead of testing every candidate's large vector again; the list is short because this
    # statement only runs when the whole-word hits did not fill the page.
    if words is not None and len(words) == 1:
        prefix = "item.search_head @@ q.head_q"
    else:
        prefix = "(item.search_head @@ q.guard_q AND ts_match_vq(item.search_vector, q.prefix_q))"
        if words is not None:
            prefix = f"(item.search_head @@ q.head_q OR {prefix})"
    arms = [prefix]
    if likes:
        arms.append(
            "("
            + " AND ".join(f"{FILENAME_KEY} LIKE lower(%(like_{i})s)" for i in range(len(likes)))
            + ")"
        )
    where = " AND ".join(
        [*clauses, f"({' OR '.join(arms)})", "item.id NOT IN (SELECT id FROM whole_hit)"]
    )
    # Prefix hits by the same field tiers as whole-word hits, then substring-only hits, each in
    # the order of the head rank; the score stays below 1.
    prefix_score = (
        "0.5 + (CASE WHEN item.search_head @@ q.ap_q THEN 4 ELSE 0 END "
        "+ CASE WHEN item.search_head @@ q.bp_q THEN 2 ELSE 0 END "
        "+ CASE WHEN item.search_head @@ q.cp_q THEN 1 ELSE 0 END) / 16.0 "
        "+ ts_rank(item.search_head, q.guard_q, 33) / 32"
    )
    score = f"CASE WHEN {prefix} THEN {prefix_score} ELSE 0.25 END" if likes else prefix_score
    filename = (
        f" OR {FILENAME_KEY} LIKE ANY (SELECT lower(w) FROM unnest(%(likes)s::text[]) AS w)"
        if likes
        else ""
    )
    hit = (
        f"SELECT item.id AS hit_id, {score} AS hit_rank FROM item CROSS JOIN q WHERE {where} "
        "ORDER BY hit_rank DESC, item.id DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    whole_hit = (
        f"whole_hit AS MATERIALIZED (SELECT item.id FROM item CROSS JOIN q "
        f"WHERE {' AND '.join(whole)})"
    )
    return _result_sql(
        hit,
        queries=f"{PARTIAL_QUERIES_CTE}, {whole_hit}",
        rank="hit.hit_rank",
        headline_query="q.prefix_q",
        fields=_matched_fields_sql("field_q", filename),
    )


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
    words = _plain_words(q)
    likes = substring_patterns(words)
    params.update(
        {
            "q": q,
            "q_split": _JOINERS.sub(" ", q),
            "limit": limit + 1,
            "offset": offset,
            "options": HEADLINE_OPTIONS,
            "likes": likes,
            **{f"like_{i}": pattern for i, pattern in enumerate(likes)},
        }
    )
    whole = [*clauses, "item.search_vector @@ q.full_q"]
    with connection(settings) as conn:
        rows = conn.execute(_whole_word_sql(whole), params).fetchall()
        if len(rows) <= limit:
            # Every whole-word hit is on this page or before it; partial hits follow them.
            skip, needed = 0, limit + 1 - len(rows)
            if not rows and offset:
                # The offset lies beyond the whole-word hits, which therefore number at most
                # `offset`: count them to continue in the partial hits where they end.
                counted = conn.execute(
                    f"WITH {QUERIES_CTE} SELECT count(*) AS n FROM (SELECT 1 FROM item "
                    f"CROSS JOIN q WHERE {' AND '.join(whole)} LIMIT %(offset)s) c",
                    params,
                ).fetchone()
                skip = offset - (counted["n"] if counted else 0)
            params.update({"limit": needed, "offset": skip})
            rows += conn.execute(_partial_sql(clauses, whole, words, likes), params).fetchall()
    results = [SearchResult.model_validate(_summary(row)) for row in rows[:limit]]
    return SearchPage(
        results=results,
        next_offset=offset + limit if len(rows) > limit else None,
    )
