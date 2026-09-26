import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from bag.api import create_app
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.jobs import claim, finish
from bag.processors import PROCESSORS, Outcome, ProcessingItem, ProcessorError
from bag.storage import BlobStorage, FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


@pytest.fixture
def worker(settings: Settings) -> Worker:
    return Worker(settings, FileSystemStorage(settings.storage_path))


def drain(worker: Worker, limit: int = 100) -> int:
    count = 0
    while worker.run_once():
        count += 1
        assert count <= limit, "queue did not drain"
    return count


def runs(client: TestClient, headers: dict[str, str], item_id: str) -> dict[str, dict[str, Any]]:
    response = client.get(f"/api/v1/items/{item_id}/processing", headers=headers)
    assert response.status_code == 200, response.text
    return {row["processor"]: row for row in response.json()}


def test_capture_enqueues_transactionally_and_worker_enriches_text(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
) -> None:
    content = "Grüße aus der Tasche.\n<script>untrusted</script>"
    saved = client.post("/api/v1/capture/text", json={"content": content}, headers=headers).json()
    assert saved["processing_status"] == "queued"
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "queued" and item["extracted_text"] is None
    pending = runs(client, headers, saved["id"])
    assert set(pending) == set(PROCESSORS)
    assert all(r["status"] == "pending" and r["attempts"] == 0 for r in pending.values())

    assert drain(worker) == len(PROCESSORS)
    assert worker.run_once() is False
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "ready"
    assert item["extracted_text"] == item["content"] == content
    assert item["mime_type"] == "text/plain" and item["updated_at"] > item["created_at"]
    done = runs(client, headers, saved["id"])
    for run in done.values():
        assert run["status"] == "succeeded" and run["attempts"] == 1
        assert run["last_error"] is None and run["started_at"] and run["finished_at"]
    with connection(settings) as conn:
        jobs = conn.execute("SELECT status, lease_expires_at, attempts FROM job").fetchall()
        assert jobs and all(
            j["status"] == "succeeded" and j["lease_expires_at"] is None and j["attempts"] == 1
            for j in jobs
        )
        match = conn.execute(
            "SELECT search_vector @@ plainto_tsquery('simple', 'Grüße') AS hit FROM item"
        ).fetchone()
        assert match == {"hit": True}


