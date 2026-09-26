"""Web login: username, password hash and server-side sessions."""

from pathlib import Path

from alembic import op

revision = "0004_sessions"
down_revision = "0003_active_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP TABLE session")
    op.execute('ALTER TABLE "user" DROP COLUMN password_hash')
    op.execute('ALTER TABLE "user" DROP COLUMN username')
