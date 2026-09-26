import hashlib
import ipaddress
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import pytest
from bag.config import Settings
from bag.db import connection
from bag.fetch import (
    Blocked,
    TextExtractor,
    UrlFetcher,
    clean_text,
    is_public_address,
    parse_content_type,
    resolve,
)
from bag.processors import PROCESSORS, ProcessorError
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain, runs

PAGE = (
    "<html><head><title> Bücher &amp; Kisten </title><script>alert('x')</script>"
    "<style>p{}</style></head><body><h1>Umzug</h1><p>Kisten\tim   Keller.</p>"
    "<noscript>hidden</noscript><p>Zweite&nbsp;Zeile</p></body></html>"
).encode()
PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"
LOOPBACK = ipaddress.ip_address("127.0.0.1")


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("2001:db8::1", False),
    ],
)
def test_public_address_policy(address: str, allowed: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is allowed


def test_resolve_validates_every_answer_and_pins_the_first() -> None:
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    mixed = public + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    with patch("socket.getaddrinfo", return_value=public):
        destination = resolve("https://Example.org:8443/a b?q=1#frag", is_public_address)
    assert destination.host == "example.org" and destination.port == 8443
    assert str(destination.address) == "93.184.216.34" and destination.path == "/a b?q=1"
    with patch("socket.getaddrinfo", return_value=mixed), pytest.raises(Blocked):
        resolve("https://example.org/", is_public_address)
    with patch("socket.getaddrinfo", side_effect=socket.gaierror), pytest.raises(ProcessorError):
        resolve("https://nowhere.invalid/", is_public_address)
    for url in (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/",
        "http://[::ffff:169.254.169.254]/",
        "ftp://example.org/",
        "http://user:pw@example.org/",
        "http://example.org:99999/",
    ):
        with pytest.raises(Blocked):
            resolve(url, is_public_address)
    assert resolve("http://8.8.8.8/", is_public_address).port == 80


def test_content_type_text_cleaning_and_html_extraction() -> None:
    assert parse_content_type(None) == ("application/octet-stream", None)
    assert parse_content_type('Text/HTML; Charset="UTF-8"') == ("text/html", "utf-8")
    assert parse_content_type("weird stuff") == ("application/octet-stream", None)
    assert clean_text("  a \x00b\r\n\n\t c  ") == "a b\nc"
    assert clean_text("\x00\x01") is None
    assert len(clean_text("ü" * 100, limit=11) or "") == 5
    parser = TextExtractor()
    parser.feed(PAGE.decode())
    assert "".join(parser.title).strip() == "Bücher & Kisten"
    text = "".join(parser.text)
    assert "alert" not in text and "hidden" not in text and "p{}" not in text
    assert "Umzug" in text and "Kisten" in text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes = b"", content_type: str | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        routes: dict[str, Any] = {
            "/page": lambda: self._send(200, PAGE, "text/html; charset=utf-8"),
            "/latin": lambda: self._send(
                200, "<title>Größe</title>Käse".encode("latin-1"), "text/html; charset=iso-8859-1"
            ),
            "/pdf": lambda: self._send(200, PDF, "application/pdf"),
            "/plain": lambda: self._send(200, b"nur Text\n\n\nZeile", "text/plain"),
            "/big": lambda: self._send(200, b"x" * 5000, "text/plain"),
            "/missing": lambda: self._send(404, b"gone"),
            "/down": lambda: self._send(503, b"later"),
        }
        if self.path in routes:
            routes[self.path]()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif self.path == "/private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/secret")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(2)
            self._send(200, b"late", "text/plain")
        else:
            self._send(404)


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.integration
def test_url_fetch_stores_snapshots_and_respects_policy(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    server: str,
) -> None:
    fetching = settings.model_copy(
        update={"fetch_urls": True, "fetch_max_bytes": 4096, "fetch_timeout_seconds": 0.5}
    )
    fetcher = UrlFetcher(policy=lambda address: address == LOOPBACK)
    worker = Worker(
        fetching, FileSystemStorage(settings.storage_path), {**PROCESSORS, "url_fetch": fetcher}
    )
    paths = [
        "page",
        "latin",
        "pdf",
        "plain",
        "big",
        "missing",
        "down",
        "redirect",
        "loop",
        "private",
        "slow",
    ]
    ids: dict[str, str] = {}
    for path in paths:
        response = client.post(
            "/api/v1/capture/url", json={"url": f"{server}/{path}"}, headers=headers
        )
        assert response.status_code == 201, response.text
        ids[path] = response.json()["id"]
    titled = client.post(
        "/api/v1/capture/url", json={"url": f"{server}/page", "user_note": "mine"}, headers=headers
    ).json()["id"]
    client.patch(f"/api/v1/items/{titled}", json={"title": "Eigener Titel"}, headers=headers)
    creds = client.post(
        "/api/v1/capture/url", json={"url": "http://user:pw@127.0.0.1/page"}, headers=headers
    ).json()["id"]
    drain(worker, limit=400)

    def item(path: str) -> dict[str, Any]:
        result: dict[str, Any] = client.get(f"/api/v1/items/{ids[path]}", headers=headers).json()
        return result

    def run(path: str) -> dict[str, Any]:
        return runs(client, headers, ids[path])["url_fetch"]

    page = item("page")
    assert page["title"] == "Bücher & Kisten" and page["mime_type"] == "text/html"
    assert page["extracted_text"] == "Umzug\nKisten im Keller.\nZweite Zeile"
    assert page["processing_status"] == "ready" and page["kind"] == "url"
    assert page["content"] == f"{server}/page"
    with connection(settings) as conn:
        blob = conn.execute(
            "SELECT role, sha256, mime_type FROM blob WHERE item_id = %s", (ids["page"],)
        ).fetchall()
        assert blob == [
            {
                "role": "snapshot",
                "sha256": hashlib.sha256(PAGE).hexdigest(),
                "mime_type": "text/html",
            }
        ]
        meta = conn.execute("SELECT metadata FROM item WHERE id = %s", (ids["page"],)).fetchone()
        assert meta is not None and meta["metadata"]["url_fetch"]["status"] == 200
        assert meta["metadata"]["url_fetch"]["redirects"] == 0
    hits = client.get("/api/v1/search?q=Keller", headers=headers).json()["results"]
    assert {row["id"] for row in hits} == {ids["page"], ids["redirect"], titled}
    assert client.get(f"/api/v1/items/{titled}", headers=headers).json()["title"] == "Eigener Titel"
    assert client.get(f"/api/v1/items/{ids['page']}/content", headers=headers).status_code == 404

    assert item("latin")["title"] == "Größe" and item("latin")["extracted_text"] == "Käse"
    assert item("pdf")["mime_type"] == "application/pdf"
    assert item("pdf")["extracted_text"] is None and item("pdf")["processing_status"] == "ready"
    assert item("plain")["extracted_text"] == "nur Text\nZeile"
    assert item("redirect")["title"] == "Bücher & Kisten"
    with connection(settings) as conn:
        meta = conn.execute(
            "SELECT metadata FROM item WHERE id = %s", (ids["redirect"],)
        ).fetchone()
        assert meta is not None and meta["metadata"]["url_fetch"]["redirects"] == 1
        assert meta["metadata"]["url_fetch"]["final_url"] == f"{server}/page"

    assert run("big")["status"] == "failed" and run("big")["attempts"] == 1
    assert run("big")["last_error"] == "Response exceeds the size limit"
    assert run("missing") == {
        **run("missing"),
        "status": "failed",
        "attempts": 1,
        "last_error": "HTTP 404",
    }
    assert (
        run("down")["status"] == "failed" and run("down")["attempts"] == settings.job_max_attempts
    )
    assert run("loop")["last_error"] == "Too many redirects" and run("loop")["attempts"] == 1
    assert run("slow")["last_error"] == "Fetch timed out"
    assert run("slow")["attempts"] == settings.job_max_attempts
    # Every other processor skips URLs, so a failed fetch leaves nothing succeeded.
    assert item("big")["processing_status"] == "failed" and item("big")["content"].endswith("/big")
    for path in ("private",):
        assert run(path)["status"] == "skipped" and item(path)["processing_status"] == "ready"
    assert runs(client, headers, creds)["url_fetch"]["status"] == "skipped"
    with connection(settings) as conn:
        meta = conn.execute("SELECT metadata FROM item WHERE id = %s", (ids["private"],)).fetchone()
        assert meta is not None and "not allowed" in meta["metadata"]["url_fetch"]["skipped"]
        # page, latin, pdf, plain, redirect and the titled item each keep one snapshot.
        assert conn.execute("SELECT count(*) AS n FROM blob").fetchone() == {"n": 6}

    # Reprocessing replaces the snapshot row instead of adding one; disabled fetching skips.
    client.post(f"/api/v1/items/{ids['page']}/reprocess", headers=headers)
    drain(worker)
    with connection(settings) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM blob WHERE item_id = %s", (ids["page"],)
        ).fetchone() == {"n": 1}
    client.post(f"/api/v1/items/{ids['page']}/reprocess", headers=headers)
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    assert run("page")["status"] == "skipped"
    assert item("page")["title"] == "Bücher & Kisten"
