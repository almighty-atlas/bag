-- Merkliste (ADR-0028): an item is pinned while pinned_at is set. The timestamp orders the
-- list (newest pin first); pinning again keeps it, unpinning clears it. No other table, no
-- second item store: trash, restore, purge and export treat it like any other item column.
ALTER TABLE item ADD COLUMN pinned_at timestamptz;

-- The list of one owner, newest pin first; small, because only pinned rows are indexed.
CREATE INDEX item_pinned_idx ON item (owner_id, pinned_at DESC, id DESC)
    WHERE pinned_at IS NOT NULL;
