"""Prefix and substring search: search_head vector, prefix queries and a filename trigram index."""

from pathlib import Path

from alembic import op

revision = "0006_search_partial"
down_revision = "0005_search_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    # The functions of 0005 come back first, so no trigger writes the column being dropped.
    # pg_trgm stays installed: other objects may use it, and it is harmless.
    sql = Path(__file__).with_name("0006_search_partial_down.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
    for statement in (
        "DROP INDEX item_filename_trgm_idx",
        "DROP INDEX item_search_head_idx",
        "ALTER TABLE item DROP COLUMN search_head",
        "DROP FUNCTION bag_prefix_query(tsquery, tsquery, integer, text, boolean)",
        "DROP FUNCTION bag_filename_key(text)",
        "DROP FUNCTION bag_head_vector(text, text, tsvector, text, text, text)",
    ):
        op.execute(statement)
