import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from alembic import command
from bag.api import create_app
from bag.cli import initialize, migration_config
from bag.config import Settings
from bag.db import connection
from fastapi.testclient import TestClient
from pydantic import SecretStr


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Settings]:
    url = os.getenv("BAG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BAG_TEST_DATABASE_URL to a disposable PostgreSQL 17 database")
    if not urlparse(url).path.endswith("_test"):
        pytest.fail("Test database name must end in _test; tests clear its Bag tables")
    monkeypatch.setenv("BAG_DATABASE_URL", url)
    command.upgrade(migration_config(), "head")
    value = Settings(database_url=SecretStr(url), storage_path=tmp_path / "storage")
    with connection(value) as conn:
        conn.execute('TRUNCATE TABLE "user" CASCADE')
    yield value
    with connection(value) as conn:
        conn.execute('TRUNCATE TABLE "user" CASCADE')


@pytest.fixture
def token(settings: Settings) -> str:
    value = initialize(settings)
    assert value is not None
    return value


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as value:
        yield value


@pytest.fixture
def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
