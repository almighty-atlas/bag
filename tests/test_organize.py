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


def item_ids(client: TestClient, headers: dict[str, str], path: str) -> list[str]:
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    return [str(row["id"]) for row in body.get("items", body.get("results"))]


def test_tags_and_collections_assign_filter_and_count(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    first = client.post("/api/v1/capture/text", json={"content": "Umzug Kisten"}, headers=headers)
    second = client.post("/api/v1/capture/text", json={"content": "Umzug Bücher"}, headers=headers)
    first_id, second_id = first.json()["id"], second.json()["id"]
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    created = client.post("/api/v1/tags", json={"name": " Wohnung "}, headers=headers)
    assert created.status_code == 201 and created.json()["name"] == "Wohnung"
    assert created.json()["item_count"] == 0
    again = client.post("/api/v1/tags", json={"name": "Wohnung"}, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == created.json()["id"]
    assert (
        client.post("/api/v1/collections", json={"name": "Umzug 2026"}, headers=headers).status_code
        == 201
    )

    edited = client.patch(
        f"/api/v1/items/{first_id}",
        json={"tags": ["Wohnung", "todo", "todo"], "collections": ["Umzug 2026"]},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert sorted(edited.json()["tags"]) == ["Wohnung", "todo"]
    assert edited.json()["collections"] == ["Umzug 2026"]
    assert client.get(f"/api/v1/items/{second_id}", headers=headers).json()["tags"] == []
    tags = {row["name"]: row for row in client.get("/api/v1/tags", headers=headers).json()}
    assert set(tags) == {"Wohnung", "todo"} and tags["todo"]["item_count"] == 1
    collections = client.get("/api/v1/collections", headers=headers).json()
    assert [(c["name"], c["item_count"]) for c in collections] == [("Umzug 2026", 1)]

    assert item_ids(client, headers, "/api/v1/items?tag=todo") == [first_id]
    assert item_ids(client, headers, "/api/v1/items?collection=Umzug%202026") == [first_id]
    assert item_ids(client, headers, "/api/v1/items?tag=missing") == []
    assert item_ids(client, headers, "/api/v1/search?q=Umzug&tag=Wohnung") == [first_id]
    assert set(item_ids(client, headers, "/api/v1/search?q=Umzug")) == {first_id, second_id}
    summary = client.get("/api/v1/items?tag=todo", headers=headers).json()["items"][0]
    assert sorted(summary["tags"]) == ["Wohnung", "todo"]
    assert summary["collections"] == ["Umzug 2026"]

    # Replacing the set keeps system assignments and drops only user ones.
    with connection(settings) as conn:
        owner = conn.execute("SELECT owner_id FROM item WHERE id = %s", (first_id,)).fetchone()
        assert owner is not None
        conn.execute(
            "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, 'auto')",
            (uuid7(), owner["owner_id"]),
        )
        conn.execute(
            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by, confidence) "
            "SELECT %s, owner_id, %s, id, 'system', 0.5 FROM tag WHERE name = 'auto'",
            (uuid7(), first_id),
        )
    replaced = client.patch(f"/api/v1/items/{first_id}", json={"tags": ["done"]}, headers=headers)
    assert replaced.json()["tags"] == ["auto", "done"]
    cleared = client.patch(
        f"/api/v1/items/{first_id}", json={"tags": [], "collections": []}, headers=headers
    )
    assert cleared.json()["tags"] == ["auto"] and cleared.json()["collections"] == []
    tags = {
        row["name"]: row["item_count"] for row in client.get("/api/v1/tags", headers=headers).json()
    }
    assert tags == {"Wohnung": 0, "auto": 1, "done": 0, "todo": 0}
    # Trashed items do not count.
    client.patch(f"/api/v1/items/{second_id}", json={"tags": ["todo"]}, headers=headers)
    client.delete(f"/api/v1/items/{second_id}", headers=headers)
    tags = {
        row["name"]: row["item_count"] for row in client.get("/api/v1/tags", headers=headers).json()
    }
    assert tags["todo"] == 0
    assert item_ids(client, headers, "/api/v1/items?tag=todo&trashed=true") == [second_id]


def test_organize_validation_and_scoping(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    path = f"/api/v1/items/{saved['id']}"
    for payload in (
        {"name": ""},
        {"name": "  "},
        {"name": "x" * 101},
        {"name": "a\nb"},
        {"nam": "x"},
    ):
        assert client.post("/api/v1/tags", json=payload, headers=headers).status_code == 422, (
            payload
        )
        assert client.post("/api/v1/collections", json=payload, headers=headers).status_code == 422
    for body in (
        {"tags": [""]},
        {"tags": "x"},
        {"collections": ["x" * 101]},
        {"tags": ["a\x00"]},
    ):
        assert client.patch(path, json=body, headers=headers).status_code == 422, body
    assert client.get("/api/v1/items?tag=", headers=headers).status_code == 422
    assert client.get("/api/v1/tags").status_code == 401
    assert client.post("/api/v1/tags", json={"name": "x"}).status_code == 401

    client.patch(path, json={"tags": ["secret"], "collections": ["private"]}, headers=headers)
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    foreign = {"Authorization": "Bearer other"}
    assert client.get("/api/v1/tags", headers=foreign).json() == []
    assert client.get("/api/v1/collections", headers=foreign).json() == []
    assert item_ids(client, foreign, "/api/v1/items?tag=secret") == []
    # The same name is a separate tag per owner.
    mirrored = client.post("/api/v1/tags", json={"name": "secret"}, headers=foreign)
    assert mirrored.status_code == 201
    assert mirrored.json()["id"] != client.get("/api/v1/tags", headers=headers).json()[0]["id"]
