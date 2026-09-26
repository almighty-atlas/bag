from typing import Any

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


def capture(client: TestClient, headers: dict[str, str], **payload: Any) -> str:
    response = client.post("/api/v1/capture/text", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def search(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/search", params=params, headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_multilingual_search_with_snippets_and_ranking(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    german = capture(
        client,
        headers,
        content="Die Taschen sind voll, und wir haben noch nicht alle Bücher aus dem Keller.",
        user_note="Umzug",
    )
    english = capture(
        client,
        headers,
        content="The bags are full and we have not yet fetched all the books from the basement.",
    )
    url = client.post(
        "/api/v1/capture/url",
        json={"url": "https://example.org/wayland-notes", "user_note": "Wayland Tasche"},
        headers=headers,
    ).json()["id"]
    # Notes are searchable on commit; extracted text only after processing.
    assert [r["id"] for r in search(client, headers, q="Tasche")["results"]] == [url]
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # German stemming: "Tasche" finds "Taschen"; the note matches the URL item exactly.
    result = search(client, headers, q="Tasche")
    assert {row["id"] for row in result["results"]} == {german, url}
    assert result["next_offset"] is None
    row = next(row for row in result["results"] if row["id"] == german)
    assert "«Taschen»" in row["snippet"] and row["language"] == "de"
    assert "<" not in row["snippet"] and row["rank"] > 0
    assert set(row) >= {"kind", "processing_status", "captured_at", "user_note"}
    assert "content" not in row and "extracted_text" not in row

    # English stemming and web-search operators.
    assert [r["id"] for r in search(client, headers, q="book")["results"]] == [english]
    assert [r["id"] for r in search(client, headers, q="Bücher")["results"]] == [german]
    assert [r["id"] for r in search(client, headers, q='"from the basement"')["results"]] == [
        english
    ]
    assert search(client, headers, q="basement -books")["results"] == []
    assert {r["id"] for r in search(client, headers, q="Keller OR basement")["results"]} == {
        german,
        english,
    }
    # URL tokens and notes are searchable; the snippet shows why.
    row = search(client, headers, q="wayland")["results"][0]
    assert row["id"] == url and "«Wayland»" in row["snippet"]
    # Filters narrow the result set.
    assert search(client, headers, q="Tasche", kind="url")["results"][0]["id"] == url
    assert search(client, headers, q="Tasche", status="failed")["results"] == []
    assert search(client, headers, q="Tasche", trashed="true")["results"] == []
    late = search(client, headers, q="Tasche", **{"from": "2099-01-01T00:00:00Z"})
    assert late["results"] == []
    # Pagination by offset.
    first = search(client, headers, q="Tasche", limit=1)
    assert len(first["results"]) == 1 and first["next_offset"] == 1
    second = search(client, headers, q="Tasche", limit=1, offset=1)
    assert len(second["results"]) == 1 and second["next_offset"] is None
    assert first["results"][0]["id"] != second["results"][0]["id"]


def test_search_validation_and_owner_scoping(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    mine = capture(client, headers, content="private notebook entry about authentication")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    other_headers = {"Authorization": "Bearer other"}
    assert search(client, other_headers, q="authentication")["results"] == []
    assert search(client, headers, q="authentication")["results"][0]["id"] == mine
    assert client.get("/api/v1/search", params={"q": "x"}).status_code == 401
    for params in (
        {},
        {"q": ""},
        {"q": "x" * 501},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": 101},
        {"q": "x", "offset": -1},
        {"q": "x", "offset": 1001},
        {"q": "x", "kind": "Text!"},
        {"q": "x", "from": "2026-01-01T00:00:00"},
        {"q": "x", "trashed": "maybe"},
    ):
        response = client.get("/api/v1/search", params=params, headers=headers)
        assert response.status_code == 422, params
        assert response.json() == {"detail": "Invalid request"}
    assert client.get("/api/v1/search", params={"q": "a\x00b"}, headers=headers).status_code == 422
    # Operators alone or stopwords only yield an empty query, not an error.
    assert search(client, headers, q="the")["results"] == []
    assert search(client, headers, q="-")["results"] == []
    assert search(client, headers, q='"')["results"] == []


def test_item_list_filters_and_cursor_pagination(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    ids = [capture(client, headers, content=f"entry {i}") for i in range(5)]
    url = client.post(
        "/api/v1/capture/url", json={"url": "https://example.org/"}, headers=headers
    ).json()["id"]
    with connection(settings) as conn:
        conn.execute("UPDATE item SET deleted_at = now() WHERE id = %s", (ids[2],))
    page = client.get("/api/v1/items", headers=headers, params={"limit": 3}).json()
    assert [row["id"] for row in page["items"]] == [url, ids[4], ids[3]]
    assert page["next_cursor"] == ids[3]
    rest = client.get(
        "/api/v1/items", headers=headers, params={"limit": 3, "cursor": page["next_cursor"]}
    ).json()
    assert [row["id"] for row in rest["items"]] == [ids[1], ids[0]]
    assert rest["next_cursor"] is None
    trash = client.get("/api/v1/items", headers=headers, params={"trashed": "true"}).json()
    assert [row["id"] for row in trash["items"]] == [ids[2]]
    assert trash["items"][0]["deleted_at"] is not None
    assert [
        row["id"] for row in client.get("/api/v1/items?kind=url", headers=headers).json()["items"]
    ] == [url]
    queued = client.get("/api/v1/items?status=queued", headers=headers).json()["items"]
    assert len(queued) == 5
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    assert client.get("/api/v1/items?status=queued", headers=headers).json()["items"] == []
    window = client.get(
        "/api/v1/items",
        headers=headers,
        params={"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T00:00:00Z"},
    ).json()
    assert window["items"] == []
    assert client.get("/api/v1/items").status_code == 401
    assert client.get("/api/v1/items?cursor=nope", headers=headers).status_code == 422
