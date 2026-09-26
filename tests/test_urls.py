import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest
from bag.api import create_app
from bag.config import Settings
from bag.db import connection
from bag.schemas import UrlCapture
from fastapi.testclient import TestClient
from pydantic import ValidationError


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "//example.com",
        "https:example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com",
        "https://",
        "https://[broken",
        "https://example.com:99999",
        " https://example.com",
        "https://example.com/with space",
        "https://example.com/\n",
        "https://example.com/\x00",
        "https://example.com/\\path",
        "https://example.com/\ud800",
        "https://example.com/" + "a" * 8192,
    ],
)
def test_invalid_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        UrlCapture(url=url)


@pytest.mark.integration
def test_url_preserved_without_network_and_cross_route_replay(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    url = "HTTPS://Bücher.example:443/Über/%2f?b=2&a=1&a=3#Abschnitt"
    key = str(uuid4())
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("No DNS during capture")),
        patch("socket.create_connection", side_effect=AssertionError("No URL fetching")),
    ):
        response = client.post(
            "/api/v1/capture/url",
            headers=headers,
            json={
                "url": url,
                "user_note": "Unverändert",
                "client_capture_id": key,
                "captured_at": "2026-09-26T10:00:00+02:00",
            },
        )
    assert response.status_code == 201, response.text
    saved = response.json()
    with TestClient(create_app(settings)) as restarted:
        item = restarted.get(f"/api/v1/items/{saved['id']}", headers=headers).json()
        assert item["source_url"] == item["content"] == url
        assert item["kind"] == "url" and item["mime_type"] is None
        assert item["content_hash"] == hashlib.sha256(url.encode()).hexdigest()
        assert item["captured_at"] == "2026-09-26T08:00:00Z"
        assert item["user_note"] == "Unverändert"
        assert (
            restarted.post(
                "/api/v1/capture/text",
                headers=headers,
                json={"content": "changed", "client_capture_id": key},
            ).json()
            == saved
        )
    duplicate = client.post("/api/v1/capture/url", headers=headers, json={"url": url})
    assert duplicate.json()["duplicate_of"] == saved["id"]
    assert duplicate.json()["id"] != saved["id"]
    conflict = client.post(
        "/api/v1/capture/url",
        headers={**headers, "Idempotency-Key": key},
        json={"url": url, "client_capture_id": str(uuid4())},
    )
    assert conflict.status_code == 422
    assert client.post("/api/v1/capture/url", json={"url": url}).status_code == 401
    invalid = client.post("/api/v1/capture/url", headers=headers, json={"url": "javascript:secret"})
    assert invalid.status_code == 422 and "secret" not in invalid.text


@pytest.mark.integration
def test_parallel_url_replays_and_private_address_preservation(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    key = str(uuid4())

    def capture(index: int) -> str:
        response = client.post(
            "/api/v1/capture/url",
            headers={**headers, "Idempotency-Key": key},
            json={"url": "http://127.0.0.1:8080/private"},
        )
        assert response.status_code == 201
        return str(response.json()["id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(capture, range(12)))
    assert len(set(ids)) == 1


@pytest.mark.integration
def test_url_commit_failure_and_retry(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    with connection(settings) as conn:
        conn.execute("""
            CREATE FUNCTION test_reject_url() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'failure'; END $$;
            CREATE CONSTRAINT TRIGGER test_reject_url AFTER INSERT ON item
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_reject_url();
        """)
    payload = {"url": "https://example.invalid/original", "client_capture_id": str(uuid4())}
    try:
        assert client.post("/api/v1/capture/url", headers=headers, json=payload).status_code == 503
        with connection(settings) as conn:
            assert conn.execute("SELECT count(*) AS n FROM item").fetchone() == {"n": 0}
    finally:
        with connection(settings) as conn:
            conn.execute("DROP TRIGGER test_reject_url ON item; DROP FUNCTION test_reject_url()")
    saved = client.post("/api/v1/capture/url", headers=headers, json=payload)
    assert saved.status_code == 201
    assert client.post("/api/v1/capture/url", headers=headers, json=payload).json() == saved.json()
