def test_leonardo_model_in_catalog():
    from core.models.catalog import MODEL_CATALOG
    assert "leonardo-nano-banana-2" in MODEL_CATALOG
    conf = MODEL_CATALOG["leonardo-nano-banana-2"]
    assert conf["upstream_model"] == "leonardo:nano-banana-2"
    assert conf["dynamic"] is True
    assert conf["supports_auto_aspect_ratio"] is True
    # nano-banana 系上游无 4:3（实测发 4:3 会被改写成方图）→ 不再宣称支持
    assert set(conf["supported_aspect_ratios"]) == {"1:1", "16:9", "9:16"}


def test_leonardo_gpt_model_keeps_4x3():
    from core.models.catalog import MODEL_CATALOG
    conf = MODEL_CATALOG["leonardo-gpt-image-2"]
    # gpt-image-2 实测只支持 1:1(1536²) 与 4:3(2048x1536)；16:9/9:16 被上游拒绝
    assert set(conf["supported_aspect_ratios"]) == {"1:1", "4:3"}


def test_resolve_leonardo_model():
    from core.models.resolver import resolve_model
    conf = resolve_model("leonardo-nano-banana-2")
    assert conf["upstream_model"] == "leonardo:nano-banana-2"


def test_resolve_leonardo_model_ratio():
    from core.models.resolver import resolve_image_geometry
    geometry = resolve_image_geometry({"aspect_ratio": "16:9"}, "leonardo-nano-banana-2")
    assert geometry.aspect_ratio == "16:9"


def test_resolve_leonardo_unsupported_ratio_falls_back():
    from core.models.resolver import resolve_image_geometry
    geometry = resolve_image_geometry({"aspect_ratio": "21:9"}, "leonardo-nano-banana-2")
    # supports_auto=True 时 unsupported 比例落到 1:1（resolver 现有语义）
    assert geometry.aspect_ratio == "1:1"
