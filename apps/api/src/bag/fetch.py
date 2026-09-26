"""SSRF-guarded URL fetching for the `url_fetch` processor.

Every destination, including each redirect target, is resolved first and every
resolved address must be globally routable. The connection is opened to that
exact address (no second lookup a rebinding DNS could redirect), TLS still
verifies the hostname, and size, time and redirect counts are bounded.
"""

import html
import http.client
import io
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from bag.config import Settings
from bag.processing import (
    MAX_EXTRACTED_BYTES,
    Outcome,
    ProcessingItem,
    ProcessorError,
    SnapshotBlob,
    is_plain_text,
)
from bag.storage import BlobStorage, StorageError, UploadTooLarge

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Policy = Callable[[Address], bool]
USER_AGENT = "BagOfHolding/0.1 (+https://github.com/almighty-atlas/bag)"
REDIRECTS = {301, 302, 303, 307, 308}
MIME_PATTERN = re.compile(r"^[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,64}$")
CHUNK = 64 * 1024


def is_public_address(address: Address) -> bool:
    """Only globally routable unicast addresses may be contacted."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


class Blocked(Exception):
    """The destination is not allowed by policy; a skip, not a failure."""


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    address: Address
    path: str


def resolve(url: str, policy: Policy) -> Destination:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or parts.hostname is None:
        raise Blocked("Only http and https destinations are fetched")
    if parts.username is not None or parts.password is not None:
        raise Blocked("Credentials in URLs are not fetched")
    host = parts.hostname
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise Blocked("Invalid port") from exc
    try:
        candidates: list[Address] = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ProcessorError("Destination could not be resolved") from exc
        candidates = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not candidates or not all(policy(address) for address in candidates):
        raise Blocked("Destination address is not allowed")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Destination(scheme, host, port, candidates[0], path)


SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg"}
BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "ul", "ol", "dl", "dt", "dd", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "nav",
    "aside", "main", "blockquote", "pre", "figure", "figcaption", "address", "form", "title",
}  # fmt: skip


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.text.append(data)


def clean_text(value: str, limit: int = MAX_EXTRACTED_BYTES) -> str | None:
    """Collapse whitespace and drop characters PostgreSQL text cannot hold."""
    value = "".join(c for c in value if c == "\n" or c == "\t" or is_plain_text(c))
    value = re.sub(r"[^\S\n]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value).strip()
    encoded = value.encode("utf-8")[:limit]
    value = encoded.decode("utf-8", errors="ignore").strip()
    return value or None


def parse_content_type(header: str | None) -> tuple[str, str | None]:
    if not header:
        return "application/octet-stream", None
    mime, _, rest = header.partition(";")
    mime = mime.strip().lower()
    if MIME_PATTERN.fullmatch(mime) is None:
        mime = "application/octet-stream"
    charset = None
    for param in rest.split(";"):
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()[:40]
    return mime, charset


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    mime_type: str
    charset: str | None
    body: bytes
    redirects: int


@dataclass(frozen=True)
class UrlFetcher:
    enabled: bool = True
    max_bytes: int = 5 * 1024 * 1024
    timeout: float = 10.0
    max_redirects: int = 5
    policy: Policy = is_public_address

    def with_settings(self, settings: Settings) -> "UrlFetcher":
        return replace(
            self,
            enabled=settings.fetch_urls,
            max_bytes=settings.fetch_max_bytes,
            timeout=settings.fetch_timeout_seconds,
            max_redirects=settings.fetch_max_redirects,
        )

    def _connect(self, destination: Destination) -> http.client.HTTPConnection:
        raw = socket.create_connection((str(destination.address), destination.port), self.timeout)
        try:
            if destination.scheme == "https":
                context = ssl.create_default_context()
                sock: socket.socket = context.wrap_socket(raw, server_hostname=destination.host)
                connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                    destination.host, destination.port, timeout=self.timeout, context=context
                )
            else:
                sock = raw
                connection = http.client.HTTPConnection(
                    destination.host, destination.port, timeout=self.timeout
                )
            # Pin the validated address: the client never resolves the host itself.
            connection.sock = sock
            return connection
        except BaseException:
            raw.close()
            raise

    def fetch(self, url: str) -> Fetched:
        deadline = time.monotonic() + self.timeout * 3
        redirects = 0
        while True:
            destination = resolve(url, self.policy)
            try:
                connection = self._connect(destination)
            except ssl.SSLError as exc:
                raise ProcessorError("TLS verification failed", retryable=False) from exc
            except OSError as exc:
                raise ProcessorError("Connection failed") from exc
            try:
                host = destination.host
                if destination.port not in (80, 443):
                    host = f"{host}:{destination.port}"
                if ":" in destination.host:
                    host = f"[{destination.host}]" + host[len(destination.host) :]
                connection.putrequest("GET", destination.path, skip_host=True)
                connection.putheader("Host", host)
                connection.putheader("User-Agent", USER_AGENT)
                connection.putheader("Accept", "text/html, application/xhtml+xml, */*;q=0.8")
                connection.putheader("Accept-Encoding", "identity")
                connection.endheaders()
                response = connection.getresponse()
                if response.status in REDIRECTS and response.getheader("Location"):
                    redirects += 1
                    if redirects > self.max_redirects:
                        raise ProcessorError("Too many redirects", retryable=False)
                    url = urljoin(url, response.getheader("Location", ""))
                    continue
                if response.status >= 400:
                    raise ProcessorError(
                        f"HTTP {response.status}", retryable=response.status >= 500
                    )
                buffer = io.BytesIO()
                while True:
                    if time.monotonic() > deadline:
                        raise ProcessorError("Fetch exceeded the time limit")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    if buffer.tell() + len(chunk) > self.max_bytes:
                        raise ProcessorError("Response exceeds the size limit", retryable=False)
                    buffer.write(chunk)
                mime, charset = parse_content_type(response.getheader("Content-Type"))
                return Fetched(url, response.status, mime, charset, buffer.getvalue(), redirects)
            except TimeoutError as exc:
                raise ProcessorError("Fetch timed out") from exc
            except (http.client.HTTPException, OSError) as exc:
                raise ProcessorError("Fetch failed") from exc
            finally:
                connection.close()

    def __call__(self, item: ProcessingItem, storage: BlobStorage) -> Outcome:
        if item.kind != "url" or item.content is None:
            return Outcome("skipped")
        if not self.enabled:
            return Outcome("skipped", metadata={"skipped": "fetching disabled"})
        try:
            fetched = self.fetch(item.content)
        except Blocked as exc:
            return Outcome("skipped", metadata={"skipped": str(exc)})
        try:
            snapshot = storage.put(io.BytesIO(fetched.body), self.max_bytes)
        except UploadTooLarge as exc:
            raise ProcessorError("Response exceeds the size limit", retryable=False) from exc
        except StorageError as exc:
            raise ProcessorError("Snapshot storage unavailable") from exc
        updates: dict[str, str | None] = {"mime_type": fetched.mime_type}
        defaults: dict[str, str] = {}
        if fetched.mime_type in {"text/html", "application/xhtml+xml", "text/plain"}:
            text = fetched.body.decode(fetched.charset or "utf-8", errors="replace")
            if fetched.mime_type == "text/plain":
                updates["extracted_text"] = clean_text(text)
            else:
                parser = TextExtractor()
                parser.feed(text)
                parser.close()
                updates["extracted_text"] = clean_text(html.unescape("".join(parser.text)))
                title = clean_text(" ".join(parser.title), 500)
                if title:
                    defaults["title"] = title
        return Outcome(
            "succeeded",
            updates,
            {
                "final_url": fetched.url[:2048],
                "status": fetched.status,
                "redirects": fetched.redirects,
                "size_bytes": len(fetched.body),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            defaults=defaults,
            blobs=[SnapshotBlob("snapshot", snapshot, fetched.mime_type)],
        )
