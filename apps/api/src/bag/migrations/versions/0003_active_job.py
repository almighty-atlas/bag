"""At most one active job per item and processor."""

from pathlib import Path

from alembic import op

revision = "0003_active_job"
down_revision = "0002_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP INDEX job_active_idx")
