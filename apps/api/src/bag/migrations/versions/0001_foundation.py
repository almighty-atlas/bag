"""Initial owner-scoped schema."""

from pathlib import Path

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    for table in (
        "relation",
        "item_collection",
        "collection",
        "item_tag",
        "tag",
        "processing_run",
        "blob",
        "item",
        "api_token",
        "user",
    ):
        op.execute(f'DROP TABLE "{table}"')