def test_file_kinds_text_files_urls_and_truncation(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
) -> None:
    def upload(name: str, data: bytes, claimed: str = "image/png") -> str:
        response = client.post(
            "/api/v1/capture/file", headers=headers, files={"file": (name, data, claimed)}
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    pdf = upload("x.png", PDF)
    text = upload("notes.bin", "Gedanken über Wayland 💼\r\n\tTab".encode())
    binary = upload("junk.txt", b"\x00\x01\x02\xff" * 10, "text/plain")
    long = upload("long.txt", b"abc " * 300_000)
    url = client.post(
        "/api/v1/capture/url", headers=headers, json={"url": "http://127.0.0.1/private"}
    ).json()["id"]
    drain(worker)

    item = client.get(f"/api/v1/items/{pdf}", headers=headers).json()
    assert item["kind"] == "document" and item["mime_type"] == "application/pdf"
    assert item["processing_status"] == "ready" and item["extracted_text"] is None
    assert runs(client, headers, pdf)["text_extract"]["status"] == "skipped"

    item = client.get(f"/api/v1/items/{text}", headers=headers).json()
    assert item["kind"] == "file" and item["mime_type"] == "text/plain"
    assert item["extracted_text"] == "Gedanken über Wayland 💼\r\n\tTab"
    assert item["processing_status"] == "ready"

    item = client.get(f"/api/v1/items/{binary}", headers=headers).json()
    assert item["mime_type"] == "application/octet-stream" and item["extracted_text"] is None
    assert item["processing_status"] == "ready"

    item = client.get(f"/api/v1/items/{long}", headers=headers).json()
    assert item["extracted_text"] is not None and len(item["extracted_text"]) < 1_200_000
    with connection(settings) as conn:
        row = conn.execute("SELECT metadata FROM item WHERE id = %s", (long,)).fetchone()
        assert row is not None and row["metadata"]["text_extract"]["truncated"] is True

    item = client.get(f"/api/v1/items/{url}", headers=headers).json()
    assert item["mime_type"] is None and item["processing_status"] == "ready"
    assert all(r["status"] == "skipped" for r in runs(client, headers, url).values())
    assert client.get(f"/api/v1/items/{pdf}/content", headers=headers).content == PDF


def test_failures_retry_with_bounds_and_preserve_originals(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    def crash(item: ProcessingItem, storage: BlobStorage) -> Outcome:
        raise RuntimeError("private detail must not be stored")

    def permanent(item: ProcessingItem, storage: BlobStorage) -> Outcome:
        raise ProcessorError("Unsupported", retryable=False)

    storage = FileSystemStorage(settings.storage_path)
    flaky = Worker(settings, storage, {**PROCESSORS, "text_extract": crash})
    saved = client.post("/api/v1/capture/text", json={"content": "keep"}, headers=headers).json()
    assert drain(flaky, limit=2 + settings.job_max_attempts) == 1 + settings.job_max_attempts
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "partial"
    assert item["content"] == "keep" and item["extracted_text"] is None
    result = runs(client, headers, saved["id"])
    assert result["mime_detect"]["status"] == "succeeded"
    failed = result["text_extract"]
    assert failed["status"] == "failed" and failed["attempts"] == settings.job_max_attempts
    assert failed["last_error"] == "Unexpected RuntimeError"
    with connection(settings) as conn:
        job = conn.execute(
            "SELECT status, attempts FROM job WHERE item_id = %s AND processor = 'text_extract'",
            (saved["id"],),
        ).fetchone()
        assert job == {"status": "failed", "attempts": settings.job_max_attempts}

    broken = Worker(settings, storage, {"mime_detect": permanent, "text_extract": permanent})
    saved = client.post("/api/v1/capture/text", json={"content": "keep 2"}, headers=headers).json()
    assert drain(broken) == 2
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "failed" and item["content"] == "keep 2"
    assert all(
        r["status"] == "failed" and r["attempts"] == 1 and r["last_error"] == "Unsupported"
        for r in runs(client, headers, saved["id"]).values()
    )


def test_retry_backoff_delays_requeued_jobs(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    def crash(item: ProcessingItem, storage: BlobStorage) -> Outcome:
        raise ProcessorError("Temporarily unavailable")

    slow = settings.model_copy(update={"job_retry_seconds": 60.0})
    worker = Worker(
        slow, FileSystemStorage(settings.storage_path), {**PROCESSORS, "mime_detect": crash}
    )
    saved = client.post("/api/v1/capture/text", json={"content": "later"}, headers=headers).json()
    assert drain(worker) == 2
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "processing"
    run = runs(client, headers, saved["id"])["mime_detect"]
    assert run["status"] == "pending" and run["attempts"] == 1
    assert run["last_error"] == "Temporarily unavailable"
    with connection(settings) as conn:
        job = conn.execute(
            "SELECT status, run_after > now() + interval '30 seconds' AS delayed FROM job "
            "WHERE item_id = %s AND processor = 'mime_detect'",
            (saved["id"],),
        ).fetchone()
        assert job == {"status": "queued", "delayed": True}


def test_lease_expiry_recovery_and_stale_results_are_discarded(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "lease"}, headers=headers).json()
    with connection(settings) as conn:
        first = claim(conn, "crashed-worker", settings.job_lease_seconds)
        assert first is not None
    assert (
        client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()["processing_status"]
        == "processing"
    )
    # A live lease is not stolen; an expired one is recovered.
    with connection(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM job WHERE status = 'queued'").fetchone() == {
            "n": 1
        }
    assert drain(worker) == 1
    assert runs(client, headers, saved["id"])[first.processor]["status"] == "running"
    with connection(settings) as conn:
        conn.execute(
            "UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (first.id,),
        )
    assert drain(worker) == 1
    recovered = runs(client, headers, saved["id"])[first.processor]
    assert recovered["status"] == "succeeded" and recovered["attempts"] == 2
    with connection(settings) as conn:
        stale = Outcome("succeeded", {"extracted_text": "stale result"})
        assert finish(conn, settings, "crashed-worker", first, stale) is False
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "ready" and item["extracted_text"] != "stale result"

    # Repeated crashes are bounded: the reclaim after the final attempt fails the run.
    saved = client.post("/api/v1/capture/text", json={"content": "crashy"}, headers=headers).json()
    for _ in range(settings.job_max_attempts):
        with connection(settings) as conn:
            job = claim(conn, "crashed-worker", 1)
            assert job is not None and job.processor == "mime_detect"
            conn.execute(
                "UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (job.id,),
            )
    assert drain(worker) == 2
    result = runs(client, headers, saved["id"])
    assert result["mime_detect"]["status"] == "failed"
    assert result["mime_detect"]["last_error"] == "Lease expired after the final attempt"
    assert result["text_extract"]["status"] == "succeeded"
    assert (
        client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()["processing_status"]
        == "partial"
    )


def test_concurrent_workers_run_every_job_exactly_once(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    calls: list[str] = []
    lock = threading.Lock()

    def counting(name: str) -> Any:
        inner = PROCESSORS[name]

        def processor(item: ProcessingItem, storage: BlobStorage) -> Outcome:
            with lock:
                calls.append(f"{item.id}:{name}")
            return inner(item, storage)

        return processor

    processors = {name: counting(name) for name in PROCESSORS}
    ids = [
        client.post("/api/v1/capture/text", json={"content": f"n{i}"}, headers=headers).json()["id"]
        for i in range(10)
    ]
    storage = FileSystemStorage(settings.storage_path)
    workers = [Worker(settings, storage, processors) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(drain, workers))
    assert sum(counts) == len(ids) * len(PROCESSORS)
    assert len(calls) == len(set(calls)) == len(ids) * len(PROCESSORS)
    with connection(settings) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM job WHERE status <> 'succeeded' OR attempts <> 1"
        ).fetchone() == {"n": 0}
        assert conn.execute(
            "SELECT count(*) AS n FROM item WHERE processing_status <> 'ready'"
        ).fetchone() == {"n": 0}


def test_search_vector_limit_fails_permanently_without_data_loss(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
) -> None:
    data = " ".join(f"w{i}" for i in range(131_000)).encode()
    saved = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": ("words.txt", data)}
    ).json()
    assert drain(worker) == 2
    result = runs(client, headers, saved["id"])
    assert result["mime_detect"]["status"] == "succeeded"
    assert result["text_extract"]["status"] == "failed"
    assert result["text_extract"]["attempts"] == 1
    assert result["text_extract"]["last_error"] == "Result exceeds a database limit"
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["processing_status"] == "partial" and item["extracted_text"] is None
    assert client.get(f"/api/v1/items/{saved['id']}/content", headers=headers).content == data


def test_processing_endpoint_scoping_and_worker_readiness(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    path = f"/api/v1/items/{saved['id']}/processing"
    assert client.get(path).status_code == 401
    assert client.get(f"/api/v1/items/{uuid7()}/processing", headers=headers).status_code == 404
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 404
    with connection(settings) as conn:
        conn.execute("UPDATE item SET deleted_at = now() WHERE id = %s", (saved["id"],))
    assert client.get(path, headers=headers).status_code == 404
    with TestClient(create_app(settings, worker=True, worker_alive=lambda: False)) as dead:
        assert dead.get("/ready").status_code == 503
    with TestClient(create_app(settings, worker=True, worker_alive=lambda: True)) as alive:
        assert alive.get("/ready").status_code == 200
        assert alive.get(path, headers=headers).status_code == 404
