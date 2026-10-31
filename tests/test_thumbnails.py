import hashlib
import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from bag import imaging
from bag.auth import token_hash
from bag.cli import reprocess_items
from bag.config import Settings
from bag.db import connection
from bag.export import export_bag
from bag.ids import uuid7
from bag.imaging import ImageRejected, is_clean_webp, render_thumbnail
from bag.importer import import_bag
from bag.maintenance import collect_garbage, purge_items
from bag.processors import PROCESSORS
from bag.sessions import set_password
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import GIF, JPEG, PDF, PNG, WEBP, drain, encode_image, photo, runs
from PIL import Image

pytestmark = pytest.mark.integration

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.cookie)</script></svg>'
CSRF = {"X-Bag-Csrf": "1"}


@pytest.fixture
def storage(settings: Settings) -> FileSystemStorage:
    return FileSystemStorage(settings.storage_path)


@pytest.fixture
def worker(settings: Settings, storage: FileSystemStorage) -> Worker:
    return Worker(settings, storage)


def upload(
    client: TestClient, headers: dict[str, str], name: str, data: bytes, claimed: str = "image/png"
) -> str:
    response = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": (name, data, claimed)}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def thumbnail_rows(settings: Settings, item_id: str) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        return list(
            conn.execute(
                "SELECT id, storage_key, sha256, size_bytes, mime_type FROM blob "
                "WHERE item_id = %s AND role = 'thumbnail' ORDER BY created_at, id",
                (item_id,),
            ).fetchall()
        )


def objects(settings: Settings) -> set[str]:
    return {p.name for p in settings.storage_path.rglob("*") if p.is_file()}


def object_path(settings: Settings, sha256: str) -> Path:
    return settings.storage_path / sha256[:2] / sha256[2:4] / sha256


def thumbnail_url(item_id: str) -> str:
    return f"/api/v1/items/{item_id}/thumbnail"


