import pytest

from core.leonardo_generation import to_aspect, clamp_quantity


def test_to_aspect_prefers_explicit_aspect_ratio():
    assert to_aspect(size="1024x1024", aspect_ratio="16:9") == "16:9"


def test_to_aspect_from_size():
    assert to_aspect(size="1792x1024") == "16:9"
    assert to_aspect(size="1024x1792") == "9:16"
    assert to_aspect(size="1024x1024") == "1:1"


def test_to_aspect_passthrough_supported_ratio():
    assert to_aspect(aspect_ratio="4:3") == "4:3"


def test_to_aspect_defaults_to_square_on_unknown():
    assert to_aspect() == "1:1"
    assert to_aspect(size="weird") == "1:1"
    assert to_aspect(aspect_ratio="7:5") == "1:1"   # 不在支持集


def test_clamp_quantity():
    assert clamp_quantity(1) == 1
    assert clamp_quantity(9) == 4
    assert clamp_quantity(0) == 1
    assert clamp_quantity(None) == 1
    assert clamp_quantity("3") == 3
    assert clamp_quantity("x") == 1
