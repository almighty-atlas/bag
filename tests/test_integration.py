from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from bag.api import create_app
from bag.auth import token_hash
from bag.cli import initialize, migration_config
from bag.config import Settings
from bag.db import connection, ready
from bag.ids import uuid7
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_capture_persist_restart_and_replay(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    content = "  Grüße 💼 — 日本語\r\n<script>untrusted</script>\n  "
    payload = {
        "content": content,
        "user_note": "Für später",
        "client_capture_id": str(uuid4()),
        "captured_at": "2026-09-25T13:45:00+02:00",
    }
    response = client.post("/api/v1/capture/text", json=payload, headers=headers)
    assert response.status_code == 201
    item_id = response.json()["id"]
    assert UUID(item_id).version == 7
    with connection(settings) as conn:
        owner = conn.execute('SELECT id FROM "user"').fetchone()
        assert owner is not None
        row = conn.execute(
            "SELECT content FROM item WHERE owner_id = %s AND id = %s",
            (owner["id"], item_id),
        ).fetchone()
        assert row is not None and row["content"] == content
    # A new app/connection has no in-memory state from the capture.
    with TestClient(create_app(settings)) as restarted:
        item = restarted.get(f"/api/v1/items/{item_id}", headers=headers)
        assert item.status_code == 200
        assert item.json()["content"] == content
        assert item.json()["user_note"] == "Für später"
        assert item.json()["captured_at"] == "2026-09-25T11:45:00Z"
        replay = restarted.post(
            "/api/v1/capture/text",
            json={**payload, "content": "changed"},
            headers=headers,
        )
        assert replay.json() == response.json()
        assert (
            restarted.get(f"/api/v1/items/{item_id}", headers=headers).json()["content"] == content
        )


def test_concurrent_idempotency_and_duplicates(
    client: TestClient,
    headers: dict[str, str],
    settings: Settings,
) -> None:
    key = str(uuid4())

    def capture(index: int) -> dict[str, object]:
        response = client.post(
            "/api/v1/capture/text",
            json={"content": "same original", "client_capture_id": key},
            headers=headers,
        )
        assert response.status_code == 201
        result: dict[str, object] = response.json()
        return result

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(capture, range(12)))
    assert len({str(row["id"]) for row in results}) == 1

    def duplicate(index: int) -> dict[str, object]:
        response = client.post(
            "/api/v1/capture/text",
            json={"content": "other original"},
            headers=headers,
        )
        assert response.status_code == 201
        result: dict[str, object] = response.json()
        return result

    with ThreadPoolExecutor(max_workers=6) as pool:
        duplicates = list(pool.map(duplicate, range(6)))
    assert len({str(row["id"]) for row in duplicates}) == 6
    roots = [row["id"] for row in duplicates if row["duplicate_of"] is None]
    assert len(roots) == 1
    assert all(row["duplicate_of"] == roots[0] for row in duplicates if row["id"] != roots[0])


def test_header_idempotency_validation_and_duplicate_replay(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    key = str(uuid4())
    first = client.post("/api/v1/capture/text", json={"content": "same"}, headers=headers).json()
    keyed = {**headers, "Idempotency-Key": key}
    response = client.post("/api/v1/capture/text", json={"content": "same"}, headers=keyed)
    assert response.json()["duplicate_of"] == first["id"]
    replay = client.post("/api/v1/capture/text", json={"content": "same"}, headers=keyed)
    assert replay.json() == response.json()
    conflict = client.post(
        "/api/v1/capture/text",
        json={"content": "x", "client_capture_id": str(uuid4())},
        headers=keyed,
    )
    assert conflict.status_code == 422
    for payload in ({"content": "a\x00"}, {"content": ""}, {"content": "x", "kind": "file"}):
        invalid = client.post("/api/v1/capture/text", json=payload, headers=headers)
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "Invalid request"}


def test_authentication_owner_isolation_and_soft_deleted_replay(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    key = str(uuid4())
    payload = {"content": "private", "client_capture_id": key}
    saved = client.post("/api/v1/capture/text", json=payload, headers=headers).json()
    other_owner = uuid7()
    with connection(settings) as conn:
        conn.execute(
            'INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other_owner, "Other")
        )
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other_owner, "test", token_hash("other-token")),
        )
    other_headers = {"Authorization": "Bearer other-token"}
    assert client.get(f"/api/v1/items/{saved['id']}", headers=other_headers).status_code == 404
    other = client.post("/api/v1/capture/text", json=payload, headers=other_headers).json()
    assert other["id"] != saved["id"] and other["duplicate_of"] is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection(settings) as conn:
        conn.execute(
            "INSERT INTO relation (id, owner_id, source_item_id, target_item_id, "
            "relation_type, created_by) VALUES (%s, %s, %s, %s, 'related_to', 'user')",
            (uuid7(), other_owner, other["id"], saved["id"]),
        )
    with connection(settings) as conn:
        conn.execute(
            "UPDATE item SET deleted_at = now() WHERE owner_id = %s AND id = %s",
            (other_owner, other["id"]),
        )
    assert client.get(f"/api/v1/items/{other['id']}", headers=other_headers).status_code == 404
    assert (
        client.post(
            "/api/v1/capture/text",
            json=payload,
            headers=other_headers,
        ).json()["id"]
        == other["id"]
    )
    with connection(settings) as conn:
        conn.execute("UPDATE api_token SET revoked_at = now() WHERE owner_id = %s", (other_owner,))
    assert client.get(f"/api/v1/items/{other['id']}", headers=other_headers).status_code == 401
    assert client.post("/api/v1/capture/text", json=payload).status_code == 401
    assert (
        client.post(
            "/api/v1/capture/text",
            json=payload,
            headers={"Authorization": "Bearer invalid"},
        ).status_code
        == 401
    )


def test_commit_failure_never_acknowledges_and_retry_succeeds(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    with connection(settings) as conn:
        conn.execute("""
            CREATE FUNCTION test_reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private text must not leak'; END $$;
            CREATE CONSTRAINT TRIGGER test_reject_commit AFTER INSERT ON item
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_reject_commit();
        """)
    payload = {"content": "precious original", "client_capture_id": str(uuid4())}
    try:
        response = client.post("/api/v1/capture/text", json=payload, headers=headers)
        assert response.status_code == 503
        assert "private text" not in response.text
        with connection(settings) as conn:
            assert conn.execute("SELECT count(*) AS count FROM item").fetchone() == {"count": 0}
    finally:
        with connection(settings) as conn:
            conn.execute(
                "DROP TRIGGER test_reject_commit ON item; DROP FUNCTION test_reject_commit()"
            )
    saved = client.post("/api/v1/capture/text", json=payload, headers=headers)
    assert saved.status_code == 201
    # Simulate losing the HTTP success response: retry the same operation.
    assert client.post("/api/v1/capture/text", json=payload, headers=headers).json() == saved.json()


def test_bootstrap_and_migration_round_trip(settings: Settings) -> None:
    assert ready(settings)
    first = initialize(settings)
    assert first is not None
    assert initialize(settings) is None
    with connection(settings) as conn:
        row = conn.execute("SELECT token_hash FROM api_token").fetchone()
        assert row == {"token_hash": token_hash(first)}
        assert conn.execute('SELECT count(*) AS count FROM "user"').fetchone() == {"count": 1}
    command.downgrade(migration_config(), "base")
    assert not ready(settings)
    command.upgrade(migration_config(), "head")
    assert ready(settings)
