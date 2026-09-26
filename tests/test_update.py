import pytest
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain, runs

pytestmark = pytest.mark.integration


def test_patch_edits_metadata_and_protects_user_language(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post(
        "/api/v1/capture/text",
        json={"content": "The bags are full and we have not yet fetched the books."},
        headers=headers,
    ).json()
    path = f"/api/v1/items/{saved['id']}"
    worker = Worker(settings, FileSystemStorage(settings.storage_path))
    drain(worker)
    before = client.get(path, headers=headers).json()
    assert before["language"] == "en" and before["title"] is None

    edited = client.patch(path, json={"title": "Umzug 💼", "user_note": "Bücher"}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["title"] == "Umzug 💼" and edited.json()["user_note"] == "Bücher"
    assert edited.json()["language"] == "en" and edited.json()["content"] == before["content"]
    assert edited.json()["updated_at"] > before["updated_at"]
    hit = client.get("/api/v1/search?q=Umzug", headers=headers).json()["results"]
    assert [row["id"] for row in hit] == [saved["id"]] and hit[0]["title"] == "Umzug 💼"

    # Absent fields stay, null clears.
    cleared = client.patch(path, json={"user_note": None}, headers=headers).json()
    assert cleared["user_note"] is None and cleared["title"] == "Umzug 💼"
    assert client.patch(path, json={}, headers=headers).json()["title"] == "Umzug 💼"

    # A user-chosen language survives reprocessing; clearing it re-enables detection.
    chosen = client.patch(path, json={"language": "de"}, headers=headers).json()
    assert chosen["language"] == "de"
    assert client.post(f"{path}/reprocess", headers=headers).status_code == 202
    drain(worker)
    assert client.get(path, headers=headers).json()["language"] == "de"
    assert runs(client, headers, saved["id"])["language"]["status"] == "skipped"
    with connection(settings) as conn:
        row = conn.execute("SELECT metadata FROM item WHERE id = %s", (saved["id"],)).fetchone()
        assert row is not None and row["metadata"]["language"]["user"] is True
    assert client.patch(path, json={"language": None}, headers=headers).json()["language"] is None
    client.post(f"{path}/reprocess", headers=headers)
    drain(worker)
    assert client.get(path, headers=headers).json()["language"] == "en"
    with connection(settings) as conn:
        row = conn.execute("SELECT metadata FROM item WHERE id = %s", (saved["id"],)).fetchone()
        assert row is not None and "user" not in row["metadata"]["language"]
    # Originals are not editable.
    assert client.get(path, headers=headers).json()["content"] == (
        "The bags are full and we have not yet fetched the books."
    )


def test_patch_validation_and_scoping(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    path = f"/api/v1/items/{saved['id']}"
    for payload in (
        {"content": "rewrite"},
        {"kind": "url"},
        {"language": "fr"},
        {"title": "x" * 501},
        {"title": "nul\x00"},
        {"user_note": 5},
    ):
        response = client.patch(path, json=payload, headers=headers)
        assert response.status_code == 422, payload
        assert response.json() == {"detail": "Invalid request"}
    assert client.patch(path, json={"title": "x"}).status_code == 401
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    foreign = {"Authorization": "Bearer other"}
    assert client.patch(path, json={"title": "x"}, headers=foreign).status_code == 404
    assert (
        client.patch(f"/api/v1/items/{uuid7()}", json={"title": "x"}, headers=headers).status_code
        == 404
    )
    assert client.delete(path, headers=headers).status_code == 204
    assert client.patch(path, json={"title": "x"}, headers=headers).status_code == 404
    assert (
        client.get("/api/v1/items?trashed=true", headers=headers).json()["items"][0]["title"]
        is None
    )