def assert_attachment_only(client: TestClient, headers: dict[str, str], item_id: str) -> None:
    response = client.get(f"/api/v1/items/{item_id}/content", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    ("name", "data", "claimed"),
    [
        ("a.png", PNG, "image/png"),
        ("b.jpg", JPEG, "image/jpeg"),
        ("c.webp", WEBP, "image/webp"),
        ("d.gif", GIF, "image/gif"),
        ("lying.pdf", PNG, "application/pdf"),
    ],
)
def test_supported_images_get_a_re_encoded_inline_thumbnail(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
    name: str,
    data: bytes,
    claimed: str,
) -> None:
    item_id = upload(client, headers, name, data, claimed)
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404  # not yet
    drain(worker)

    run = runs(client, headers, item_id)["thumbnail"]
    assert run["status"] == "succeeded" and run["last_error"] is None
    response = client.get(thumbnail_url(item_id), headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["content-disposition"] == 'inline; filename="thumbnail.webp"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    )
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Authorization, Cookie"
    assert int(response.headers["content-length"]) == len(response.content)
    assert is_clean_webp(response.content) and response.content != data
    assert response.content == render_thumbnail(data)

    (row,) = thumbnail_rows(settings, item_id)
    assert row["sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["etag"] == f'"{row["sha256"]}"' and row["mime_type"] == "image/webp"
    with connection(settings) as conn:
        item = conn.execute(
            "SELECT i.kind, i.metadata, count(b.id) FILTER (WHERE b.role = 'original') "
            "AS originals FROM item i JOIN blob b ON b.item_id = i.id "
            "WHERE i.id = %s GROUP BY i.id",
            (item_id,),
        ).fetchone()
    assert item is not None and item["originals"] == 1 and item["kind"] == "image"
    meta = item["metadata"]["thumbnail"]
    assert meta["size_bytes"] == len(response.content) and meta["width"] == 512
    # The original is untouched and still only ever an attachment.
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == data
    assert_attachment_only(client, headers, item_id)
    assert client.get(f"/api/v1/items/{item_id}", headers=headers).json()["processing_status"] == (
        "ready"
    )


def test_conditional_requests_use_the_content_hash(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    first = client.get(thumbnail_url(item_id), headers=headers)
    etag = first.headers["etag"]
    for value in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        again = client.get(thumbnail_url(item_id), headers={**headers, "If-None-Match": value})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == etag and again.headers["cache-control"] == (
            "private, no-cache"
        )
    stale = client.get(thumbnail_url(item_id), headers={**headers, "If-None-Match": '"stale"'})
    assert stale.status_code == 200 and stale.content == first.content
    # A matching validator does not bypass authentication or the trash.
    assert client.get(thumbnail_url(item_id), headers={"If-None-Match": etag}).status_code == 401
    client.delete(f"/api/v1/items/{item_id}", headers=headers)
    gone = client.get(thumbnail_url(item_id), headers={**headers, "If-None-Match": etag})
    assert gone.status_code == 404 and gone.headers["cache-control"] == "no-store"


def test_capture_survives_corrupt_images_and_the_run_reports_it(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    truncated = JPEG[: len(JPEG) // 2]
    broken = {
        "truncated.jpg": truncated,
        "header-only.png": PNG[:40],
        "junk.png": b"\x89PNG\r\n\x1a\n" + b"junk" * 50,
        "flipped.png": PNG[:200] + bytes(b ^ 0xFF for b in PNG[200:400]) + PNG[400:],
    }
    ids = {name: upload(client, headers, name, data) for name, data in broken.items()}
    good = upload(client, headers, "good.png", PNG)
    drain(worker)

    for name, item_id in ids.items():
        # The original was acknowledged and stays retrievable byte for byte.
        assert (
            client.get(f"/api/v1/items/{item_id}/content", headers=headers).content
            == (broken[name])
        )
        run = runs(client, headers, item_id)["thumbnail"]
        if run["status"] == "succeeded":  # a decoder may conceal damage in pixel data
            assert is_clean_webp(client.get(thumbnail_url(item_id), headers=headers).content)
            continue
        assert run["status"] == "failed" and run["attempts"] == 1
        assert run["last_error"] == "Image could not be decoded safely"
        assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
        item = client.get(f"/api/v1/items/{item_id}", headers=headers).json()
        assert item["processing_status"] == "partial"
        assert runs(client, headers, item_id)["mime_detect"]["status"] == "succeeded"
    assert client.get(thumbnail_url(good), headers=headers).status_code == 200


def test_oversized_and_unsupported_images_are_skipped_with_a_reason(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    worker: Worker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bomb = encode_image(Image.new("1", (30_000, 30_000)), "PNG")
    wide = encode_image(Image.new("1", (20_000, 8)), "PNG")
    cases = {
        "bomb.png": (bomb, "too_large"),
        "wide.png": (wide, "too_large"),
        "scan.bmp": (encode_image(photo((20, 20)), "BMP"), "unsupported_format"),
        "scan.tiff": (encode_image(photo((20, 20)), "TIFF"), "unsupported_format"),
    }
    ids = {name: upload(client, headers, name, data) for name, (data, _) in cases.items()}
    svg = upload(client, headers, "logo.svg", SVG, "image/svg+xml")
    text = upload(client, headers, "notes.txt", b"just words", "text/plain")
    pdf = upload(client, headers, "doc.pdf", PDF, "application/pdf")
    monkeypatch.setattr("bag.processors.MAX_INPUT_BYTES", len(PNG) - 1)
    big = upload(client, headers, "big.png", PNG)
    drain(worker)

    for name, item_id in ids.items():
        run = runs(client, headers, item_id)["thumbnail"]
        assert run["status"] == "skipped" and run["last_error"] is None
        with connection(settings) as conn:
            row = conn.execute("SELECT metadata FROM item WHERE id = %s", (item_id,)).fetchone()
        assert row is not None and row["metadata"]["thumbnail"] == {"reason": cases[name][1]}
        assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
        assert client.get(f"/api/v1/items/{item_id}", headers=headers).json()[
            "processing_status"
        ] == ("ready")
    with connection(settings) as conn:
        row = conn.execute("SELECT metadata FROM item WHERE id = %s", (big,)).fetchone()
    assert row is not None and row["metadata"]["thumbnail"] == {"reason": "too_large"}
    for item_id in (svg, text, pdf):
        assert runs(client, headers, item_id)["thumbnail"]["status"] == "skipped"
        assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    # An SVG with a script is stored as an opaque original and never rendered or inlined.
    assert_attachment_only(client, headers, svg)
    assert client.get(f"/api/v1/items/{svg}/content", headers=headers).content == SVG
    assert not any(thumbnail_rows(settings, item_id) for item_id in (*ids.values(), svg, text))


def test_text_urls_and_missing_originals_are_not_thumbnailed(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    text = client.post("/api/v1/capture/text", json={"content": "hi"}, headers=headers).json()
    url = client.post(
        "/api/v1/capture/url", json={"url": "http://127.0.0.1/x.png"}, headers=headers
    ).json()
    drain(worker)
    for item_id in (text["id"], url["id"]):
        assert runs(client, headers, item_id)["thumbnail"]["status"] == "skipped"
        assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404


def test_capture_is_acknowledged_before_any_thumbnail_work(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(data: bytes) -> bytes:
        raise AssertionError("capture must not render")

    monkeypatch.setattr("bag.processors.render_isolated", explode)
    item_id = upload(client, headers, "a.png", PNG)
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG
    assert runs(client, headers, item_id)["thumbnail"]["status"] == "pending"


def test_unavailable_renderer_is_retried_then_fails_without_touching_the_capture(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    storage: FileSystemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def cannot_start(data: bytes) -> bytes:
        attempts.append(1)
        raise OSError("fork failed")

    monkeypatch.setattr("bag.processors.render_isolated", cannot_start)
    item_id = upload(client, headers, "a.png", PNG)
    drain(Worker(settings, storage))
    run = runs(client, headers, item_id)["thumbnail"]
    assert len(attempts) == settings.job_max_attempts == run["attempts"]
    assert run["status"] == "failed" and run["last_error"] == "Thumbnail renderer could not start"
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    monkeypatch.undo()
    # The failed run is picked up again by the standard retry scope and then succeeds.
    assert reprocess_items(settings, "failed", None) == {"items": 1, "jobs": 1}
    drain(Worker(settings, storage))
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 200


def test_a_decoder_crash_or_hang_is_a_permanent_failure_not_a_retry_loop(
    client: TestClient, headers: dict[str, str], worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def crash(data: bytes) -> bytes:
        calls.append(1)
        raise ImageRejected("corrupt")

    monkeypatch.setattr("bag.processors.render_isolated", crash)
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    assert len(calls) == 1
    assert runs(client, headers, item_id)["thumbnail"]["last_error"] == (
        "Image could not be decoded safely"
    )


def test_authentication_and_owner_isolation(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), other, "test", token_hash("other")),
        )
    path = thumbnail_url(item_id)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 404
    assert client.get(f"{path}?token={headers['Authorization'][7:]}").status_code == 401
    assert client.get(thumbnail_url(str(uuid7())), headers=headers).status_code == 404
    assert client.get(thumbnail_url("not-a-uuid"), headers=headers).status_code == 422
    assert client.post(path, headers=headers).status_code == 405
    assert client.get(path, headers=headers).status_code == 200
    # The same owner can use a web session cookie for <img> requests; no CSRF header on GET.
    with connection(settings) as conn:
        owner = conn.execute(
            "SELECT owner_id FROM api_token WHERE token_hash = %s",
            (token_hash(headers["Authorization"].split()[1]),),
        ).fetchone()
    assert owner is not None
    set_password(settings, "atlas", "korrekt-pferd-batterie-heftklammer", owner["owner_id"])
    web = TestClient(client.app)
    login = web.post(
        "/api/v1/session",
        json={"username": "atlas", "password": "korrekt-pferd-batterie-heftklammer"},
        headers=CSRF,
    )
    assert login.status_code == 200
    assert web.get(path).status_code == 200
    web.delete("/api/v1/session", headers=CSRF)
    assert web.get(path).status_code == 401


def test_trashed_items_lose_their_thumbnail_until_restored(
    client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    before = client.get(thumbnail_url(item_id), headers=headers).content
    assert client.delete(f"/api/v1/items/{item_id}", headers=headers).status_code == 204
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).status_code == 404
    assert client.post(f"/api/v1/items/{item_id}/restore", headers=headers).status_code == 200
    assert client.get(thumbnail_url(item_id), headers=headers).content == before


def test_missing_or_damaged_derived_blobs_never_break_the_item(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    storage: FileSystemStorage,
    worker: Worker,
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    (row,) = thumbnail_rows(settings, item_id)
    path = object_path(settings, row["sha256"])
    good = path.read_bytes()

    path.unlink()  # missing object
    missing = client.get(thumbnail_url(item_id), headers=headers)
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert missing.json() == {"detail": "Thumbnail not available"}
    assert client.get(f"/api/v1/items/{item_id}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG

    # Reprocessing writes the same bytes to the same hash path again: no new row or object.
    assert client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers).status_code == 202
    drain(worker)
    assert path.read_bytes() == good
    assert [r["sha256"] for r in thumbnail_rows(settings, item_id)] == [row["sha256"]]
    assert client.get(thumbnail_url(item_id), headers=headers).content == good

    path.write_bytes(good[:-5] + b"XXXXX")  # same size, wrong content
    damaged = client.get(thumbnail_url(item_id), headers=headers)
    assert damaged.status_code == 404 and b"XXXXX" not in damaged.content
    path.write_bytes(b"short")
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG

    # A damaged derived object is not silently overwritten: the rerun reports it ...
    client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)
    drain(worker)
    run = runs(client, headers, item_id)["thumbnail"]
    assert run["status"] == "failed" and run["last_error"] == "Thumbnail storage unavailable"
    # ... and removing the (derived, regenerable) file lets the next rerun repair it.
    path.unlink()
    client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)
    drain(worker)
    assert runs(client, headers, item_id)["thumbnail"]["status"] == "succeeded"
    assert client.get(thumbnail_url(item_id), headers=headers).content == good
    assert len(thumbnail_rows(settings, item_id)) == 1


def test_tampered_thumbnail_rows_never_serve_foreign_bytes_inline(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    html = upload(client, headers, "page.html", b"<html><script>alert(1)</script>", "text/html")
    drain(worker)
    (row,) = thumbnail_rows(settings, item_id)
    original = hashlib.sha256(PNG).hexdigest()
    key = f"{original[:2]}/{original[2:4]}/{original}"
    foreign = hashlib.sha256(b"<html><script>alert(1)</script>").hexdigest()
    cases = [
        # Row re-pointed at the original image, still claiming WebP.
        ("sha256 = %s, storage_key = %s, size_bytes = %s", (original, key, len(PNG))),
        # A genuine thumbnail with another media type.
        ("mime_type = %s", ("text/html",)),
        ("mime_type = %s", ("image/png",)),
        # A size claim above the limit.
        ("size_bytes = %s", (imaging.MAX_OUTPUT_BYTES + 1,)),
        # Uploaded HTML registered as a "thumbnail".
        (
            "sha256 = %s, storage_key = %s, size_bytes = %s",
            (
                foreign,
                f"{foreign[:2]}/{foreign[2:4]}/{foreign}",
                len(b"<html><script>alert(1)</script>"),
            ),
        ),
    ]
    for assignment, values in cases:
        with connection(settings) as conn:
            conn.execute(
                f"UPDATE blob SET {assignment} WHERE id = %s",  # noqa: S608 - fixed test SQL
                (*values, row["id"]),
            )
        response = client.get(thumbnail_url(item_id), headers=headers)
        assert response.status_code == 404, assignment
        assert PNG not in response.content and b"<script" not in response.content
        with connection(settings) as conn:
            conn.execute(
                "UPDATE blob SET sha256 = %s, storage_key = %s, size_bytes = %s, mime_type = %s "
                "WHERE id = %s",
                (row["sha256"], row["storage_key"], row["size_bytes"], row["mime_type"], row["id"]),
            )
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 200
    # The snapshot endpoint only reads its own role and never returns thumbnails.
    assert client.get(f"/api/v1/items/{item_id}/snapshot", headers=headers).status_code == 404
    assert client.get(thumbnail_url(html), headers=headers).status_code == 404


def test_reprocessing_replaces_the_thumbnail_without_duplicates_or_orphans(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    storage: FileSystemStorage,
    worker: Worker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    (first,) = thumbnail_rows(settings, item_id)
    baseline = objects(settings)
    assert len(baseline) == 2  # original and thumbnail

    for _ in range(3):  # repeated rendering is deterministic: same hash, same object
        client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)
        client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)  # no second job
        with connection(settings) as conn:
            active = conn.execute(
                "SELECT count(*) AS n FROM job WHERE item_id = %s AND processor = 'thumbnail' "
                "AND status IN ('queued', 'running')",
                (item_id,),
            ).fetchone()
        assert active == {"n": 1}
        assert drain(worker) == len(PROCESSORS)
        (again,) = thumbnail_rows(settings, item_id)
        assert again["sha256"] == first["sha256"] and objects(settings) == baseline

    # If the renderer's output changes (e.g. a library upgrade), the row is replaced and the
    # superseded object is left for garbage collection, never served and never duplicated.
    other = encode_image(Image.new("RGB", (64, 64), (1, 2, 3)), "WEBP")
    monkeypatch.setattr("bag.processors.render_isolated", lambda data: other)
    client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)
    drain(worker)
    (replaced,) = thumbnail_rows(settings, item_id)
    assert replaced["sha256"] == hashlib.sha256(other).hexdigest() != first["sha256"]
    assert client.get(thumbnail_url(item_id), headers=headers).content == other
    assert objects(settings) == baseline | {replaced["sha256"]}
    assert collect_garbage(settings, storage, min_age=timedelta(0))["removed"] == 1
    assert objects(settings) == {hashlib.sha256(PNG).hexdigest(), replaced["sha256"]}
    assert client.get(thumbnail_url(item_id), headers=headers).content == other


def test_identical_images_share_one_thumbnail_object_until_the_last_is_purged(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    storage: FileSystemStorage,
    worker: Worker,
) -> None:
    first = upload(client, headers, "one.png", PNG)
    second = upload(client, headers, "two.png", PNG)
    drain(worker)
    (row_one,) = thumbnail_rows(settings, first)
    (row_two,) = thumbnail_rows(settings, second)
    assert row_one["id"] != row_two["id"] and row_one["sha256"] == row_two["sha256"]
    assert len(objects(settings)) == 2
    assert collect_garbage(settings, storage, min_age=timedelta(0))["removed"] == 0

    client.delete(f"/api/v1/items/{first}", headers=headers)
    assert purge_items(settings, retention=timedelta(0))["purged"] == 1
    assert thumbnail_rows(settings, first) == []
    assert collect_garbage(settings, storage, min_age=timedelta(0))["removed"] == 0
    assert client.get(thumbnail_url(second), headers=headers).status_code == 200

    client.delete(f"/api/v1/items/{second}", headers=headers)
    assert purge_items(settings, retention=timedelta(0))["purged"] == 1
    assert collect_garbage(settings, storage, min_age=timedelta(0))["removed"] == 2
    assert objects(settings) == set()
    with connection(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM blob").fetchone() == {"n": 0}


def test_existing_images_get_thumbnails_through_the_missing_scope(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    with connection(settings) as conn:  # an item from before the processor existed
        conn.execute("DELETE FROM blob WHERE role = 'thumbnail'")
        conn.execute("DELETE FROM job WHERE processor = 'thumbnail'")
        conn.execute("DELETE FROM processing_run WHERE processor = 'thumbnail'")
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 404
    assert reprocess_items(settings, "missing", None) == {"items": 1, "jobs": 1}
    drain(worker)
    assert client.get(thumbnail_url(item_id), headers=headers).status_code == 200


def test_export_and_import_carry_thumbnails_and_distrust_them_on_the_way_back(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    storage: FileSystemStorage,
    worker: Worker,
    tmp_path: Path,
) -> None:
    item_id = upload(client, headers, "a.png", PNG)
    drain(worker)
    served = client.get(thumbnail_url(item_id), headers=headers).content
    target = tmp_path / "export"
    counts = export_bag(settings, storage, target)
    assert counts["blobs"] == 2 and counts["objects"] == 2
    blobs = [json.loads(line) for line in (target / "blobs.jsonl").read_text().splitlines()]
    assert {row["role"] for row in blobs} == {"original", "thumbnail"}
    assert (target / "objects" / hashlib.sha256(served).hexdigest()).read_bytes() == served

    def wipe() -> None:
        with connection(settings) as conn:
            for table in ("job", "processing_run", "relation", "item_tag", "item_collection"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            conn.execute("DELETE FROM blob; DELETE FROM item")
        shutil.rmtree(settings.storage_path)

    wipe()
    import_bag(settings, storage, target)
    assert client.get(thumbnail_url(item_id), headers=headers).content == served
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG

    # An export edited to carry HTML as the "thumbnail" imports (objects are only hash-checked)
    # but the endpoint refuses to send it.
    page = b"<html><script>alert(1)</script></html>"
    digest = hashlib.sha256(page).hexdigest()
    thumb_digest = hashlib.sha256(served).hexdigest()
    (target / "objects" / thumb_digest).unlink()
    (target / "objects" / digest).write_bytes(page)
    edited = [
        {
            **row,
            "sha256": digest,
            "storage_key": f"{digest[:2]}/{digest[2:4]}/{digest}",
            "size_bytes": len(page),
        }
        if row["role"] == "thumbnail"
        else row
        for row in blobs
    ]
    (target / "blobs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in edited))
    wipe()
    import_bag(settings, storage, target)
    response = client.get(thumbnail_url(item_id), headers=headers)
    assert response.status_code == 404 and page not in response.content
    assert client.get(f"/api/v1/items/{item_id}/content", headers=headers).content == PNG
    # Reprocessing restores a genuine thumbnail.
    client.post(f"/api/v1/items/{item_id}/reprocess", headers=headers)
    drain(worker)
    assert client.get(thumbnail_url(item_id), headers=headers).content == served


def test_existing_features_keep_working_next_to_thumbnails(
    settings: Settings, client: TestClient, headers: dict[str, str], worker: Worker
) -> None:
    image = upload(client, headers, "Kassenbon-Oktober.png", PNG)
    pdf = upload(client, headers, "Bücher.pdf", PDF, "application/pdf")
    text = client.post("/api/v1/capture/text", json={"content": "Notiz"}, headers=headers).json()
    drain(worker)
    found = client.get("/api/v1/search?q=Kassenbon", headers=headers).json()["results"]
    assert [row["id"] for row in found] == [image] and found[0]["matched_fields"] == ["filename"]
    listing = client.get("/api/v1/items", headers=headers).json()["items"]
    assert {row["id"] for row in listing} == {image, pdf, text["id"]}
    assert client.get(f"/api/v1/items/{pdf}/content", headers=headers).content == PDF
    assert client.get(f"/api/v1/items/{image}/snapshot", headers=headers).status_code == 404
    assert_attachment_only(client, headers, pdf)
    assert_attachment_only(client, headers, image)
    item = client.get(f"/api/v1/items/{image}", headers=headers).json()
    assert "thumbnail" not in json.dumps(item).lower() and item["mime_type"] == "image/png"
