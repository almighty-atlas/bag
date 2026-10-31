import io
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
