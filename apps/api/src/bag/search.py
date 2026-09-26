from datetime import datetime
from typing import Any
from uuid import UUID

from bag.config import Settings
from bag.db import connection
from bag.schemas import ItemPage, ItemSummary, SearchPage, SearchResult

SUMMARY_COLUMNS = (
    "id, kind, source, title, user_note, mime_type, original_filename, source_url, language, "
    "processing_status, created_at, captured_at, updated_at, deleted_at"
)
# Query every configuration: a German query stems for German items, English for
# English ones, and simple matches exact tokens in items without a language.
QUERY_SQL = (
    "(websearch_to_tsquery('simple', %(q)s) || websearch_to_tsquery('german', %(q)s) "
    "|| websearch_to_tsquery('english', %(q)s))"
)
HEADLINE_TEXT = (
    "coalesce(title, '') || E'\\n' || coalesce(user_note, '') || E'\\n' || "
    "coalesce(source_url, '') || E'\\n' || coalesce(extracted_text, '')"
)
HEADLINE_OPTIONS = "StartSel=«, StopSel=», MaxFragments=2, MaxWords=18, MinWords=6"


def _filters(
    owner_id: UUID,
    *,
    kind: str | None,
    status: str | None,
    captured_from: datetime | None,
    captured_to: datetime | None,
    trashed: bool,
) -> tuple[list[str], dict[str, Any]]:
    clauses = ["owner_id = %(owner_id)s"]
    params: dict[str, Any] = {"owner_id": owner_id}
    clauses.append("deleted_at IS NOT NULL" if trashed else "deleted_at IS NULL")
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
) -> ItemPage:
    clauses, params = _filters(
        owner_id,
        kind=kind,
        status=status,
        captured_from=captured_from,
        captured_to=captured_to,
        trashed=trashed,
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
    items = [ItemSummary.model_validate(row) for row in rows[:limit]]
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
) -> SearchPage:
    clauses, params = _filters(
        owner_id,
        kind=kind,
        status=status,
        captured_from=captured_from,
        captured_to=captured_to,
        trashed=trashed,
    )
    clauses.append(f"search_vector @@ {QUERY_SQL}")
    params.update({"q": q, "limit": limit + 1, "offset": offset})
    with connection(settings) as conn:
        rows = conn.execute(
            f"SELECT {SUMMARY_COLUMNS}, ts_rank_cd(search_vector, {QUERY_SQL}) AS rank, "
            f"ts_headline(CASE language WHEN 'de' THEN 'german'::regconfig "
            f"WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END, "
            f"{HEADLINE_TEXT}, {QUERY_SQL}, %(options)s) AS snippet "
            f"FROM item WHERE {' AND '.join(clauses)} "
            "ORDER BY rank DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
            {**params, "options": HEADLINE_OPTIONS},
        ).fetchall()
    results = [SearchResult.model_validate(row) for row in rows[:limit]]
    return SearchPage(
        results=results,
        next_offset=offset + limit if len(rows) > limit else None,
    )
