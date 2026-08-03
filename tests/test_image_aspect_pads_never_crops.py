"""A requested aspect ratio is delivered by growing the frame, never by
cutting the picture.

Three positions have been held here. First, center-crop to force the ratio:
a 1024x1024 poster asked for as 9:16 came back 576x1024, 43% of the width
gone and the headline sliced off both edges. That was removed. Then,
deliver whatever the model produced, on the reasoning that bars are also a
silent edit.

Delivering off-ratio has a cost that only shows up downstream. The video
pipeline animates each still with generate_video, and a still whose ratio
differs from the clip's is cropped by the *provider* — outside our ffmpeg,
where nothing can pad it back. The composition is still whole here; it is
not whole after that. So the ratio is settled here, by padding.

The rule: every pixel the model produced survives. Only the canvas grows.
"""
from __future__ import annotations

import io

import pytest

from packages.core.ai.tools.extended_tools import (
    _normalize_image_bytes_for_aspect_ratio as normalize,
)

PIL = pytest.importorskip("PIL.Image")


def _encode(width, height, fmt="PNG", color=(255, 255, 255), mode="RGB"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format=fmt)
    return buffer.getvalue()


def _open(raw):
    from PIL import Image

    return Image.open(io.BytesIO(raw))


# ── Never smaller ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "width,height,ratio",
    [
        (1024, 1024, "16:9"),   # square asked for wide
        (1024, 1024, "9:16"),   # square asked for tall
        (1536, 1024, "16:9"),   # 3:2 asked for 16:9 — the stickman case
        (1080, 1920, "1:1"),    # tall asked for square
    ],
)
def test_neither_dimension_ever_shrinks(width, height, ratio):
    """Shrinking either side is cropping under another name."""
    out, _, size = normalize(_encode(width, height), "image/png", ratio)
    result = _open(out)
    assert result.width >= width and result.height >= height, (
        f"{width}x{height} -> {size} lost picture"
    )


@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1"])
def test_the_requested_ratio_is_actually_delivered(ratio):
    want_w, want_h = (int(part) for part in ratio.split(":"))
    out, _, _ = normalize(_encode(1024, 1024), "image/png", ratio)
    result = _open(out)
    assert abs(result.width / result.height - want_w / want_h) < 0.01


def test_the_original_pixels_are_centred_and_intact():
    from PIL import Image

    original = Image.new("RGB", (400, 400), (255, 255, 255))
    original.putpixel((0, 0), (255, 0, 0))       # a corner we can find again
    original.putpixel((399, 399), (0, 0, 255))
    buffer = io.BytesIO()
    original.save(buffer, format="PNG")

    out, _, _ = normalize(buffer.getvalue(), "image/png", "16:9")
    result = _open(out).convert("RGB")
    left = (result.width - 400) // 2
    top = (result.height - 400) // 2
    assert result.getpixel((left, top)) == (255, 0, 0)
    assert result.getpixel((left + 399, top + 399)) == (0, 0, 255)


# ── The padding is not damage ─────────────────────────────────────────


def test_padding_continues_the_colour_the_image_ends_in():
    """Black bars on white line art read as a broken render."""
    out, _, _ = normalize(_encode(1024, 1024, color=(255, 255, 255)), "image/png", "16:9")
    assert _open(out).convert("RGB").getpixel((0, 0)) == (255, 255, 255)

    out, _, _ = normalize(_encode(1024, 1024, color=(20, 20, 30)), "image/png", "16:9")
    assert _open(out).convert("RGB").getpixel((0, 0)) == (20, 20, 30)


def test_a_transparent_image_is_padded_transparently():
    raw = _encode(1024, 1024, color=(0, 0, 0, 0), mode="RGBA")
    out, mime, _ = normalize(raw, "image/png", "16:9")
    assert mime == "image/png"
    assert _open(out).convert("RGBA").getpixel((0, 0))[3] == 0


def test_a_png_does_not_come_back_as_a_jpeg():
    """convert() drops .format; re-encoding line art as JPEG is its own
    quiet damage."""
    out, mime, _ = normalize(_encode(1024, 1024, "PNG"), "image/png", "16:9")
    assert mime == "image/png"
    assert (_open(out).format or "").upper() == "PNG"


def test_a_jpeg_stays_a_jpeg():
    out, mime, _ = normalize(_encode(1024, 1024, "JPEG"), "image/jpeg", "16:9")
    assert mime == "image/jpeg"


# ── Leave alone what is already right ─────────────────────────────────


@pytest.mark.parametrize("ratio", ["", "   ", "4:3", "banana"])
def test_no_recognised_request_means_no_reshaping(ratio):
    raw = _encode(1000, 700)
    out, mime, size = normalize(raw, "image/png", ratio)
    assert out == raw and mime == "image/png" and size == "1000x700"


def test_an_image_already_at_the_ratio_is_untouched():
    raw = _encode(1920, 1080)
    out, mime, size = normalize(raw, "image/png", "16:9")
    assert out == raw, "a matching image must not be re-encoded"
    assert size == "1920x1080"


def test_unreadable_bytes_are_passed_through():
    out, mime, size = normalize(b"not an image", "image/png", "16:9")
    assert out == b"not an image" and size == ""


# ── The rule, pinned ──────────────────────────────────────────────────


def test_no_cropping_survives_in_the_source():
    import inspect

    from packages.core.ai.tools import extended_tools

    body = inspect.getsource(extended_tools._normalize_image_bytes_for_aspect_ratio)
    assert ".crop(" not in body, "the requested ratio is never reached by cutting"
