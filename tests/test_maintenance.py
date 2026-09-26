import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from bag.cli import main
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.maintenance import collect_garbage, purge_items
from bag.processors import PROCESSORS
from bag.storage import FileSystemStorage, StorageError
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain

PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


def objects(root: Path) -> set[str]:
    return {p.name for p in root.rglob("*") if p.is_file()}


def age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def test_scan_and_remove_are_confined_to_storage_layout(tmp_path: Path) -> None:
    storage = FileSystemStorage(tmp_path)
    assert list(storage.scan()) == []
    blob = storage.put(io.BytesIO(PDF), 1000)
    (tmp_path / ".upload-crash").write_bytes(b"partial")
    (tmp_path / "unrelated.txt").write_bytes(b"not ours")
    (tmp_path / "ab" / "cd").mkdir(parents=True)
    (tmp_path / "ab" / "cd" / "not-a-hash").write_bytes(b"stray")
    found = {obj.storage_key: obj for obj in storage.scan()}
    assert set(found) == {None, blob.storage_key}
    assert found[blob.storage_key].size_bytes == len(PDF)
    stray = found[blob.storage_key]
    with pytest.raises(StorageError):
        storage.remove(type(stray)(stray.storage_key, tmp_path / "unrelated.txt", 1, 0.0))
    assert (tmp_path / "unrelated.txt").exists()
    storage.remove(found[None])
    storage.remove(stray)
    assert objects(tmp_path) == {"unrelated.txt", "not-a-hash"}


@pytest.mark.integration
def test_purge_respects_retention_and_removes_rows_not_objects(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    first = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("a.pdf", PDF)}
    ).json()["id"]
    second = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("b.pdf", PDF)}
    ).json()["id"]
    text = client.post("/api/v1/capture/text", headers=headers, json={"content": "keep"}).json()
    drain(Worker(settings, storage))
    for item_id in (first, second):
        assert client.delete(f"/api/v1/items/{item_id}", headers=headers).status_code == 204
    with connection(settings) as conn:
        conn.execute(
            "UPDATE item SET deleted_at = now() - interval '31 days' WHERE id = %s", (first,)
        )
    with connection(settings) as conn:
        # One finished job is old enough to prune; running and recent ones stay.
        conn.execute(
            "UPDATE job SET updated_at = now() - interval '8 days' WHERE id = "
            "(SELECT id FROM job WHERE item_id = %s AND status = 'succeeded' LIMIT 1)",
            (text["id"],),
        )
        conn.execute(
            "UPDATE job SET status = 'running', lease_expires_at = now() + interval '1 hour', "
            "updated_at = now() - interval '8 days' WHERE id = "
            "(SELECT id FROM job WHERE item_id = %s AND status = 'succeeded' LIMIT 1)",
            (text["id"],),
        )
        total = conn.execute("SELECT count(*) AS n FROM job").fetchone()
        assert total is not None
    assert purge_items(settings, dry_run=True) == {"expired": 1, "purged": 0, "jobs_pruned": 1}
    assert client.get("/api/v1/items?trashed=true", headers=headers).json()["items"]
    assert purge_items(settings) == {"expired": 1, "purged": 1, "jobs_pruned": 1}
    assert purge_items(settings) == {"expired": 0, "purged": 0, "jobs_pruned": 0}
    with connection(settings) as conn:
        remaining = conn.execute("SELECT count(*) AS n FROM job").fetchone()
        # The pruned job plus the purged item's own jobs are gone; the running one stays.
        assert remaining == {"n": total["n"] - 1 - len(PROCESSORS)}
        assert conn.execute(
            "SELECT count(*) AS n FROM job WHERE status = 'running'"
        ).fetchone() == {"n": 1}
    trash = client.get("/api/v1/items?trashed=true", headers=headers).json()["items"]
    assert [row["id"] for row in trash] == [second]
    assert client.post(f"/api/v1/items/{first}/restore", headers=headers).status_code == 404
    with connection(settings) as conn:
        for table, column in (
            ("item", "id"),
            ("blob", "item_id"),
            ("job", "item_id"),
            ("processing_run", "item_id"),
            ("relation", "source_item_id"),
        ):
            count = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE {column} = %s", (first,)
            ).fetchone()
            assert count == {"n": 0}, table
    # The shared object still serves the surviving duplicate after restore.
    assert client.post(f"/api/v1/items/{second}/restore", headers=headers).status_code == 200
    assert client.get(f"/api/v1/items/{second}/content", headers=headers).content == PDF
    assert client.get(f"/api/v1/items/{text['id']}", headers=headers).status_code == 200
    assert len(objects(settings.storage_path)) == 1

    # Retention zero purges everything trashed; the CLI reports counts.
    assert client.delete(f"/api/v1/items/{second}", headers=headers).status_code == 204
    monkeypatch.setattr("sys.argv", ["bag", "purge", "--retention-days", "0"])
    main()
    assert json.loads(capsys.readouterr().out) == {"expired": 1, "purged": 1, "jobs_pruned": 0}
    assert client.get("/api/v1/items?trashed=true", headers=headers).json()["items"] == []
    assert len(objects(settings.storage_path)) == 1


