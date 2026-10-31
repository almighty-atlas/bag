import io
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from bag import imaging
from bag.imaging import (
    MAX_OUTPUT_BYTES,
    THUMBNAIL_EDGE,
    ImageRejected,
    is_clean_webp,
    render_isolated,
    render_thumbnail,
    webp_chunks,
)
from helpers import GIF, JPEG, PDF, PNG, WEBP, encode_image, photo
from PIL import Image
from PIL.PngImagePlugin import PngInfo


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def pixel(image: Image.Image, point: tuple[int, int]) -> tuple[int, ...]:
    value = image.getpixel(point)
    assert isinstance(value, tuple)
    return value


def reject(data: bytes) -> str:
    with pytest.raises(ImageRejected) as error:
        render_thumbnail(data)
    return error.value.reason


def patch_png_header(data: bytes, width: int, height: int) -> bytes:
    """Claim other dimensions in IHDR with a valid CRC, leaving the pixel data alone."""
    body = b"IHDR" + struct.pack(">II", width, height) + data[24:29]
    return data[:8] + struct.pack(">I", 13) + body + struct.pack(">I", zlib.crc32(body)) + data[33:]


@pytest.mark.parametrize(
    ("name", "data"), [("png", PNG), ("jpeg", JPEG), ("webp", WEBP), ("gif", GIF)]
)
def test_supported_formats_become_small_clean_webp(name: str, data: bytes) -> None:
    out = render_thumbnail(data)
    assert is_clean_webp(out) and len(out) < len(data) and len(out) <= MAX_OUTPUT_BYTES
    image = decode(out)
    assert image.format == "WEBP" and image.size == (THUMBNAIL_EDGE, 341), name
    left, right = pixel(image.convert("RGB"), (20, 170)), pixel(image.convert("RGB"), (490, 170))
    assert left[0] > 150 > left[2] and right[2] > 150 > right[0]


def test_small_images_are_not_enlarged_and_thin_ones_survive() -> None:
    assert decode(render_thumbnail(encode_image(photo((40, 30)), "PNG"))).size == (40, 30)
    assert decode(render_thumbnail(encode_image(Image.new("RGB", (16_384, 1)), "PNG"))).size == (
        THUMBNAIL_EDGE,
        1,
    )
    assert decode(render_thumbnail(encode_image(Image.new("RGB", (1, 1)), "PNG"))).size == (1, 1)


def test_transparency_is_kept_and_other_modes_are_normalized() -> None:
    clear = Image.new("RGBA", (300, 200), (255, 0, 0, 0))
    out = render_thumbnail(encode_image(clear, "PNG"))
    assert pixel(decode(out).convert("RGBA"), (10, 10))[3] == 0
    palette = Image.new("P", (64, 64))
    palette.info["transparency"] = 0
    palette_out = decode(render_thumbnail(encode_image(palette, "PNG")))
    assert pixel(palette_out.convert("RGBA"), (1, 1))[3] == 0
    # 16-bit grayscale is scaled, not clipped to white; CMYK JPEG becomes RGB.
    wide = decode(render_thumbnail(encode_image(Image.new("I;16", (64, 64), 16_384), "PNG")))
    assert 50 < pixel(wide.convert("RGB"), (5, 5))[0] < 80
    cmyk = Image.new("CMYK", (64, 64), (0, 255, 255, 0))
    assert (
        pixel(decode(render_thumbnail(encode_image(cmyk, "JPEG"))).convert("RGB"), (5, 5))[0] > 200
    )


def test_only_the_first_frame_of_animations_is_used() -> None:
    first = Image.new("RGB", (80, 40), (255, 0, 0))
    second = Image.new("RGB", (80, 40), (0, 0, 255))
    animated = encode_image(first, "GIF", save_all=True, append_images=[second], duration=50)
    out = render_thumbnail(animated)
    assert webp_chunks(out) == [b"VP8 "]
    assert pixel(decode(out).convert("RGB"), (5, 5))[0] > 200


