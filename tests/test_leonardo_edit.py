"""Leonardo 图生图（omni edit）。

参数结构取自 Leonardo 前端真实构造，并已线上验证：
  parameters = {omni_edit: true, prompt, prompt_enhance: "OFF", quantity,
                guidances: {image_reference: [{image: {id, type: "UPLOADED"}, strength: "MID"}]},
                width, height}
注意**不能**带 style_ids / modelId / guidance_scale / num_inference_steps 等文生图参数，
带上会被上游拒绝（实测 12+ 种变体全失败，加上 omni_edit 并精简后立即成功，
上游 generations.imageToImage 置为 true，输出明显受参考图影响）。
"""
import json

import pytest

from core.leonardo_client import (
    UPLOAD_INIT_IMAGE_QUERY,
    build_edit_payload,
    parse_init_image_upload,
)

IID = "eb659d8b-dfe8-4e50-bdf0-9c601fd7fff3"


def test_edit_payload_core_shape():
    p = build_edit_payload("make it watercolor", "nano-banana-2", 1024, 1024, [IID])
    assert p["operationName"] == "Generate"
    req = p["variables"]["request"]
    assert req["model"] == "nano-banana-2"
    params = req["parameters"]
    assert params["omni_edit"] is True
    assert params["prompt"] == "make it watercolor"
    assert params["prompt_enhance"] == "OFF"
    assert params["width"] == 1024 and params["height"] == 1024
    assert params["guidances"] == {
        "image_reference": [
            {"image": {"id": IID, "type": "UPLOADED"}, "strength": "MID"}
        ]
    }


def test_edit_payload_omits_text2image_only_params():
    # 这些参数会让 omni edit 被上游拒绝
    params = build_edit_payload("x", "nano-banana-2", 1024, 1024, [IID])["variables"]["request"]["parameters"]
    for forbidden in ("style_ids", "modelId", "guidance_scale",
                      "num_inference_steps", "negative_prompt", "dimensions"):
        assert forbidden not in params, forbidden


def test_edit_payload_multiple_reference_images():
    p = build_edit_payload("x", "gpt-image-2", 1536, 1536, [IID, "second-id"])
    refs = p["variables"]["request"]["parameters"]["guidances"]["image_reference"]
    assert [r["image"]["id"] for r in refs] == [IID, "second-id"]
    assert all(r["strength"] == "MID" for r in refs)


def test_edit_payload_requires_reference_image():
    with pytest.raises(ValueError):
        build_edit_payload("x", "nano-banana-2", 1024, 1024, [])


def test_upload_mutation_asks_for_needed_fields():
    q = UPLOAD_INIT_IMAGE_QUERY("png")["query"]
    assert "uploadInitImage" in q
    for field in ("id", "url", "fields", "key"):
        assert field in q


def test_parse_init_image_upload():
    resp = {"data": {"uploadInitImage": {
        "id": IID, "url": "https://s3.example/", "key": "users/u/initImages/x.png",
        "fields": json.dumps({"key": "users/u/initImages/x.png", "policy": "p"})}}}
    up = parse_init_image_upload(resp)
    assert up["id"] == IID
    assert up["url"] == "https://s3.example/"
    assert up["fields"]["policy"] == "p"


def test_parse_init_image_upload_rejects_bad_response():
    with pytest.raises(Exception):
        parse_init_image_upload({"data": {"uploadInitImage": None}})


def test_client_upload_init_image_posts_to_s3(monkeypatch):
    from core.leonardo_client import LeonardoClient
    import core.leonardo_client as lc

    posted = {}

    class _Resp:
        status_code = 204

    def fake_post(url, files=None, timeout=None, **kw):
        posted["url"] = url
        posted["fields"] = {k: v for k, v in files.items() if k != "file"}
        posted["file"] = files["file"]
        return _Resp()

    monkeypatch.setattr(lc.requests, "post", fake_post)
    client = LeonardoClient(gql=lambda t, p: {"data": {"uploadInitImage": {
        "id": IID, "url": "https://s3.example/", "key": "k.png",
        "fields": json.dumps({"key": "k.png", "policy": "p"})}}})

    got = client.upload_init_image("tok", b"\x89PNG-bytes", extension="png")
    assert got == IID
    assert posted["url"] == "https://s3.example/"
    assert posted["file"][1] == b"\x89PNG-bytes"
    assert posted["fields"]["policy"][1] == "p"  # (None, value) 形式的表单字段


def test_client_upload_raises_on_s3_failure(monkeypatch):
    from core.leonardo_client import LeonardoClient, LeonardoError
    import core.leonardo_client as lc

    class _Resp:
        status_code = 403
        text = "denied"

    monkeypatch.setattr(lc.requests, "post", lambda *a, **k: _Resp())
    client = LeonardoClient(gql=lambda t, p: {"data": {"uploadInitImage": {
        "id": IID, "url": "https://s3.example/", "key": "k.png",
        "fields": json.dumps({"key": "k.png"})}}})
    with pytest.raises(LeonardoError):
        client.upload_init_image("tok", b"x", extension="png")


def test_edit_images_uploads_then_generates():
    from core.leonardo_generation import edit_images

    calls = {}

    class _Client:
        def upload_init_image(self, token, image_bytes, extension="png", **kw):
            calls.setdefault("uploads", []).append((image_bytes, extension))
            return f"up-{len(calls['uploads'])}"

        def create_edit_generation(self, token, prompt, model_slug, width, height,
                                   init_image_ids, **kw):
            calls["gen"] = dict(prompt=prompt, model_slug=model_slug,
                                size=(width, height), ids=list(init_image_ids))
            return "gen-1"

        def wait_for_completion(self, token, gen_id, **kw):
            return {"success": True, "images": ["https://cdn/out.jpg"]}

    out = edit_images(_Client(), "tok", prompt="watercolor",
                      model_slug="nano-banana-2", model_id="uuid",
                      input_images=[(b"a", "image/png"), (b"b", "image/jpeg")],
                      aspect_ratio="1:1", output_resolution="2K")
    assert [e for _, e in calls["uploads"]] == ["png", "jpeg"]
    assert calls["gen"]["ids"] == ["up-1", "up-2"]
    assert calls["gen"]["model_slug"] == "nano-banana-2"
    assert out["data"] == [{"url": "https://cdn/out.jpg"}]
    assert out["provider"]["generation_id"] == "gen-1"


def test_edit_images_requires_input_image():
    from core.leonardo_generation import edit_images

    with pytest.raises(Exception):
        edit_images(object(), "tok", prompt="x", model_slug="nano-banana-2",
                    model_id="uuid", input_images=[], aspect_ratio="1:1")