@pytest.mark.integration
def test_gc_keeps_referenced_objects_and_waits_for_captures(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FileSystemStorage(settings.storage_path)
    live = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("live.pdf", PDF)}
    ).json()["id"]
    orphan = storage.put(io.BytesIO(b"orphaned original"), 1000)
    fresh = storage.put(io.BytesIO(b"fresh orphan"), 1000)
    temp = settings.storage_path / ".upload-crashed"
    temp.write_bytes(b"partial upload")
    for name in (orphan.storage_key, temp.name):
        age(settings.storage_path / name, 2)
    for path in settings.storage_path.rglob("*"):
        if path.is_file() and path.name == live_hash(settings, live):
            age(path, 2)
    assert collect_garbage(settings, storage, dry_run=True)["removed"] == 2
    assert objects(settings.storage_path) == {
        live_hash(settings, live),
        orphan.sha256,
        fresh.sha256,
        temp.name,
    }
    result = collect_garbage(settings, storage)
    assert result["removed"] == 2 and result["kept"] == 1
    assert result["freed_bytes"] == len(b"orphaned original") + len(b"partial upload")
    assert objects(settings.storage_path) == {live_hash(settings, live), fresh.sha256}
    assert client.get(f"/api/v1/items/{live}/content", headers=headers).content == PDF

    # A capture that published its object under the owner lock blocks GC until commit.
    payload = b"published before commit"
    started = threading.Event()
    with connection(settings) as holder:
        owner = holder.execute('SELECT id FROM "user" FOR UPDATE').fetchone()
        assert owner is not None
        pending = storage.put(io.BytesIO(payload), 1000)
        age(settings.storage_path / pending.storage_key, 2)
        outcome: dict[str, int] = {}

        def run_gc() -> None:
            started.set()
            outcome.update(collect_garbage(settings, storage))

        thread = threading.Thread(target=run_gc)
        thread.start()
        started.wait()
        time.sleep(0.5)
        assert thread.is_alive(), "GC must wait for the capture lock"
        assert (settings.storage_path / pending.storage_key).exists()
        item_id = uuid7()
        holder.execute(
            "INSERT INTO item (id, owner_id, kind, source, content_hash, processing_status) "
            "VALUES (%s, %s, 'file', 'api', %s, 'ready')",
            (item_id, owner["id"], pending.sha256),
        )
        holder.execute(
            "INSERT INTO blob (id, owner_id, item_id, role, storage_key, sha256, size_bytes, "
            "mime_type) VALUES (%s, %s, %s, 'original', %s, %s, %s, 'application/octet-stream')",
            (uuid7(), owner["id"], item_id, pending.storage_key, pending.sha256, len(payload)),
        )
    thread.join(timeout=10)
    assert outcome["removed"] == 0 and outcome["kept"] >= 1
    assert (settings.storage_path / pending.storage_key).read_bytes() == payload

    monkeypatch.setenv("BAG_STORAGE_PATH", str(settings.storage_path))
    monkeypatch.setattr("sys.argv", ["bag", "gc", "--min-age-hours", "0", "--dry-run"])
    main()
    assert json.loads(capsys.readouterr().out)["removed"] == 1  # only the fresh orphan


def live_hash(settings: Settings, item_id: str) -> str:
    with connection(settings) as conn:
        row = conn.execute("SELECT sha256 FROM blob WHERE item_id = %s", (item_id,)).fetchone()
        assert row is not None
        return str(row["sha256"])
