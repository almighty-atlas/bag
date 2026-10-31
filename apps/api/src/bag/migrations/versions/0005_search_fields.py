"""Search over filenames and tags with field weights."""

from pathlib import Path

from alembic import op

revision = "0005_search_fields"
down_revision = "0004_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    for statement in (
        "DROP TRIGGER tag_search_renamed ON tag",
        "DROP TRIGGER item_tag_search_update ON item_tag",
        "DROP TRIGGER item_tag_search_delete ON item_tag",
        "DROP TRIGGER item_tag_search_insert ON item_tag",
        "DROP TRIGGER item_search_before ON item",
        "DROP FUNCTION tag_search_renamed()",
        "DROP FUNCTION item_tag_search_updated()",
        "DROP FUNCTION item_tag_search_changed()",
        "DROP FUNCTION item_search_before()",
        "DROP FUNCTION bag_refresh_item_search(uuid, uuid)",
        "DROP FUNCTION bag_restrict_query(tsquery, text)",
        "DROP FUNCTION bag_any_term_query(tsquery)",
        "DROP FUNCTION bag_item_tags_vector(uuid, uuid)",
        "DROP FUNCTION bag_item_vector(text, text, tsvector, text, text, text, text, boolean)",
        'DROP FUNCTION bag_name_vector(text, "char")',
        'DROP FUNCTION bag_text_vector(text, regconfig, "char")',
        "DROP FUNCTION bag_name_text(text)",
        "DROP FUNCTION bag_language_config(text)",
        # A generated column cannot be restored in place; the table is rewritten.
        "DROP INDEX item_search_idx",
        "ALTER TABLE item DROP COLUMN search_vector",
        """
        ALTER TABLE item ADD COLUMN search_vector tsvector GENERATED ALWAYS AS (
            to_tsvector('simple'::regconfig,
                coalesce(title, '') || ' ' || coalesce(extracted_text, '') || ' ' ||
                coalesce(source_url, '') || ' ' || coalesce(user_note, '')) ||
            to_tsvector(CASE language WHEN 'de' THEN 'german'::regconfig
                WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END,
                coalesce(title, '') || ' ' || coalesce(extracted_text, '') || ' ' ||
                coalesce(source_url, '') || ' ' || coalesce(user_note, ''))
        ) STORED
        """,
        "CREATE INDEX item_search_idx ON item USING gin (search_vector)",
    ):
        op.execute(statement)
