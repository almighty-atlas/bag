import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from bag.cli import main
from bag.config import Settings
from bag.db import connection
from bag.export import ExportError, export_bag
from bag.processors import PROCESSORS
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


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
