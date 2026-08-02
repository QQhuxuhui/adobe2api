from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models.catalog import MODEL_CATALOG

# OpenAI 模型名 -> (slug=request.model, Leonardo custom_models modelId UUID)
EXPECTED = {
    "leonardo-nano-banana-2": ("nano-banana-2", "7418e71f-4133-4e1b-9895-bee19f48f2ce"),
    "leonardo-nano-banana-pro": ("gemini-image-2", "7c02ef35-3a6b-4df6-b78d-873e5032c3b4"),
    "leonardo-gpt-image-2": ("gpt-image-2", "135b2740-a20b-48c8-8f86-6f68199e06c5"),
    "leonardo-gpt-image-1": ("gpt-image-1", "f75b1998-e5cb-4fdf-9eef-98e8186c2c2f"),
}


@pytest.mark.parametrize("model_id,expected", EXPECTED.items())
def test_leonardo_models_registered(model_id, expected):
    slug, uuid = expected
    conf = MODEL_CATALOG.get(model_id)
    assert conf is not None, f"{model_id} 未注册"
    assert conf["upstream_model"] == f"leonardo:{slug}"
    assert conf["upstream_model_id"] == uuid


def test_leonardo_family_count():
    leo = [m for m in MODEL_CATALOG if m.startswith("leonardo-")]
    assert set(leo) >= set(EXPECTED)
