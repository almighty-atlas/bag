from typing import Any

import pytest
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import JPEG, PDF, PNG, drain
from test_thumbnails import (
    SVG,
    object_path,
    thumbnail_rows,
    thumbnail_url,
    upload,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(settings: Settings) -> FileSystemStorage:
    return FileSystemStorage(settings.storage_path)


@pytest.fixture
def worker(settings: Settings, storage: FileSystemStorage) -> Worker:
    return Worker(settings, storage)


def detail_flag(client: TestClient, headers: dict[str, str], item_id: str) -> bool:
    response = client.get(f"/api/v1/items/{item_id}", headers=headers)
    assert response.status_code == 200
    return bool(response.json()["thumbnail_available"])


def flags(client: TestClient, headers: dict[str, str], **params: str) -> dict[str, bool]:
    response = client.get("/api/v1/items", headers=headers, params={"limit": "100", **params})
    assert response.status_code == 200
    return {row["id"]: row["thumbnail_available"] for row in response.json()["items"]}


def test_listing_summaries_say_whether_a_thumbnail_can_be_tried(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    image = upload(client, headers, "a.png", PNG)
    text = client.post(
        "/api/v1/capture/text", headers=headers, json={"content": "nur Text", "source": "web"}
    ).json()["id"]
    document = upload(client, headers, "b.pdf", PDF, "application/pdf")
    svg = upload(client, headers, "logo.svg", SVG, "image/svg+xml")
    # Nothing is rendered yet: every card falls back, with no request for an absent thumbnail.
    assert flags(client, headers) == {image: False, text: False, document: False, svg: False}
    assert (
        client.get(f"/api/v1/items/{image}", headers=headers).json()["thumbnail_available"] is False
    )

    drain(worker)
    assert flags(client, headers) == {image: True, text: False, document: False, svg: False}
    assert (
        client.get(f"/api/v1/items/{image}", headers=headers).json()["thumbnail_available"] is True
    )
    assert (
        client.get(f"/api/v1/items/{svg}", headers=headers).json()["thumbnail_available"] is False
    )
    assert client.get(thumbnail_url(image), headers=headers).status_code == 200
    assert client.get(thumbnail_url(svg), headers=headers).status_code == 404


def test_the_flag_reaches_search_results_and_every_page(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    images = [upload(client, headers, f"Wohnzimmer-{n}.png", PNG) for n in range(3)]
    plain = client.post(
        "/api/v1/capture/text",
        headers=headers,
        json={"content": "Wohnzimmer Notiz", "source": "web"},
    ).json()["id"]
    drain(worker)

    seen: dict[str, bool] = {}
    offset: int | None = 0
    while offset is not None:
        page = client.get(
            "/api/v1/search",
            headers=headers,
            params={"q": "wohnzimmer", "limit": "2", "offset": str(offset)},
        ).json()
        assert all("thumbnail_available" in row for row in page["results"])
        seen |= {row["id"]: row["thumbnail_available"] for row in page["results"]}
        offset = page["next_offset"]
    assert seen == {**{item: True for item in images}, plain: False}

    first = client.get("/api/v1/items", headers=headers, params={"limit": "2"}).json()
    rest = client.get(
        "/api/v1/items", headers=headers, params={"limit": "2", "cursor": first["next_cursor"]}
    ).json()
    assert {row["id"]: row["thumbnail_available"] for row in first["items"] + rest["items"]} == {
        **{item: True for item in images},
        plain: False,
    }


def test_the_flag_follows_edits_trash_and_restore_without_trusting_the_blob(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    edited = client.patch(f"/api/v1/items/{item_id}", headers=headers, json={"title": "Lampe"})
    assert edited.json()["thumbnail_available"] is True

    client.delete(f"/api/v1/items/{item_id}", headers=headers)
    trashed = client.get("/api/v1/items", headers=headers, params={"trashed": "true"}).json()
    # The row still exists, but the endpoint refuses trashed items: clients must not ask.
    assert [row["thumbnail_available"] for row in trashed["items"]] == [True]
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    restored = client.post(f"/api/v1/items/{item_id}/restore", headers=headers)
    assert restored.json()["thumbnail_available"] is True

    # A hint, not a guarantee: a missing object still answers 404 and the item stays intact.
    (row,) = thumbnail_rows(settings, item_id)
    object_path(settings, row["sha256"]).unlink()
    assert flags(client, headers) == {item_id: True}
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    assert client.get(f"/api/v1/items/{item_id}", headers=headers).status_code == 200


def test_rows_the_endpoint_would_refuse_are_not_advertised(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.jpg", JPEG, "image/jpeg")
    drain(worker)
    (row,) = thumbnail_rows(settings, item_id)
    for column, value in (("mime_type", "image/svg+xml"), ("size_bytes", 256 * 1024 + 1)):
        with connection(settings) as conn:
            conn.execute(f"UPDATE blob SET {column} = %s WHERE id = %s", (value, row["id"]))
        assert flags(client, headers) == {item_id: False}
        assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
        with connection(settings) as conn:
            conn.execute(f"UPDATE blob SET {column} = %s WHERE id = %s", (row[column], row["id"]))
        assert flags(client, headers) == {item_id: True}


def test_the_flag_never_exposes_another_owners_thumbnail(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    mine = upload(client, headers, "a.png", PNG)
    drain(worker)
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    theirs: dict[str, Any] = {"Authorization": "Bearer other"}
    assert flags(client, theirs) == {}
    own = upload(client, theirs, "b.png", PNG)
    assert flags(client, theirs) == {own: False}
    assert flags(client, headers) == {mine: True}
    drain(worker)
    assert flags(client, theirs) == {own: True}
    assert flags(client, headers) == {mine: True}
