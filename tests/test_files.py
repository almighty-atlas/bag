import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest
from bag.api import create_app
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import StorageError
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


def test_storage_failure_does_not_acknowledge_or_insert(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    with patch("bag.storage.FileSystemStorage.put", side_effect=StorageError("disk full")):
        response = client.post("/api/v1/capture/file", headers=headers, files={"file": ("x", PDF)})
    assert response.status_code == 503
    with connection(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM item").fetchone() == {"n": 0}
        assert conn.execute("SELECT count(*) AS n FROM blob").fetchone() == {"n": 0}


def test_file_original_metadata_and_safe_download(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    filename = "../Gedanken 💼.html"
    key = str(uuid4())
    response = client.post(
        "/api/v1/capture/file",
        headers=headers,
        files={"file": (filename, PDF, "text/html")},
        data={"metadata": json.dumps({"user_note": "Original", "client_capture_id": key})},
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["original_filename"] == filename
    assert item["mime_type"] == "application/pdf" and item["kind"] == "document"
    assert item["content"] is None and item["user_note"] == "Original"
    assert item["content_hash"] == hashlib.sha256(PDF).hexdigest()
    with TestClient(create_app(settings)) as restarted:
        original = restarted.get(f"/api/v1/items/{saved['id']}/content", headers=headers)
        assert original.content == PDF
        assert original.headers["content-type"] == "application/octet-stream"
        assert original.headers["content-disposition"].startswith("attachment;")
        assert "../" not in original.headers["content-disposition"]
        assert original.headers["x-content-type-options"] == "nosniff"
        replay = restarted.post(
            "/api/v1/capture/file",
            headers={**headers, "Idempotency-Key": key},
            files={"file": ("changed.txt", b"changed", "text/plain")},
        )
        assert replay.json() == saved
        assert restarted.get(f"/api/v1/items/{saved['id']}/content", headers=headers).content == PDF
    assert len([p for p in settings.storage_path.rglob("*") if p.is_file()]) == 1


def test_concurrent_file_replays_and_duplicate_storage(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    key = str(uuid4())

    def capture(index: int) -> str:
        response = client.post(
            "/api/v1/capture/file",
            headers={**headers, "Idempotency-Key": key},
            files={"file": ("same.bin", PDF)},
        )
        assert response.status_code == 201
        return str(response.json()["id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(capture, range(12)))
    assert len(set(ids)) == 1

    def duplicate(index: int) -> str:
        response = client.post(
            "/api/v1/capture/file",
            headers=headers,
            files={"file": ("same.bin", PDF)},
        )
        assert response.status_code == 201
        assert response.json()["duplicate_of"] == ids[0]
        return str(response.json()["id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        duplicates = list(pool.map(duplicate, range(6)))
    assert len(set(duplicates)) == 6
    assert len([p for p in settings.storage_path.rglob("*") if p.is_file()]) == 1


def test_files_owner_scoping_trash_and_malicious_claim(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    html = b"<script>alert('untrusted')</script>"
    saved = client.post(
        "/api/v1/capture/file",
        headers=headers,
        files={"file": ("x.png", html, "image/png")},
    ).json()
    item = client.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
    assert item["mime_type"] == "application/octet-stream"
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    path = f"/api/v1/items/{saved['id']}/content"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 404
    assert client.get(path, headers=headers).content == html
    with connection(settings) as conn:
        owner = conn.execute(
            "SELECT owner_id FROM api_token WHERE token_hash = %s",
            (token_hash(headers["Authorization"].split()[1]),),
        ).fetchone()
        assert owner is not None
        conn.execute(
            "UPDATE item SET deleted_at = now() WHERE owner_id = %s AND id = %s",
            (owner["owner_id"], saved["id"]),
        )
    assert client.get(path, headers=headers).status_code == 404


def test_file_limits_invalid_metadata_and_no_auth(
    settings: Settings,
    headers: dict[str, str],
) -> None:
    limited = settings.model_copy(update={"max_upload_bytes": 10, "max_request_bytes": 1024})
    with TestClient(create_app(limited)) as client:
        assert client.post("/api/v1/capture/file", files={"file": ("x", b"a")}).status_code == 401
        for metadata in ("bad JSON", '{"user_note":"\\u0000"}', '{"kind":"url"}'):
            result = client.post(
                "/api/v1/capture/file",
                headers=headers,
                files={"file": ("x", b"a")},
                data={"metadata": metadata},
            )
            assert result.status_code == 422
        result = client.post(
            "/api/v1/capture/file", headers=headers, files={"file": ("x", b"a" * 11)}
        )
        assert result.status_code == 413
        # Unknown-length request must hit the aggregate limit too.
        result = client.post(
            "/api/v1/capture/file", headers=headers, content=iter([b"x" * 600, b"x" * 600])
        )
        assert result.status_code == 413
        result = client.post(
            "/api/v1/capture/file", headers=headers, files={"file": ("empty", b"")}
        )
        assert result.status_code == 201


def test_file_commit_failure_orphan_retry_and_corruption(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    with connection(settings) as conn:
        conn.execute("""
            CREATE FUNCTION test_reject_file() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'failure'; END $$;
            CREATE CONSTRAINT TRIGGER test_reject_file AFTER INSERT ON item
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_reject_file();
        """)
    keyed = {**headers, "Idempotency-Key": str(uuid4())}
    try:
        response = client.post(
            "/api/v1/capture/file", headers=keyed, files={"file": ("x.pdf", PDF)}
        )
        assert response.status_code == 503
        with connection(settings) as conn:
            assert conn.execute("SELECT count(*) AS n FROM item").fetchone() == {"n": 0}
            assert conn.execute("SELECT count(*) AS n FROM blob").fetchone() == {"n": 0}
        files = [p for p in settings.storage_path.rglob("*") if p.is_file()]
        assert len(files) == 1 and files[0].read_bytes() == PDF
    finally:
        with connection(settings) as conn:
            conn.execute("DROP TRIGGER test_reject_file ON item; DROP FUNCTION test_reject_file()")
    saved = client.post(
        "/api/v1/capture/file", headers=keyed, files={"file": ("x.pdf", PDF)}
    ).json()
    path = f"/api/v1/items/{saved['id']}/content"
    assert client.get(path, headers=headers).content == PDF
    files[0].write_bytes(b"corrupted")
    assert client.get(path, headers=headers).status_code == 503
    assert (
        client.post(
            "/api/v1/capture/file", headers=headers, files={"file": ("x.pdf", PDF)}
        ).status_code
        == 503
    )
    assert files[0].read_bytes() == b"corrupted"
