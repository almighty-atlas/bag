"""Merkliste: ADR-0028, migration 0008_pinned."""

import json
from pathlib import Path

import pytest
from alembic import command
from bag.cli import migration_config
from bag.config import Settings
from bag.db import connection
from bag.export import export_bag
from bag.importer import import_bag
from bag.maintenance import purge_items
from bag.sessions import set_password
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain
from test_search_fields import add_user, found, text

pytestmark = pytest.mark.integration


def pins(client: TestClient, headers: dict[str, str], **params: object) -> list[str]:
    response = client.get("/api/v1/pins", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


def pin(client: TestClient, headers: dict[str, str], item: str) -> dict[str, object]:
    response = client.put(f"/api/v1/items/{item}/pin", headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def unpin(client: TestClient, headers: dict[str, str], item: str) -> dict[str, object]:
    response = client.delete(f"/api/v1/items/{item}/pin", headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def test_pin_and_unpin_are_idempotent_and_ordered_by_pin_time(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    first, second, third = (text(client, headers, f"Eintrag {n}") for n in range(3))
    assert pins(client, headers) == []
    detail = client.get(f"/api/v1/items/{first}", headers=headers).json()
    assert detail["pinned_at"] is None

    pinned = pin(client, headers, second)
    assert pinned["pinned_at"] is not None
    pin(client, headers, first)
    # Pinning again keeps the first pin time, so the order does not change.
    assert pin(client, headers, second)["pinned_at"] == pinned["pinned_at"]
    assert pins(client, headers) == [first, second]
    # Summaries in feed and search carry the state too.
    feed = client.get("/api/v1/items", headers=headers).json()["items"]
    assert {row["id"]: row["pinned_at"] is not None for row in feed} == {
        first: True,
        second: True,
        third: False,
    }
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    hits = client.get("/api/v1/search", params={"q": "eintrag"}, headers=headers).json()
    assert {row["id"] for row in hits["results"] if row["pinned_at"]} == {first, second}

    assert unpin(client, headers, second)["pinned_at"] is None
    assert unpin(client, headers, second)["pinned_at"] is None
    assert pins(client, headers) == [first]
    # Pinning never changes the item itself or creates another one.
    assert client.get(f"/api/v1/items/{second}", headers=headers).json()["content"] == "Eintrag 1"
    assert len(client.get("/api/v1/items", headers=headers).json()["items"]) == 3


def test_pinned_filter_in_listing_and_search(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    kept = text(client, headers, "Rezept Linsensuppe")
    other = text(client, headers, "Rezept Kürbis")
    pin(client, headers, kept)
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    listed = client.get("/api/v1/items", params={"pinned": "true"}, headers=headers).json()
    assert [row["id"] for row in listed["items"]] == [kept]
    unpinned = client.get("/api/v1/items", params={"pinned": "false"}, headers=headers).json()
    assert [row["id"] for row in unpinned["items"]] == [other]
    assert found(client, headers, "rezept", pinned="true") == [kept]
    assert set(found(client, headers, "rezept")) == {kept, other}


def test_pins_are_paged_and_owner_scoped(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    items = [text(client, headers, f"Seite {n}") for n in range(5)]
    for item in items:
        pin(client, headers, item)
    page = client.get("/api/v1/pins", params={"limit": 2}, headers=headers).json()
    assert [row["id"] for row in page["items"]] == items[::-1][:2]
    assert page["next_offset"] == 2
    collected = []
    offset: int | None = 0
    while offset is not None:
        page = client.get(
            "/api/v1/pins", params={"limit": 2, "offset": offset}, headers=headers
        ).json()
        collected += [row["id"] for row in page["items"]]
        offset = page["next_offset"]
    assert collected == items[::-1]

    other = add_user(settings, "other")
    assert pins(client, other) == []
    for method in ("put", "delete"):
        response = client.request(method, f"/api/v1/items/{items[0]}/pin", headers=other)
        assert response.status_code == 404
    assert pins(client, headers)[-1] == items[0]
    missing = "01a1fb3e-59c5-71f6-aa43-69157f98da25"
    assert client.put(f"/api/v1/items/{missing}/pin", headers=headers).status_code == 404
    assert client.put("/api/v1/items/not-a-uuid/pin", headers=headers).status_code == 422
    assert client.get("/api/v1/pins", params={"limit": 0}, headers=headers).status_code == 422
    assert client.get("/api/v1/pins", params={"offset": -1}, headers=headers).status_code == 422


def test_trash_hides_a_pin_and_restore_brings_it_back(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = text(client, headers, "Wichtig")
    pin(client, headers, item)
    assert client.delete(f"/api/v1/items/{item}", headers=headers).status_code == 204
    assert pins(client, headers) == []
    # A trashed item cannot be pinned or unpinned, like every other edit.
    assert client.put(f"/api/v1/items/{item}/pin", headers=headers).status_code == 404
    assert client.delete(f"/api/v1/items/{item}/pin", headers=headers).status_code == 404
    restored = client.post(f"/api/v1/items/{item}/restore", headers=headers).json()
    assert restored["pinned_at"] is not None
    assert pins(client, headers) == [item]
    # Purge removes the item and with it the pin; nothing else holds it.
    client.delete(f"/api/v1/items/{item}", headers=headers)
    with connection(settings) as conn:
        conn.execute("UPDATE item SET deleted_at = now() - interval '400 days'")
    purge_items(settings)
    assert pins(client, headers) == []


def test_web_session_needs_csrf_to_pin(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = text(client, headers, "Per Browser")
    set_password(settings, "atlas", "korrekt-pferd-batterie-heftklammer", None)
    body = {"username": "atlas", "password": "korrekt-pferd-batterie-heftklammer"}
    csrf = {"X-Bag-Csrf": "1"}
    assert client.post("/api/v1/session", json=body, headers=csrf).status_code == 200
    assert client.put(f"/api/v1/items/{item}/pin").status_code == 403
    assert client.put(f"/api/v1/items/{item}/pin", headers=csrf).status_code == 200
    assert client.get("/api/v1/pins").json()["items"][0]["id"] == item
    assert client.delete(f"/api/v1/items/{item}/pin").status_code == 403
    assert client.delete(f"/api/v1/items/{item}/pin", headers=csrf).status_code == 200


def test_export_and_import_keep_pins(
    settings: Settings, client: TestClient, headers: dict[str, str], tmp_path: Path
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    kept = text(client, headers, "Behalten")
    loose = text(client, headers, "Lose")
    pinned_at = pin(client, headers, kept)["pinned_at"]
    target = tmp_path / "export"
    export_bag(settings, storage, target)
    rows = {
        row["id"]: row for row in map(json.loads, (target / "items.jsonl").read_text().splitlines())
    }
    assert rows[kept]["pinned_at"] is not None and rows[loose]["pinned_at"] is None

    # An export from before 0008 has no pinned_at at all: it imports as not pinned.
    legacy = tmp_path / "legacy"
    export_bag(settings, storage, legacy)
    lines = [json.loads(line) for line in (legacy / "items.jsonl").read_text().splitlines()]
    (legacy / "items.jsonl").write_text(
        "".join(
            json.dumps({k: v for k, v in row.items() if k != "pinned_at"}) + "\n" for row in lines
        )
    )
    with connection(settings) as conn:
        for table in ("job", "processing_run", "relation", "blob", "item"):
            conn.execute(f"DELETE FROM {table}")
    import_bag(settings, storage, legacy)
    assert pins(client, headers) == []
    with connection(settings) as conn:
        for table in ("job", "processing_run", "relation", "blob", "item"):
            conn.execute(f"DELETE FROM {table}")
    import_bag(settings, storage, target)
    assert pins(client, headers) == [kept]
    detail = client.get(f"/api/v1/items/{kept}", headers=headers).json()
    assert detail["pinned_at"] == pinned_at


def test_migration_0008_round_trip(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    config = migration_config()
    item = text(client, headers, "Vorher")
    pin(client, headers, item)
    command.downgrade(config, "0007_search_followups")
    try:
        with connection(settings) as conn:
            column = conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'item' AND column_name = 'pinned_at'"
            ).fetchone()
        assert column == {"n": 0}
    finally:
        command.upgrade(config, "head")
    assert pins(client, headers) == []
    pin(client, headers, item)
    assert pins(client, headers) == [item]