@pytest.mark.parametrize(
    ("orientation", "size", "top_left", "bottom_right"),
    [
        (1, (400, 100), (255, 0, 0), (0, 0, 255)),
        (6, (100, 400), (255, 0, 0), (0, 0, 255)),  # 90 degrees clockwise: left edge ends on top
        (3, (400, 100), (0, 0, 255), (255, 0, 0)),
        (8, (100, 400), (0, 0, 255), (255, 0, 0)),
    ],
)
def test_exif_orientation_is_applied_to_the_pixels(
    orientation: int,
    size: tuple[int, int],
    top_left: tuple[int, int, int],
    bottom_right: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", (400, 100), (255, 0, 0))
    image.paste((0, 0, 255), (200, 0, 400, 100))
    exif = Image.Exif()
    exif[0x0112] = orientation
    out = render_thumbnail(encode_image(image, "PNG", exif=exif))
    result = decode(out).convert("RGB")
    assert result.size == size
    for expected, point in ((top_left, (2, 2)), (bottom_right, (size[0] - 3, size[1] - 3))):
        assert all(abs(a - b) < 40 for a, b in zip(pixel(result, point), expected, strict=True))
    assert b"EXIF" not in out


def test_metadata_never_reaches_the_output() -> None:
    exif = Image.Exif()
    exif[0x010F] = "SECRET-CAMERA-MAKE"
    exif[0x0112] = 1
    exif.get_ifd(0x8825)[0x0002] = (52.0, 31.0, 12.0)  # GPS latitude
    xmp = b"<x:xmpmeta>SECRET-XMP-AUTHOR</x:xmpmeta>"
    profile = b"SECRET-ICC-PROFILE" * 20
    jpeg = encode_image(
        photo((600, 400)), "JPEG", exif=exif, icc_profile=profile, comment=b"SECRET-COMMENT"
    )
    png = encode_image(photo((600, 400)), "PNG", exif=exif, icc_profile=profile)
    webp = encode_image(photo((600, 400)), "WEBP", exif=exif, icc_profile=profile, xmp=xmp)
    text = PngInfo()
    text.add_text("Author", "SECRET-TEXT")
    text_png = encode_image(photo((50, 50)), "PNG", pnginfo=text)
    for source in (jpeg, png, webp, text_png):
        assert b"SECRET" in source
        out = render_thumbnail(source)
        assert b"SECRET" not in out and b"GPS" not in out
        assert webp_chunks(out) == [b"VP8 "]
        assert not decode(out).info.get("exif") and not decode(out).info.get("icc_profile")


def test_malformed_exif_does_not_fail_the_thumbnail() -> None:
    broken = b"Exif\x00\x00MM\x00*\xff\xff\xff\xffgarbage"
    out = render_thumbnail(encode_image(photo((200, 100)), "JPEG", exif=broken))
    assert decode(out).size == (200, 100)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_truncated_and_flipped_files_are_corrupt(fmt: str) -> None:
    image = photo((300, 200)).convert("P") if fmt == "GIF" else photo((300, 200))
    data = encode_image(image, fmt)
    assert reject(data[: len(data) // 2]) == "corrupt"
    assert reject(data[:12]) == "corrupt"
    flipped = bytearray(data)
    for index in range(len(flipped) // 3, len(flipped) // 3 + 40):
        flipped[index] ^= 0xFF
    try:
        decode_ok = render_thumbnail(bytes(flipped))
    except ImageRejected as error:
        assert error.reason == "corrupt"
    else:
        # Some decoders conceal damage in entropy-coded data; the output must still be clean.
        assert is_clean_webp(decode_ok)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x89PNG\r\n\x1a\n" + b"junk" * 10,
        b"\xff\xd8\xff" + b"\x00" * 30,
        b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8,
        b"GIF89a" + b"\x00" * 20,
        PNG[:8] + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 12,
    ],
)
def test_garbage_with_image_signatures_is_corrupt(data: bytes) -> None:
    assert reject(data) == "corrupt"


@pytest.mark.parametrize(
    "data",
    [
        PDF,
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        encode_image(photo((20, 20)), "BMP"),
        encode_image(photo((20, 20)), "TIFF"),
        encode_image(photo((20, 20)), "ICO"),
        b"plain text",
    ],
)
def test_other_formats_are_never_decoded(data: bytes) -> None:
    assert reject(data) == "corrupt"


def test_polyglot_trailing_bytes_are_dropped() -> None:
    page = b"<html><script>alert(document.cookie)</script></html>"
    out = render_thumbnail(PNG + page)
    assert b"<script" not in out and is_clean_webp(out)


def test_pixel_and_dimension_limits_stop_bombs_before_decoding() -> None:
    bomb = encode_image(Image.new("1", (20_000, 20_000)), "PNG")
    assert len(bomb) < 100_000
    assert reject(bomb) == "too_large"
    assert reject(encode_image(Image.new("1", (20_000, 10)), "PNG")) == "too_large"
    assert reject(patch_png_header(PNG, 100_000, 100_000)) == "too_large"
    assert reject(patch_png_header(PNG, 2**31 - 1, 2**31 - 1)) == "too_large"
    assert reject(patch_png_header(PNG, 0, 5)) == "corrupt"
    # 6000 x 6667 is just over the pixel budget though every side is allowed.
    assert reject(encode_image(Image.new("1", (6_000, 6_700)), "PNG")) == "too_large"
    within = encode_image(Image.new("1", (6_000, 6_600)), "PNG")
    assert max(decode(render_thumbnail(within)).size) == THUMBNAIL_EDGE


def test_a_header_that_lies_small_cannot_expand_beyond_it() -> None:
    lying = patch_png_header(encode_image(Image.new("RGB", (800, 800)), "PNG"), 10, 10)
    try:
        out = render_thumbnail(lying)
    except ImageRejected as error:
        assert error.reason == "corrupt"
    else:
        assert decode(out).size == (10, 10)


def test_byte_and_output_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imaging, "MAX_INPUT_BYTES", len(PNG) - 1)
    assert reject(PNG) == "too_large"
    monkeypatch.undo()
    noise = Image.frombytes("RGB", (512, 512), bytes(range(256)) * 3072)
    monkeypatch.setattr(imaging, "MAX_OUTPUT_BYTES", 300)
    assert reject(encode_image(noise, "PNG")) == "output_too_large"


def test_clean_webp_check_rejects_metadata_and_damage() -> None:
    clean = render_thumbnail(PNG)
    assert is_clean_webp(clean) and not is_clean_webp(b"") and not is_clean_webp(PNG)
    assert not is_clean_webp(clean[:-1]) and not is_clean_webp(clean + b"\x00")
    assert not is_clean_webp(b"RIFF" + struct.pack("<I", 4) + b"WEBP")
    with_exif = encode_image(photo((64, 64)), "WEBP", exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08")
    assert b"EXIF" in (webp_chunks(with_exif) or [])
    assert not is_clean_webp(with_exif)
    assert not is_clean_webp(clean.ljust(MAX_OUTPUT_BYTES + 1, b"\x00"))


def test_isolated_rendering_matches_in_process_output() -> None:
    assert render_isolated(PNG) == render_thumbnail(PNG)
    assert render_isolated(JPEG) == render_thumbnail(JPEG)


def test_isolated_rendering_reports_rejections() -> None:
    assert _isolated_reason(b"\x89PNG\r\n\x1a\n" + b"x" * 40) == "corrupt"
    assert _isolated_reason(encode_image(Image.new("1", (20_000, 20_000)), "PNG")) == "too_large"
    assert _isolated_reason(PDF) == "corrupt"


def _isolated_reason(data: bytes) -> str:
    with pytest.raises(ImageRejected) as error:
        render_isolated(data)
    return error.value.reason


def test_child_runs_without_environment_secrets_and_with_fixed_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    real = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, **kwargs})
        return real(*args, **kwargs)

    monkeypatch.setenv("BAG_DATABASE_URL", "postgresql://secret")
    monkeypatch.setattr(subprocess, "run", spy)
    render_isolated(PNG)
    assert calls[0]["env"] == {} and calls[0]["args"][0][1:] == ["-I", "-m", "bag.imaging"]
    assert calls[0]["timeout"] == imaging.CHILD_WALL_SECONDS and calls[0]["check"] is False


