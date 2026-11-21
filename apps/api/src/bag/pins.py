"""Merkliste (ADR-0028): pin and unpin items, list the pinned ones newest pin first."""

import logging
from uuid import UUID

from fastapi import HTTPException

from bag.capture import get_item
from bag.config import Settings
from bag.db import connection
from bag.schemas import ItemResponse, ItemSummary, PinnedPage
from bag.search import SUMMARY_COLUMNS, summary_row

logger = logging.getLogger(__name__)


def set_pinned(settings: Settings, owner_id: UUID, item_id: UUID, pinned: bool) -> ItemResponse:
    """Pin or unpin; repeating either keeps the state (and the first pin time).

    Trashed and unknown items are 404, as for every other item edit.
    """
    value = "coalesce(pinned_at, now())" if pinned else "NULL"
    changed = "pinned_at IS NULL" if pinned else "pinned_at IS NOT NULL"
    with connection(settings) as conn:
        row = conn.execute(
            f"UPDATE item SET pinned_at = {value}, "
            f"updated_at = CASE WHEN {changed} THEN now() ELSE updated_at END "
            "WHERE owner_id = %s AND id = %s AND deleted_at IS NULL RETURNING id",
            (owner_id, item_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Item not found")
    logger.info(
        "item_pinned" if pinned else "item_unpinned",
        extra={"item_id": item_id, "owner_id": owner_id},
    )
    return get_item(settings, owner_id, item_id)


def list_pinned(settings: Settings, owner_id: UUID, *, limit: int, offset: int) -> PinnedPage:
    """Pinned items that are not in the trash; a restored item comes back with its pin."""
    with connection(settings) as conn:
        rows = conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM item WHERE owner_id = %s AND pinned_at IS NOT NULL "
            "AND deleted_at IS NULL ORDER BY pinned_at DESC, id DESC LIMIT %s OFFSET %s",
            (owner_id, limit + 1, offset),
        ).fetchall()
    items = [ItemSummary.model_validate(summary_row(row)) for row in rows[:limit]]
    return PinnedPage(items=items, next_offset=offset + limit if len(rows) > limit else None)
