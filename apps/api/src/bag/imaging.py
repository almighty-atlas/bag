"""Safe thumbnail rendering (ADR-0023).

`render_thumbnail` decodes an untrusted PNG, JPEG, WebP or GIF with Pillow and
re-encodes a small, metadata-free WebP. `render_isolated` runs it in a child
Python process with address-space, CPU and wall-clock limits, so a decoder
crash, a hang or a memory blow-up cannot take the worker down.
"""

import io
import struct
import subprocess
import sys
import tempfile
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

# Accepted input; everything else (SVG, HEIC, AVIF, TIFF, BMP, ...) is declined.
INPUT_MIME_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
INPUT_FORMATS: Final = ("PNG", "JPEG", "WEBP", "GIF")
OUTPUT_MIME_TYPE: Final = "image/webp"

MAX_INPUT_BYTES: Final = 32 * 1024 * 1024
MAX_PIXELS: Final = 40_000_000
MAX_DIMENSION: Final = 16_384
THUMBNAIL_EDGE: Final = 512
MAX_OUTPUT_BYTES: Final = 256 * 1024
QUALITIES: Final = (80, 60, 40)

CHILD_MEMORY_BYTES: Final = 1024 * 1024 * 1024
CHILD_CPU_SECONDS: Final = 20
CHILD_WALL_SECONDS: Final = 30

# Chunks a thumbnail may contain; EXIF, ICCP, XMP and ANIM/ANMF never appear.
WEBP_CHUNKS: Final = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH"})

EXIT_CORRUPT: Final = 11
EXIT_TOO_LARGE: Final = 12
EXIT_OUTPUT_TOO_LARGE: Final = 13
_EXIT_CODES: Final = {
    "corrupt": EXIT_CORRUPT,
    "too_large": EXIT_TOO_LARGE,
    "output_too_large": EXIT_OUTPUT_TOO_LARGE,
}
_REASONS: Final = {code: reason for reason, code in _EXIT_CODES.items()}

# 16-bit grayscale PNGs; converting them directly would clip instead of scale.
_WIDE_GRAY: Final = frozenset({"I;16", "I;16L", "I;16B", "I;16N", "I", "F"})
_ALPHA_MODES: Final = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
# Modes Pillow resamples with a real filter (palette and 1-bit images would be nearest-neighbor).
_RESAMPLED_MODES: Final = frozenset({"L", "LA", "RGB", "RGBA"})
_ORIENTATION: Final = 0x0112


class ImageRejected(Exception):
    """The image cannot be turned into a thumbnail; `reason` is a stable token.

    `corrupt`: not decodable (truncated, malformed, not the claimed format, or the
    decoder crashed or hung); `too_large`: over a pixel, dimension, byte or memory
    limit; `output_too_large`: no quality step fit the output limit.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def webp_chunks(data: bytes) -> list[bytes] | None:
    """Chunk types of a well-formed RIFF/WebP container, else None."""
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        return None
    chunks: list[bytes] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            return None
        chunks.append(data[offset : offset + 4])
        size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        offset += 8 + size + (size & 1)
    return chunks if offset == len(data) else None


def is_clean_webp(data: bytes) -> bool:
    """A bounded, well-formed WebP made only of image-data chunks, no metadata."""
    if not 0 < len(data) <= MAX_OUTPUT_BYTES:
        return False
    chunks = webp_chunks(data)
    return chunks is not None and bool(chunks) and set(chunks) <= WEBP_CHUNKS


def _oriented(image: Image.Image) -> Image.Image:
    """The pixels turned upright per EXIF; the full-size copy is made only when needed."""
    try:
        if image.getexif().get(_ORIENTATION, 1) in (0, 1):
            return image
        return ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001 - malformed EXIF is untrusted metadata, not fatal
        return image


def _flatten(image: Image.Image) -> Image.Image:
    """An 8-bit RGB or RGBA image without palette, 16-bit or CMYK modes."""
    if image.mode in _WIDE_GRAY:
        image = image.point(lambda value: value * (1 / 256)).convert("L")
    if image.mode in _ALPHA_MODES or "transparency" in image.info:
        return image if image.mode == "RGBA" else image.convert("RGBA")
    return image if image.mode == "RGB" else image.convert("RGB")


def render_thumbnail(data: bytes) -> bytes:
    """Decode `data` and return a re-encoded WebP of at most 512 px per side.

    Only the first frame of animated input is used. Orientation from EXIF is
    applied to the pixels; no metadata of the input reaches the output.
    """
    if len(data) > MAX_INPUT_BYTES:
        raise ImageRejected("too_large")
    try:
        with Image.open(io.BytesIO(data), formats=INPUT_FORMATS) as source:
            width, height = source.size
            if width <= 0 or height <= 0:
                raise ImageRejected("corrupt")
            if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
                raise ImageRejected("too_large")
            if source.format == "JPEG":
                # Let libjpeg decode at a reduced scale; no mode change (keeps CMYK intact).
                source.draft(None, (THUMBNAIL_EDGE * 2, THUMBNAIL_EDGE * 2))
            source.load()
            image = _oriented(source)
            if image.mode not in _RESAMPLED_MODES:
                image = _flatten(image)
            image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
            image = _flatten(image)
            image.info = {}
            return _encode(image)
    except ImageRejected:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageRejected("too_large") from exc
    except MemoryError as exc:
        raise ImageRejected("too_large") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ImageRejected("corrupt") from exc
    except struct.error as exc:
        raise ImageRejected("corrupt") from exc


def _encode(image: Image.Image) -> bytes:
    for quality in QUALITIES:
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=4, icc_profile=None, exif=b"", xmp=b"")
        encoded = buffer.getvalue()
        if len(encoded) <= MAX_OUTPUT_BYTES:
            if not is_clean_webp(encoded):
                raise ImageRejected("corrupt")
            return encoded
    raise ImageRejected("output_too_large")


def _limit_resources() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES * 2, MAX_OUTPUT_BYTES * 2))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        # Platforms without these limits (macOS development) keep the wall-clock bound.
        pass


def main() -> int:
    """Child entry point: original bytes on stdin, WebP on stdout, outcome in the exit code."""
    _limit_resources()
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        encoded = render_thumbnail(data)
    except ImageRejected as exc:
        return _EXIT_CODES[exc.reason]
    except MemoryError:
        return EXIT_TOO_LARGE
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def render_isolated(data: bytes) -> bytes:
    """`render_thumbnail` in a resource-limited child process.

    Raises `ImageRejected` for rejected, crashing and hanging input, and `OSError`
    when the child cannot be started (a retryable condition).
    """
    if len(data) > MAX_INPUT_BYTES:
        raise ImageRejected("too_large")
    with tempfile.TemporaryFile() as output:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user data in argv
                [sys.executable, "-I", "-m", "bag.imaging"],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                # The child never needs the database URL or any other secret.
                env={},
                timeout=CHILD_WALL_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ImageRejected("corrupt") from exc
        if done.returncode in _REASONS:
            raise ImageRejected(_REASONS[done.returncode])
        if done.returncode != 0:
            # A signal (decoder crash, CPU limit) or an unexpected exception.
            raise ImageRejected("corrupt")
        output.seek(0)
        encoded = output.read(MAX_OUTPUT_BYTES + 1)
    if not is_clean_webp(encoded):
        raise ImageRejected("corrupt")
    return encoded


if __name__ == "__main__":
    sys.exit(main())
