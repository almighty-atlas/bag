"""Processing job queue."""

from pathlib import Path

from alembic import op

revision = "0002_jobs"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP TABLE job")
