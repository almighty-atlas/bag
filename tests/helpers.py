import io
import struct
from typing import Any

from bag.worker import Worker
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

PDF_TEXT = "Umzug der Buecher in den Keller"


def build_pdf(text: str = PDF_TEXT) -> bytes:
    """A real one-page PDF with a text layer, built without font files."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    content = StreamObject()
    content.set_data(f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode("latin-1"))
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


PDF = build_pdf()
BROKEN_PDF = b"%PDF-1.4\n%original\x00\xff\n%%EOF\n"


def encode_image(image: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def photo(size: tuple[int, int] = (1200, 800)) -> Image.Image:
    """A deterministic image whose left half is red and right half blue."""
    image = Image.new("RGB", size, (220, 20, 20))
    image.paste((20, 20, 220), (size[0] // 2, 0, size[0], size[1]))
    return image


PNG = encode_image(photo(), "PNG")
JPEG = encode_image(photo(), "JPEG", quality=90)
WEBP = encode_image(photo(), "WEBP")
GIF = encode_image(photo().convert("P"), "GIF")


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


def riff(*chunks: tuple[bytes, bytes], size_delta: int = 0) -> bytes:
    """A RIFF/WebP container around the given (type, payload) chunks, padded as required."""
    body = b"WEBP"
    for name, payload in chunks:
        body += name + struct.pack("<I", len(payload)) + payload + (b"\0" * (len(payload) & 1))
    return b"RIFF" + struct.pack("<I", len(body) + size_delta) + body


def vp8(
    width: int, height: int, filler: bytes = bytes(64), scale: int = 0, partition: int = 4
) -> bytes:
    """A lossy key-frame header claiming the size, followed by filler instead of pixel data."""
    tag = (1 << 4) | (partition << 5)  # key frame, profile 0, shown
    dims = struct.pack("<HH", width | scale << 14, height)
    return tag.to_bytes(3, "little") + b"\x9d\x01\x2a" + dims + filler
