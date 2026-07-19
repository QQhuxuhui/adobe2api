import io
import struct
import zlib

import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from core.models import resolve_image_geometry
from core.models.resolver import nearest_supported_ratio


def png_bytes(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


def oriented_jpeg_bytes(width: int, height: int, orientation: int) -> bytes:
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("RGB", (width, height), (20, 40, 60)).save(
        output, format="JPEG", exif=exif
    )
    return output.getvalue()


def oversized_png_header(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def test_auto_capable_model_uses_primary_image_ratio_and_aligned_size():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free", "quality": "2k"},
        "firefly-nano-banana-pro",
        [(png_bytes(1000, 1379), "image/png")],
    )

    assert resolved.aspect_ratio == "auto"
    assert resolved.usage_ratio == "1000:1379"
    assert resolved.output_resolution == "2K"
    assert resolved.output_size is not None
    assert resolved.output_size["width"] % 16 == 0
    assert resolved.output_size["height"] % 16 == 0
    actual = resolved.output_size["width"] / resolved.output_size["height"]
    assert abs(actual - 1000 / 1379) < 0.01
    assert resolved.fallback_aspect_ratio == "3:4"


def test_gpt_image_maps_primary_image_to_its_nearest_supported_ratio():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "auto"},
        "gpt-image-2",
        [(png_bytes(1000, 1379), "image/png")],
    )

    assert resolved.aspect_ratio == "3:4"
    assert resolved.usage_ratio == "3:4"
    assert resolved.output_size is None


def test_multiple_images_always_use_first_image_dimensions():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free"},
        "firefly-nano-banana-pro",
        [
            (png_bytes(1600, 900), "image/png"),
            (png_bytes(900, 1600), "image/png"),
        ],
    )

    assert resolved.usage_ratio == "16:9"
    assert resolved.output_size is not None
    assert resolved.output_size["width"] > resolved.output_size["height"]


def test_omitted_model_does_not_apply_default_models_fixed_ratio_to_free():
    resolved = resolve_image_geometry({"aspect_ratio": "free"}, None, [])

    assert resolved.aspect_ratio == "auto"
    assert resolved.usage_ratio == "1:1"
    assert resolved.output_size is None
    assert resolved.model_id == "firefly-nano-banana-pro"


def test_explicit_fixed_ratio_model_wins_without_decoding_primary_image():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free"},
        "firefly-nano-banana-pro-2k-16x9",
        [(b"not-an-image", "image/png")],
    )

    assert resolved.aspect_ratio == "16:9"
    assert resolved.usage_ratio == "16:9"
    assert resolved.output_size is None


def test_free_uses_size_when_no_input_image_exists():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free", "size": "1000x1379"},
        "gpt-image-2",
        [],
    )

    assert resolved.aspect_ratio == "3:4"


def test_unreadable_first_image_returns_400_instead_of_using_second_image():
    with pytest.raises(HTTPException) as exc_info:
        resolve_image_geometry(
            {"aspect_ratio": "free"},
            "gpt-image-2",
            [
                (b"not-an-image", "image/png"),
                (png_bytes(1600, 900), "image/png"),
            ],
        )

    assert exc_info.value.status_code == 400
    assert "first input image" in str(exc_info.value.detail)


def test_primary_image_dimensions_respect_exif_orientation():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free"},
        "gpt-image-2",
        [(oriented_jpeg_bytes(1200, 800, 6), "image/jpeg")],
    )

    assert resolved.aspect_ratio == "2:3"


def test_primary_image_dimensions_do_not_decode_pixels(monkeypatch):
    image_bytes = png_bytes(1000, 1379)

    def fail_if_loaded(*_args, **_kwargs):
        raise AssertionError("pixel data should not be decoded to read dimensions")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", fail_if_loaded)

    resolved = resolve_image_geometry(
        {"aspect_ratio": "free"},
        "gpt-image-2",
        [(image_bytes, "image/png")],
    )

    assert resolved.aspect_ratio == "3:4"


def test_decompression_bomb_header_returns_400():
    with pytest.raises(HTTPException) as exc_info:
        resolve_image_geometry(
            {"aspect_ratio": "free"},
            "firefly-nano-banana-pro",
            [(oversized_png_header(20_000, 10_000), "image/png")],
        )

    assert exc_info.value.status_code == 400
    assert "first input image" in str(exc_info.value.detail)


def test_extreme_auto_ratio_keeps_size_within_resolution_bounds():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free", "quality": "2k"},
        "firefly-nano-banana-pro",
        [(png_bytes(1, 100), "image/png")],
    )

    assert resolved.aspect_ratio == "auto"
    assert resolved.output_size is not None
    assert max(resolved.output_size.values()) <= 6144
    assert resolved.fallback_aspect_ratio == "9:16"


def test_nearest_ratio_uses_ordered_candidates_for_an_exact_tie():
    assert nearest_supported_ratio(1, 1, ("2:1", "1:2")) == "2:1"
