from typing import Any

import pytest
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.excerpt import EXCERPT_CHARS
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import PDF, drain

pytestmark = pytest.mark.integration


def capture(client: TestClient, headers: dict[str, str], **payload: Any) -> str:
    response = client.post("/api/v1/capture/text", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def feed(client: TestClient, headers: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    response = client.get("/api/v1/items", params=params, headers=headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in rows}


def test_text_items_carry_a_bounded_plain_excerpt(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    plain = capture(client, headers, content="Eine Idee für den Keller")
    noted = capture(client, headers, content="Kabelführung", user_note="Schreibtisch")
    multiline = capture(client, headers, content="\n\n# Einkauf\n\n- Äpfel\n- 🍐 Birnen\r\n\n")
    hostile = capture(client, headers, content='<img src=x onerror="alert(1)"> **fett**')
    blank = capture(client, headers, content="  \n\t\n  ")
    big = capture(client, headers, content="Wort " * 150_000)
    url = client.post(
        "/api/v1/capture/url", json={"url": "https://example.org/a"}, headers=headers
    ).json()["id"]
    file = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("beleg.pdf", PDF)}
    ).json()["id"]

    response = client.get("/api/v1/items", headers=headers)
    # Large originals never travel: the whole page stays small.
    assert len(response.content) < 8_000
    rows = by_id(response.json()["items"])
    assert rows[plain]["excerpt"] == "Eine Idee für den Keller"
    assert rows[noted]["excerpt"] == "Kabelführung" and rows[noted]["user_note"] == "Schreibtisch"
    assert rows[multiline]["excerpt"] == "Einkauf Äpfel 🍐 Birnen"
    # Markup is never interpreted; the client renders the string as text.
    assert rows[hostile]["excerpt"] == '<img src=x onerror="alert(1)"> fett'
    assert rows[blank]["excerpt"] is None
    assert rows[big]["excerpt"] is not None
    assert len(rows[big]["excerpt"]) <= EXCERPT_CHARS and rows[big]["excerpt"].endswith("Wort…")
    assert rows[url]["excerpt"] is None and rows[file]["excerpt"] is None
    assert all("content" not in row and "extracted_text" not in row for row in rows.values())

    # A multi-megabyte original still yields a bounded summary.
    with connection(settings) as conn:
        conn.execute("UPDATE item SET content = %s WHERE id = %s", ("Ä" * 6_000_000, plain))
    row = by_id(feed(client, headers))[plain]
    assert row["excerpt"] == "Ä" * (EXCERPT_CHARS - 1) + "…"


def test_excerpt_survives_processing_and_editing_and_leaves_the_original_alone(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = capture(client, headers, content="Das Original bleibt wie es ist.")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    row = by_id(feed(client, headers))[item]
    assert row["excerpt"] == "Das Original bleibt wie es ist." and row["language"] == "de"

    edited = client.patch(
        f"/api/v1/items/{item}", json={"title": "Titel", "user_note": "Notiz"}, headers=headers
    )
    assert edited.status_code == 200
    row = by_id(feed(client, headers))[item]
    assert (row["title"], row["user_note"]) == ("Titel", "Notiz")
    assert row["excerpt"] == "Das Original bleibt wie es ist."
    assert client.get(f"/api/v1/items/{item}", headers=headers).json()["content"] == (
        "Das Original bleibt wie es ist."
    )


def test_excerpt_in_paging_filters_search_and_trash(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    ids = [
        capture(client, headers, content=f"Gedanke Nummer {i} zum Thema Tasche") for i in range(5)
    ]
    url = client.post(
        "/api/v1/capture/url", json={"url": "https://example.org/"}, headers=headers
    ).json()["id"]
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    first = client.get("/api/v1/items", params={"limit": 3}, headers=headers).json()
    rest = client.get(
        "/api/v1/items", params={"limit": 3, "cursor": first["next_cursor"]}, headers=headers
    ).json()
    seen = [row["id"] for row in first["items"] + rest["items"]]
    assert seen == [url, *reversed(ids)] and rest["next_cursor"] is None
    assert [row["excerpt"] for row in first["items"] + rest["items"]] == [
        None,
        *[f"Gedanke Nummer {i} zum Thema Tasche" for i in reversed(range(5))],
    ]
    only_text = feed(client, headers, kind="text")
    assert [row["id"] for row in only_text] == list(reversed(ids))

    found = client.get("/api/v1/search", params={"q": "Tasche"}, headers=headers).json()
    assert {row["id"] for row in found["results"]} == set(ids)
    for row in found["results"]:
        assert row["excerpt"].startswith("Gedanke Nummer") and "«" in row["snippet"]
        assert "«" not in row["excerpt"]

    assert client.delete(f"/api/v1/items/{ids[0]}", headers=headers).status_code == 204
    assert ids[0] not in {row["id"] for row in feed(client, headers)}
    trash = feed(client, headers, trashed="true")
    assert [row["id"] for row in trash] == [ids[0]]
    assert trash[0]["excerpt"] == "Gedanke Nummer 0 zum Thema Tasche"
    trashed_search = client.get(
        "/api/v1/search", params={"q": "Tasche", "trashed": "true"}, headers=headers
    ).json()
    assert [row["id"] for row in trashed_search["results"]] == [ids[0]]
    assert trashed_search["results"][0]["excerpt"] == "Gedanke Nummer 0 zum Thema Tasche"


def test_excerpts_are_owner_scoped_and_need_authentication(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    capture(client, headers, content="geheimer Inhalt über Authentifizierung")
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    other_headers = {"Authorization": "Bearer other"}
    mine = capture(client, other_headers, content="Inhalt des anderen Nutzers")

    assert [row["id"] for row in feed(client, other_headers)] == [mine]
    for params in ({}, {"trashed": "true"}):
        for path, extra in (("/api/v1/items", {}), ("/api/v1/search", {"q": "Inhalt"})):
            body = client.get(path, params={**params, **extra}, headers=other_headers).text
            assert "geheimer" not in body
    assert client.get("/api/v1/items").status_code == 401
    assert client.get("/api/v1/items", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/search", params={"q": "Inhalt"}).status_code == 401
