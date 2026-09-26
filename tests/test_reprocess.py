import json

import psycopg
import pytest
from bag.auth import token_hash
from bag.cli import main, reprocess_items
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.jobs import claim
from bag.processors import PROCESSORS, Outcome, ProcessingItem
from bag.storage import BlobStorage, FileSystemStorage
from bag.tokens import TokenAdminError
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain, runs

pytestmark = pytest.mark.integration


def crash(item: ProcessingItem, storage: BlobStorage) -> Outcome:
    raise RuntimeError("flaky")


def test_reprocess_resets_runs_recovers_failures_and_skips_active_jobs(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    worker = Worker(settings, storage)
    saved = client.post("/api/v1/capture/text", json={"content": "again"}, headers=headers).json()
    drain(Worker(settings, storage, {**PROCESSORS, "text_extract": crash}))
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "partial" and item["extracted_text"] is None

    path = f"/api/v1/items/{saved['id']}/reprocess"
    response = client.post(path, headers=headers)
    assert response.status_code == 202, response.text
    assert all(
        row["status"] == "pending" and row["attempts"] == 0 and row["last_error"] is None
        for row in response.json()
    )
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "queued" and item["content"] == "again"
    # Reprocessing an already queued item must not duplicate jobs.
    assert client.post(path, headers=headers).status_code == 202
    with connection(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM job WHERE status = 'queued'").fetchone() == {
            "n": len(PROCESSORS)
        }
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO job (id, owner_id, item_id, processor, max_attempts) "
                "SELECT %s, owner_id, item_id, processor, 1 FROM job "
                "WHERE status = 'queued' LIMIT 1",
                (uuid7(),),
            )
    assert drain(worker) == len(PROCESSORS)
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "ready" and item["extracted_text"] == "again"
    assert all(r["attempts"] == 1 for r in runs(client, headers, saved["id"]).values())

    # A running processor keeps its lease; only the others are rescheduled.
    client.post(path, headers=headers)
    assert worker.run_once()
    with connection(settings) as conn:
        running = claim(conn, "busy", settings.job_lease_seconds)
        assert running is not None
    finished = next(name for name in PROCESSORS if name != running.processor)
    assert runs(client, headers, saved["id"])[finished]["status"] == "succeeded"
    result = {row["processor"]: row for row in client.post(path, headers=headers).json()}
    assert result[running.processor]["status"] == "running"
    assert result[running.processor]["attempts"] == 1
    assert result[finished]["status"] == "pending" and result[finished]["attempts"] == 0
    with connection(settings) as conn:
        assert conn.execute("SELECT status FROM job WHERE id = %s", (running.id,)).fetchone() == {
            "status": "running"
        }
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "processing"


def test_reprocess_scoping(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    path = f"/api/v1/items/{saved['id']}/reprocess"
    assert client.post(path).status_code == 401
    assert client.post(f"/api/v1/items/{uuid7()}/reprocess", headers=headers).status_code == 404
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    assert client.post(path, headers={"Authorization": "Bearer other"}).status_code == 404
    with connection(settings) as conn:
        conn.execute("UPDATE item SET deleted_at = now() WHERE id = %s", (saved["id"],))
    assert client.post(path, headers=headers).status_code == 404
    with pytest.raises(TokenAdminError, match="Multiple owners"):
        reprocess_items(settings, "all", None)


def test_bulk_reprocess_scopes_and_pre_migration_items(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    processed = client.post("/api/v1/capture/text", json={"content": "done"}, headers=headers)
    drain(Worker(settings, storage))
    partial = client.post("/api/v1/capture/text", json={"content": "broken"}, headers=headers)
    drain(Worker(settings, storage, {**PROCESSORS, "text_extract": crash}))
    partial_id = str(partial.json()["id"])
    assert runs(client, headers, partial_id)["text_extract"]["status"] == "failed"
    with connection(settings) as conn:
        owner = conn.execute("SELECT owner_id FROM item LIMIT 1").fetchone()
        assert owner is not None
        legacy = uuid7()
        # An item captured before migration 0002: ready, without runs or jobs.
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, content, content_hash, mime_type, "
            "processing_status) VALUES (%s, %s, 'text', 'api', 'alt', repeat('a', 64), "
            "'text/plain', 'ready')",
            (legacy, owner["owner_id"]),
        )
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, content, processing_status, deleted_at) "
            "VALUES (%s, %s, 'text', 'api', 'trash', 'ready', now())",
            (uuid7(), owner["owner_id"]),
        )

    assert reprocess_items(settings, "missing", None) == {"items": 1, "jobs": len(PROCESSORS)}
    assert reprocess_items(settings, "missing", None) == {"items": 0, "jobs": 0}
    assert set(runs(client, headers, str(legacy))) == set(PROCESSORS)
    assert reprocess_items(settings, "failed", None) == {"items": 1, "jobs": 1}
    assert runs(client, headers, partial_id)["text_extract"]["status"] == "pending"
    drain(Worker(settings, storage))
    for item_id in (str(legacy), partial_id, str(processed.json()["id"])):
        item = client.get(f"/api/v1/items/{item_id}", headers=headers).json()
        assert item["processing_status"] == "ready"
        assert item["extracted_text"] == item["content"]

    monkeypatch.setattr("sys.argv", ["bag", "reprocess", "--scope", "all"])
    main()
    assert json.loads(capsys.readouterr().out) == {"items": 3, "jobs": 3 * len(PROCESSORS)}
    with connection(settings) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM item WHERE processing_status = 'queued'"
        ).fetchone() == {"n": 3}
    assert drain(Worker(settings, storage)) == 3 * len(PROCESSORS)
