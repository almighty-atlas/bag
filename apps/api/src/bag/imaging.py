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
from collections.abc import Sequence
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

# The only VP8X feature flag a thumbnail may set; EXIF, ICCP, XMP and animation never appear.
_VP8X_ALPHA: Final = 0x10
VERIFY_BATCH: Final = 64

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


def _webp_parts(data: bytes) -> list[tuple[bytes, bytes]] | None:
    """(type, payload) of every chunk of a well-formed RIFF/WebP container, else None."""
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        return None
    parts: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            return None
        size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        end = offset + 8 + size
        padded = end + (size & 1)
        if padded > len(data) or (size & 1 and data[end] != 0):
            return None
        parts.append((data[offset : offset + 4], data[offset + 8 : end]))
        offset = padded
    return parts


def webp_chunks(data: bytes) -> list[bytes] | None:
    """Chunk types of a well-formed RIFF/WebP container, else None."""
    parts = _webp_parts(data)
    return None if parts is None else [name for name, _ in parts]


def _vp8_size(payload: bytes) -> tuple[int, int] | None:
    """Dimensions from a lossy key-frame header; None if it is not one."""
    if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
        return None
    tag = int.from_bytes(payload[:3], "little")
    # Key frame, a known profile, shown, and a first partition that fits the chunk.
    if tag & 1 or (tag >> 1) & 7 > 3 or not (tag >> 4) & 1 or (tag >> 5) > len(payload) - 10:
        return None
    width, height = struct.unpack("<HH", payload[6:10])
    if width >> 14 or height >> 14:  # upscaling hints are not part of a thumbnail
        return None
    return width, height


def _vp8l_size(payload: bytes) -> tuple[int, int] | None:
    """Dimensions from a lossless header; None if it is not one."""
    if len(payload) < 5 or payload[0] != 0x2F:
        return None
    bits = int.from_bytes(payload[1:5], "little")
    if bits >> 29:  # version
        return None
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def _alph_ok(payload: bytes) -> bool:
    """Alpha header: reserved bits clear, known pre-processing and compression."""
    return (
        bool(payload)
        and payload[0] >> 6 == 0
        and (payload[0] >> 4) & 3 <= 1
        and payload[0] & 3 <= 1
    )


def webp_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a thumbnail-shaped WebP, or None when anything is off.

    Accepts only a single still image, as `VP8 `, `VP8L`, `VP8X`+`VP8 ` or
    `VP8X`+`ALPH`+`VP8 `/`VP8L`; every chunk header must be consistent, the canvas
    must equal the frame, the size must be 1 to 512 px per side, no metadata or
    animation flag may be set and the size must fit the output limit. This reads
    headers only; whether the pixel data decodes is established by the renderer and
    the import check in a limited child process, never in the API process.
    """
    if not 0 < len(data) <= MAX_OUTPUT_BYTES:
        return None
    parts = _webp_parts(data)
    if not parts:
        return None
    canvas: tuple[int, int] | None = None
    flags = 0
    if parts[0][0] == b"VP8X":
        header = parts[0][1]
        if len(header) != 10 or header[1:4] != b"\0\0\0" or header[0] & ~_VP8X_ALPHA & 0xFF:
            return None
        flags = header[0]
        canvas = (
            int.from_bytes(header[4:7], "little") + 1,
            int.from_bytes(header[7:10], "little") + 1,
        )
        parts = parts[1:]
    names = [name for name, _ in parts]
    alpha_chunk = names == [b"ALPH", b"VP8 "]
    if names == [b"VP8 "] or alpha_chunk:
        if alpha_chunk and (canvas is None or not _alph_ok(parts[0][1])):
            return None
        if canvas is not None and bool(flags & _VP8X_ALPHA) != alpha_chunk:
            return None
        size = _vp8_size(parts[-1][1])
    elif names == [b"VP8L"]:
        size = _vp8l_size(parts[0][1])
    else:
        return None
    if size is None or (canvas is not None and size != canvas):
        return None
    if not all(0 < edge <= THUMBNAIL_EDGE for edge in size):
        return None
    return size


def is_clean_webp(data: bytes) -> bool:
    """A bounded, well-formed, metadata-free still WebP header structure (see `webp_size`)."""
    return webp_size(data) is not None


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
            # The writer's own output must pass the same checks as anything served later.
            if not decodes(encoded):
                raise ImageRejected("corrupt")
            return encoded
    raise ImageRejected("output_too_large")


def decodes(data: bytes) -> bool:
    """Whether `data` is a thumbnail-shaped WebP whose pixels actually decode.

    Decoding is bounded (at most 512 x 512 px and 256 KiB once the headers pass) but
    still runs a native decoder on untrusted bytes: call it only in the limited
    child process, never in the API or worker process.
    """
    size = webp_size(data)
    if size is None:
        return False
    try:
        with Image.open(io.BytesIO(data), formats=("WEBP",)) as image:
            if image.size != size or getattr(image, "n_frames", 1) != 1:
                return False
            image.load()
            return not {"exif", "icc_profile", "xmp"} & set(image.info)
    except (Image.DecompressionBombError, MemoryError, UnidentifiedImageError):
        return False
    except (OSError, SyntaxError, ValueError, EOFError, struct.error):
        return False


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


def _render_main() -> int:
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


def _verify_main() -> int:
    """stdin: (4-byte big-endian length, bytes)*; stdout: one `1` or `0` per entry."""
    data = sys.stdin.buffer.read(VERIFY_BATCH * (MAX_OUTPUT_BYTES + 4) + 1)
    verdicts = bytearray()
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            return EXIT_CORRUPT
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        if size > MAX_OUTPUT_BYTES or offset + size > len(data):
            return EXIT_CORRUPT
        verdicts.append(ord("1") if decodes(data[offset : offset + size]) else ord("0"))
        offset += size
    sys.stdout.buffer.write(bytes(verdicts))
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Child entry point; no argument renders (original in, WebP out), `verify` checks."""
    args = sys.argv[1:] if argv is None else argv
    _limit_resources()
    if not args:
        return _render_main()
    return _verify_main() if args == ["verify"] else 2


