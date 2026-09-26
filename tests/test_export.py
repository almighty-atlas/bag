import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from bag.cli import main
from bag.config import Settings
from bag.db import connection
from bag.export import ExportError, export_bag
from bag.ids import uuid7
from bag.importer import import_bag
from bag.processors import PROCESSORS
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import PDF, drain

pytestmark = pytest.mark.integration


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_writes_originals_and_metadata(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    text = client.post(
        "/api/v1/capture/text",
        json={"content": "Grüße 💼\n<b>", "user_note": "Notiz"},
        headers=headers,
    ).json()
    pdf = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("Bücher.pdf", PDF)}
    ).json()
    twin = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("copy.pdf", PDF)}
    ).json()
    drain(Worker(settings, storage))
    client.patch(
        f"/api/v1/items/{pdf['id']}",
        json={"tags": ["lesen"], "collections": ["Umzug"], "title": "Bücherliste"},
        headers=headers,
    )
    client.delete(f"/api/v1/items/{twin['id']}", headers=headers)

    target = tmp_path / "export"
    counts = export_bag(settings, storage, target)
    assert counts == {
        "items": 3,
        "blobs": 2,
        "tags": 1,
        "collections": 1,
        "relations": 1,
        "processing_runs": 3 * len(PROCESSORS),
        "objects": 1,
    }
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["format"] == "bag-export" and manifest["version"] == 1
    assert manifest["counts"] == counts
    items = {row["id"]: row for row in read_jsonl(target / "items.jsonl")}
    assert items[text["id"]]["content"] == "Grüße 💼\n<b>"
    assert items[text["id"]]["user_note"] == "Notiz" and items[text["id"]]["language"] is None
    assert items[pdf["id"]]["title"] == "Bücherliste"
    assert items[pdf["id"]]["original_filename"] == "Bücher.pdf"
    assert items[twin["id"]]["deleted_at"] is not None  # trash is exported too
    assert "search_vector" not in items[text["id"]]
    blobs = read_jsonl(target / "blobs.jsonl")
    digest = hashlib.sha256(PDF).hexdigest()
    assert {row["sha256"] for row in blobs} == {digest}
    assert (target / "objects" / digest).read_bytes() == PDF
    assert len(list((target / "objects").iterdir())) == 1
    tags = read_jsonl(target / "tags.jsonl")
    assert tags[0]["name"] == "lesen" and tags[0]["items"][0]["item_id"] == pdf["id"]
    assert tags[0]["items"][0]["created_by"] == "user"
    collections = read_jsonl(target / "collections.jsonl")
    assert collections[0]["name"] == "Umzug" and collections[0]["items"] == [pdf["id"]]
    relations = read_jsonl(target / "relations.jsonl")
    assert relations[0]["source_item_id"] == twin["id"]
    assert relations[0]["target_item_id"] == pdf["id"]
    assert relations[0]["relation_type"] == "duplicate_of"
    runs = read_jsonl(target / "processing_runs.jsonl")
    assert {row["processor"] for row in runs} == set(PROCESSORS)

    # A non-empty target is refused; a corrupt original aborts instead of exporting junk.
    with pytest.raises(ExportError):
        export_bag(settings, storage, target)
    monkeypatch.setenv("BAG_STORAGE_PATH", str(settings.storage_path))
    monkeypatch.setattr("sys.argv", ["bag", "export", str(tmp_path / "second")])
    main()
    assert json.loads(capsys.readouterr().out)["objects"] == 1
    stored = next(p for p in settings.storage_path.rglob("*") if p.is_file())
    stored.write_bytes(b"corrupted")
    monkeypatch.setattr("sys.argv", ["bag", "export", str(tmp_path / "third")])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert not (tmp_path / "third" / "manifest.json").exists()


