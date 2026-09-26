from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import psycopg

from bag.config import Settings
from bag.db import Row, connection
from bag.ids import uuid7
from bag.schemas import NamedResponse

Kind = Literal["tag", "collection"]
LINK = {"tag": ("item_tag", "tag_id"), "collection": ("item_collection", "collection_id")}

# Correlated arrays for item responses; user, system and AI assignments alike.
TAGS_SQL = (
    "(SELECT coalesce(array_agg(t.name ORDER BY t.name), '{}') FROM item_tag it "
    "JOIN tag t ON (t.owner_id = it.owner_id AND t.id = it.tag_id) "
    "WHERE it.owner_id = item.owner_id AND it.item_id = item.id) AS tags"
)
COLLECTIONS_SQL = (
    "(SELECT coalesce(array_agg(c.name ORDER BY c.name), '{}') FROM item_collection ic "
    "JOIN collection c ON (c.owner_id = ic.owner_id AND c.id = ic.collection_id) "
    "WHERE ic.owner_id = item.owner_id AND ic.item_id = item.id) AS collections"
)


def ensure_named(conn: psycopg.Connection[Row], kind: Kind, owner_id: UUID, name: str) -> UUID:
    """Return the ID of the owner's tag/collection, creating it if missing."""
    conn.execute(
        f"INSERT INTO {kind} (id, owner_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (owner_id, name) DO NOTHING",
        (uuid7(), owner_id, name),
    )
    row = conn.execute(
        f"SELECT id FROM {kind} WHERE owner_id = %s AND name = %s", (owner_id, name)
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def create_named(
    settings: Settings, kind: Kind, owner_id: UUID, name: str
) -> tuple[NamedResponse, bool]:
    with connection(settings) as conn:
        before = conn.execute(
            f"SELECT id FROM {kind} WHERE owner_id = %s AND name = %s", (owner_id, name)
        ).fetchone()
        named_id = ensure_named(conn, kind, owner_id, name)
        rows = _list(conn, kind, owner_id, named_id)
    return rows[0], before is None


def list_named(settings: Settings, kind: Kind, owner_id: UUID) -> list[NamedResponse]:
    with connection(settings) as conn:
        return _list(conn, kind, owner_id, None)


def _list(
    conn: psycopg.Connection[Row], kind: Kind, owner_id: UUID, only: UUID | None
) -> list[NamedResponse]:
    link, column = LINK[kind]
    rows = conn.execute(
        f"SELECT n.id, n.name, n.created_at, (SELECT count(*) FROM {link} l JOIN item i "
        f"ON (i.owner_id = l.owner_id AND i.id = l.item_id) WHERE l.owner_id = n.owner_id "
        f"AND l.{column} = n.id AND i.deleted_at IS NULL) AS item_count "
        f"FROM {kind} n WHERE n.owner_id = %s AND (%s::uuid IS NULL OR n.id = %s) "
        "ORDER BY n.name",
        (owner_id, only, only),
    ).fetchall()
    return [NamedResponse.model_validate(row) for row in rows]


def rename_named(
    settings: Settings, kind: Kind, owner_id: UUID, named_id: UUID, name: str
) -> NamedResponse | None:
    """Rename; None when the ID is not the owner's. Raises UniqueViolation on a clash."""
    with connection(settings) as conn:
        row = conn.execute(
            f"UPDATE {kind} SET name = %s WHERE owner_id = %s AND id = %s RETURNING id",
            (name, owner_id, named_id),
        ).fetchone()
        if row is None:
            return None
        return _list(conn, kind, owner_id, named_id)[0]


def delete_named(settings: Settings, kind: Kind, owner_id: UUID, named_id: UUID) -> bool:
    """Remove a name and all its assignments; items themselves are untouched."""
    link, column = LINK[kind]
    with connection(settings) as conn:
        conn.execute(
            f"DELETE FROM {link} WHERE owner_id = %s AND {column} = %s", (owner_id, named_id)
        )
        row = conn.execute(
            f"DELETE FROM {kind} WHERE owner_id = %s AND id = %s RETURNING id",
            (owner_id, named_id),
        ).fetchone()
        return row is not None


def set_item_named(
    conn: psycopg.Connection[Row],
    kind: Kind,
    owner_id: UUID,
    item_id: UUID,
    names: Sequence[str],
) -> None:
    """Replace the user's assignments with exactly these names; system/AI ones stay."""
    link, column = LINK[kind]
    wanted = {ensure_named(conn, kind, owner_id, name) for name in names}
    provenance = "AND created_by = 'user'" if kind == "tag" else ""
    conn.execute(
        f"DELETE FROM {link} WHERE owner_id = %s AND item_id = %s {provenance} "
        f"AND NOT ({column} = ANY(%s))",
        (owner_id, item_id, list(wanted)),
    )
    for named_id in wanted:
        columns = "created_by, " if kind == "tag" else ""
        values = "'user', " if kind == "tag" else ""
        conn.execute(
            f"INSERT INTO {link} (id, owner_id, item_id, {column}, {columns}created_at) "
            f"VALUES (%s, %s, %s, %s, {values}now()) "
            f"ON CONFLICT (owner_id, item_id, {column}) DO NOTHING",
            (uuid7(), owner_id, item_id, named_id),
        )