def _spawn(args: list[str], data: bytes) -> tuple[int, bytes]:
    """Run `python -m bag.imaging` with limits; (-1, b"") when it hangs.

    Raises `OSError` when the child cannot be started (a retryable condition).
    """
    with tempfile.TemporaryFile() as output:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user data in argv
                [sys.executable, "-I", "-m", "bag.imaging", *args],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                # The child never needs the database URL or any other secret.
                env={},
                timeout=CHILD_WALL_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, b""
        output.seek(0)
        return done.returncode, output.read(MAX_OUTPUT_BYTES + 1)


def render_isolated(data: bytes) -> bytes:
    """`render_thumbnail` in a resource-limited child process.

    Raises `ImageRejected` for rejected, crashing and hanging input, and `OSError`
    when the child cannot be started (a retryable condition).
    """
    if len(data) > MAX_INPUT_BYTES:
        raise ImageRejected("too_large")
    code, encoded = _spawn([], data)
    if code in _REASONS:
        raise ImageRejected(_REASONS[code])
    if code != 0:
        # A hang, a signal (decoder crash, CPU limit) or an unexpected exception.
        raise ImageRejected("corrupt")
    if not is_clean_webp(encoded):
        raise ImageRejected("corrupt")
    return encoded


def _verify_batch(batch: Sequence[bytes]) -> list[bool] | None:
    payload = b"".join(struct.pack(">I", len(item)) + item for item in batch)
    code, verdicts = _spawn(["verify"], payload)
    if code != 0 or len(verdicts) != len(batch):
        return None
    return [verdict == ord("1") for verdict in verdicts]


def verify_isolated(candidates: Sequence[bytes]) -> list[bool]:
    """Per candidate, whether it is a valid, decodable thumbnail (see `decodes`).

    Checked in limited child processes, 64 at a time. When a child fails as a whole
    its batch is retried one by one, so a decoder crash condemns only its own input.
    Raises `OSError` when no child can be started.
    """
    verdicts: list[bool] = []
    for start in range(0, len(candidates), VERIFY_BATCH):
        batch = candidates[start : start + VERIFY_BATCH]
        valid = [webp_size(item) is not None for item in batch]
        todo = [item for item, ok in zip(batch, valid, strict=True) if ok]
        checked = _verify_batch(todo) if todo else []
        if checked is None:
            # One entry cannot be blamed on a batch failure, and the rest deserve a verdict.
            checked = (
                [False] if len(todo) == 1 else [(_verify_batch([i]) or [False])[0] for i in todo]
            )
        results = iter(checked)
        verdicts.extend(next(results) if ok else False for ok in valid)
    return verdicts


if __name__ == "__main__":
    sys.exit(main())