def test_import_round_trip_is_idempotent_and_verifies_objects(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    pdf = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("Bücher.pdf", PDF)}
    ).json()
    text = client.post(
        "/api/v1/capture/text",
        json={"content": "Grüße 💼", "user_note": "Notiz", "client_capture_id": str(uuid4())},
        headers=headers,
    ).json()
    drain(Worker(settings, storage))
    client.patch(
        f"/api/v1/items/{pdf['id']}",
        json={"tags": ["lesen"], "collections": ["Umzug"], "title": "Bücherliste"},
        headers=headers,
    )
    client.delete(f"/api/v1/items/{text['id']}", headers=headers)
    before = {
        table: rows(settings, table)
        for table in (
            "item",
            "blob",
            "tag",
            "item_tag",
            "collection",
            "item_collection",
            "relation",
            "processing_run",
        )
    }
    target = tmp_path / "export"
    export_bag(settings, storage, target)

    # Wipe everything but the user, then import into the same owner.
    with connection(settings) as conn:
        for table in (
            "job",
            "processing_run",
            "relation",
            "item_tag",
            "item_collection",
            "blob",
            "item",
            "tag",
            "collection",
        ):
            conn.execute(f"DELETE FROM {table}")
    for path in list(settings.storage_path.rglob("*")):
        if path.is_file():
            path.unlink()
    counts = import_bag(settings, storage, target)
    assert counts == {
        "items": 2,
        "blobs": 1,
        "tags": 1,
        "collections": 1,
        "relations": 0,
        "processing_runs": 2 * len(PROCESSORS),
        "objects": 1,
    }
    for table, expected in before.items():
        assert rows(settings, table) == expected, table
    restored = client.get(f"/api/v1/items/{pdf['id']}", headers=headers).json()
    assert restored["title"] == "Bücherliste" and restored["tags"] == ["lesen"]
    assert restored["collections"] == ["Umzug"] and restored["processing_status"] == "ready"
    assert client.get(f"/api/v1/items/{pdf['id']}/content", headers=headers).content == PDF
    assert (
        client.get(f"/api/v1/items/{text['id']}", headers=headers).status_code == 404
    )  # still trashed
    assert [
        row["id"]
        for row in client.get("/api/v1/items?trashed=true", headers=headers).json()["items"]
    ] == [text["id"]]
    assert client.get("/api/v1/search?q=Bücherliste", headers=headers).json()["results"]
    # Importing again changes nothing; a replay of the original capture key still resolves.
    assert import_bag(settings, storage, target) == dict.fromkeys(counts, 0) | {"objects": 1}
    for table, expected in before.items():
        assert rows(settings, table) == expected, table

    monkeypatch.setenv("BAG_STORAGE_PATH", str(settings.storage_path))
    monkeypatch.setattr("sys.argv", ["bag", "import", str(target)])
    main()
    assert json.loads(capsys.readouterr().out)["items"] == 0
    # A corrupt object aborts before any row is written; a foreign ID is refused.
    (target / "objects" / hashlib.sha256(PDF).hexdigest()).write_bytes(b"tampered")
    with connection(settings) as conn:
        conn.execute("DELETE FROM job; DELETE FROM processing_run; DELETE FROM relation")
        conn.execute(
            "DELETE FROM item_tag; DELETE FROM item_collection; DELETE FROM blob; DELETE FROM item"
        )
    with pytest.raises(ExportError, match="corrupt"):
        import_bag(settings, storage, target)
    assert rows(settings, "item") == []
    (target / "objects" / hashlib.sha256(PDF).hexdigest()).write_bytes(PDF)
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, processing_status) "
            "VALUES (%s, %s, 'text', 'api', 'ready')",
            (pdf["id"], other),
        )
    owner = before["item"][0]["owner_id"]
    with pytest.raises(ExportError, match="another owner"):
        import_bag(settings, storage, target, owner)
    (target / "manifest.json").write_text('{"format": "bag-export", "version": 99}')
    with pytest.raises(ExportError, match="version"):
        import_bag(settings, storage, target, owner)


def rows(settings: Settings, table: str) -> list[dict[str, Any]]:
    # Assignments are relationships: the export carries them without their own IDs/timestamps.
    volatile = {"search_vector"} | (
        {"id", "created_at"} if table in {"item_tag", "item_collection"} else set()
    )
    with connection(settings) as conn:
        found = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    return sorted(
        ({k: v for k, v in row.items() if k not in volatile} for row in found),
        key=lambda row: str(sorted(row.items())),
    )


def test_export_scoping(
    settings: Settings, client: TestClient, headers: dict[str, str], tmp_path: Path
) -> None:
    client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers)
    with connection(settings) as conn:
        conn.execute(
            """INSERT INTO "user" (id, display_name) VALUES (gen_random_uuid(), 'other')"""
        )
        other = conn.execute("""SELECT id FROM "user" WHERE display_name = 'other'""").fetchone()
        assert other is not None
    storage = FileSystemStorage(settings.storage_path)
    with pytest.raises(Exception, match="Multiple owners"):
        export_bag(settings, storage, tmp_path / "ambiguous")
    counts = export_bag(settings, storage, tmp_path / "other", other["id"])
    assert counts["items"] == 0
    assert (tmp_path / "other" / "items.jsonl").read_text() == ""