def test_crash_hang_and_garbage_output_of_the_child_are_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def outcome(returncode: int, output: bytes = b"") -> Any:
        def run(*args: Any, **kwargs: Any) -> Any:
            kwargs["stdout"].write(output)
            return subprocess.CompletedProcess(args, returncode)

        return run

    for code in (-11, -9, -24, 1, 2, 137):  # signals (SEGV, KILL, CPU limit) and exceptions
        monkeypatch.setattr(subprocess, "run", outcome(code))
        assert _isolated_reason(PNG) == "corrupt"
    monkeypatch.setattr(subprocess, "run", outcome(12))
    assert _isolated_reason(PNG) == "too_large"
    monkeypatch.setattr(subprocess, "run", outcome(13))
    assert _isolated_reason(PNG) == "output_too_large"
    # A "successful" child must still deliver a clean WebP; anything else is distrusted.
    for payload in (b"", PNG, b"<html>", render_thumbnail(PNG) + b"x"):
        monkeypatch.setattr(subprocess, "run", outcome(0, payload))
        assert _isolated_reason(PNG) == "corrupt"

    def hang(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    assert _isolated_reason(PNG) == "corrupt"

    def cannot_start(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fork failed")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    with pytest.raises(OSError, match="fork failed"):
        render_isolated(PNG)


def test_a_real_hang_is_killed_at_the_wall_clock_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imaging, "CHILD_WALL_SECONDS", 0.01)
    assert _isolated_reason(PNG) == "corrupt"


def test_child_enforces_its_memory_limit() -> None:
    big = io.BytesIO()
    Image.linear_gradient("L").resize((6_000, 5_000)).convert("RGB").save(big, "WEBP")
    code = (
        "import sys; from bag import imaging; "
        "imaging.CHILD_MEMORY_BYTES = 250 * 1024 * 1024; sys.exit(imaging.main())"
    )

    def run(data: bytes) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-I", "-c", code],
            input=data,
            capture_output=True,
            timeout=60,
            check=False,
        )

    small = run(WEBP)
    assert small.returncode == 0 and is_clean_webp(small.stdout)
    exhausted = run(big.getvalue())
    # The decoder reports a failed allocation as an error or as MemoryError.
    assert exhausted.returncode in (imaging.EXIT_CORRUPT, imaging.EXIT_TOO_LARGE)
    assert exhausted.stdout == b""


def test_child_limits_are_set_before_reading_input(tmp_path: Path) -> None:
    code = (
        "import resource as r; from bag import imaging; imaging._limit_resources(); "
        "print(r.getrlimit(r.RLIMIT_AS)[0], r.getrlimit(r.RLIMIT_CPU)[0], "
        "r.getrlimit(r.RLIMIT_CORE)[0])"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.split() == [
        str(imaging.CHILD_MEMORY_BYTES),
        str(imaging.CHILD_CPU_SECONDS),
        "0",
    ]
