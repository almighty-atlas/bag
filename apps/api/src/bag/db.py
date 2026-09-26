from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from bag.config import Settings

SCHEMA_REVISION = "0004_sessions"
Row = dict[str, Any]


@contextmanager
def connection(settings: Settings) -> Iterator[psycopg.Connection[Row]]:
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        row_factory=dict_row,
        connect_timeout=5,
        options="-c timezone=UTC -c statement_timeout=10000 -c synchronous_commit=on",
    ) as conn:
        yield conn


def ready(settings: Settings) -> bool:
    try:
        with connection(settings) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            return row is not None and row["version_num"] == SCHEMA_REVISION
    except psycopg.Error:
        return False
