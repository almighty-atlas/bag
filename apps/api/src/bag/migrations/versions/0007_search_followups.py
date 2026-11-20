"""Search follow-ups: accent folding, URL words, name substrings and cheap tag re-indexing."""

from pathlib import Path

from alembic import op

revision = "0007_search_followups"
down_revision = "0006_search_partial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    # The functions of 0006 come back first, so no trigger writes the column being dropped.
    # unaccent stays installed: other objects may use it, and it is harmless.
    sql = Path(__file__).with_name("0007_search_followups_down.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
    for statement in (
        "DROP INDEX item_names_trgm_idx",
        "ALTER TABLE item DROP COLUMN search_names",
        "CREATE INDEX item_filename_trgm_idx ON item "
        "USING gin (bag_filename_key(original_filename) gin_trgm_ops)",
        "DROP FUNCTION bag_join_vectors(tsvector, tsvector)",
        "DROP FUNCTION bag_url_vector(text, regconfig)",
        'DROP FUNCTION bag_field_vector(text, regconfig, "char")',
        'DROP FUNCTION bag_new_lexemes(tsvector, tsvector, "char")',
        "DROP FUNCTION bag_item_names(uuid, uuid, text, text)",
        "DROP FUNCTION bag_like_key(text)",
        "DROP FUNCTION bag_name_key(text)",
        "DROP FUNCTION bag_translit(text)",
        "DROP FUNCTION bag_fold(text)",
    ):
        op.execute(statement)
