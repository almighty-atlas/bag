import pytest
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain

pytestmark = pytest.mark.integration


def ids(client: TestClient, headers: dict[str, str], **params: str) -> list[str]:
    response = client.get("/api/v1/items", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return [str(row["id"]) for row in response.json()["items"]]


def test_trash_hides_restores_and_preserves_originals(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post(
        "/api/v1/capture/file",
        headers=headers,
        files={"file": ("gedanken.txt", "Gedanken über den Umzug der Bücher".encode())},
        data={"metadata": f'{{"user_note": "Umzug", "client_capture_id": "{uuid7()}"}}'},
    ).json()
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    item_id = saved["id"]
    path = f"/api/v1/items/{item_id}"
    before = client.get(path, headers=headers).json()
    with connection(settings) as conn:
        stamp = conn.execute("SELECT updated_at FROM item WHERE id = %s", (item_id,)).fetchone()
        assert stamp is not None
    assert client.get("/api/v1/search?q=Umzug", headers=headers).json()["results"]

    assert client.delete(path, headers=headers).status_code == 204
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(f"{path}/content", headers=headers).status_code == 404
    assert client.get(f"{path}/processing", headers=headers).status_code == 404
    assert client.post(f"{path}/reprocess", headers=headers).status_code == 404
    assert ids(client, headers) == [] and ids(client, headers, trashed="true") == [item_id]
    assert client.get("/api/v1/search?q=Umzug", headers=headers).json()["results"] == []
    assert client.get("/api/v1/search?q=Umzug&trashed=true", headers=headers).json()["results"]
    # Trashing again is a no-op, and the original bytes stay on disk.
    assert client.delete(path, headers=headers).status_code == 204
    assert len([p for p in settings.storage_path.rglob("*") if p.is_file()]) == 1
    with connection(settings) as conn:
        row = conn.execute("SELECT deleted_at, updated_at FROM item WHERE id = %s", (item_id,))
        trashed = row.fetchone()
        assert trashed is not None and trashed["deleted_at"] is not None
        assert trashed["updated_at"] > stamp["updated_at"]

    # A new capture of the same bytes becomes a fresh root, not a duplicate of trash.
    again = client.post(
        "/api/v1/capture/file",
        headers=headers,
        files={"file": ("gedanken.txt", "Gedanken über den Umzug der Bücher".encode())},
    ).json()
    assert again["duplicate_of"] is None and again["id"] != item_id
    # Replaying the original capture key still returns the trashed item unchanged.
    replay = client.post(
        "/api/v1/capture/file",
        headers=headers,
        files={"file": ("other.txt", b"other")},
        data={"metadata": f'{{"client_capture_id": "{saved_key(saved, settings)}"}}'},
    ).json()
    assert replay["id"] == item_id
    assert client.get(path, headers=headers).status_code == 404

    restored = client.post(f"{path}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["content_hash"] == before["content_hash"]
    assert restored.json()["user_note"] == "Umzug" and restored.json()["extracted_text"]
    assert restored.json()["updated_at"] >= before["updated_at"]
    assert client.post(f"{path}/restore", headers=headers).status_code == 200
    assert set(ids(client, headers)) == {item_id, again["id"]}
    assert ids(client, headers, trashed="true") == []
    assert client.get(f"{path}/content", headers=headers).content == (
        "Gedanken über den Umzug der Bücher".encode()
    )
    assert {
        r["id"] for r in client.get("/api/v1/search?q=Umzug", headers=headers).json()["results"]
    }
    assert len([p for p in settings.storage_path.rglob("*") if p.is_file()]) == 1


def saved_key(saved: dict[str, str], settings: Settings) -> str:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT client_capture_id FROM item WHERE id = %s", (saved["id"],)
        ).fetchone()
        assert row is not None
        return str(row["client_capture_id"])


def test_trash_scoping_and_errors(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    path = f"/api/v1/items/{saved['id']}"
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    foreign = {"Authorization": "Bearer other"}
    assert client.delete(path).status_code == 401
    assert client.post(f"{path}/restore").status_code == 401
    assert client.delete(path, headers=foreign).status_code == 404
    assert client.post(f"{path}/restore", headers=foreign).status_code == 404
    assert client.delete(f"/api/v1/items/{uuid7()}", headers=headers).status_code == 404
    assert client.post(f"/api/v1/items/{uuid7()}/restore", headers=headers).status_code == 404
    assert client.delete("/api/v1/items/not-a-uuid", headers=headers).status_code == 422
    assert client.get(path, headers=headers).status_code == 200
    # Restoring a live item changes nothing.
    before = client.get(path, headers=headers).json()
    assert client.post(f"{path}/restore", headers=headers).json() == before
