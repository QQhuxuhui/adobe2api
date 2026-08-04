"""Gemini 原生端点在池里只有 Leonardo token 时自动改由 Leonardo 出图。

有 Adobe token 时行为不变、走 Adobe——由 test_gemini_native.py 全量守护。
本文件只覆盖"池只有 Leonardo"时的 Leonardo 分支。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes.gemini_native import build_gemini_native_router  # noqa: E402
from core.adobe_client import (  # noqa: E402
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)

PRO_UUID = "7c02ef35-3a6b-4df6-b78d-873e5032c3b4"
FLASH_UUID = "7418e71f-4133-4e1b-9895-bee19f48f2ce"


class FakeConfig:
    def __init__(self, *, api_key: str = "test-key", deadline=500):
        self.values = {"api_key": api_key, "gemini_native_deadline_seconds": deadline}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeAdobeClient:
    generate_timeout = 300

    def __init__(self):
        self.generate_calls: list[dict] = []
        self.upload_calls: list[dict] = []

    def upload_image(self, token, image_bytes, mime_type, deadline=None):
        self.upload_calls.append({"token": token})
        return f"{token}-img"

    def generate(self, **kwargs):
        self.generate_calls.append(dict(kwargs))
        kwargs["out_path"].write_bytes(b"adobe-png")
        return None, {"status": "SUCCEEDED", "outputs": [{"image": {}}]}


class FakeLeonardoClient:
    def __init__(self, images: list[str] | None = None, raise_on_create=None,
                 credit_cost=250):
        self.credit_cost = credit_cost
        self.images = images if images is not None else ["https://cdn.leonardo.ai/out.png"]
        self.create_calls: list[dict] = []
        self.wait_timeouts: list[int] = []
        self.create_deadlines: list[float | None] = []
        self.wait_deadlines: list[float | None] = []
        self._raise_on_create = raise_on_create

    def create_generation(
        self,
        token,
        prompt,
        model_id,
        aspect,
        quantity=1,
        model_slug="nano-banana-2",
        deadline=None,
        output_resolution="2K",
        on_cost=None,
    ):
        if on_cost is not None and self.credit_cost is not None:
            on_cost(self.credit_cost)
        self.create_deadlines.append(deadline)
        self.create_calls.append(
            {
                "token": token,
                "prompt": prompt,
                "model_id": model_id,
                "aspect": aspect,
                "quantity": quantity,
                "model_slug": model_slug,
                "output_resolution": output_resolution,
            }
        )
        if self._raise_on_create is not None:
            raise self._raise_on_create
        return "gen-123"

    def wait_for_completion(
        self, token, gen_id, timeout=300, poll_interval=4, deadline=None
    ):
        self.wait_timeouts.append(timeout)
        self.wait_deadlines.append(deadline)
        return {"success": True, "images": self.images}


class FakeTokenManager:
    def __init__(self, *, leonardo: bool = True):
        self.selected: list[str | None] = []
        self._leonardo = leonardo

    def get_available(self, token_type=None, strategy=None):
        self.selected.append(token_type)
        return "leo-token-1" if token_type == "leonardo" else "adobe-token-1"

    def has_active_token(self, token_type=None):
        if token_type == "leonardo":
            return self._leonardo
        if token_type == "adobe":
            return not self._leonardo
        return True


class _Resp:
    def __init__(self, content: bytes):
        self.content = content


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        leonardo: bool = True,
        cdn_bytes: bytes = b"leo-image-bytes",
        leonardo_images: list[str] | None = None,
        raise_on_create=None,
        deadline=500,
        credit_cost=250,
    ):
        self.config = FakeConfig(deadline=deadline)
        self.adobe = FakeAdobeClient()
        self.leo = FakeLeonardoClient(
            images=leonardo_images, raise_on_create=raise_on_create,
            credit_cost=credit_cost,
        )
        self.tokens = FakeTokenManager(leonardo=leonardo)
        self.previews: list[tuple[str, str]] = []
        self.accounted: list[tuple[Path, int, int]] = []
        self.credit_contexts: list[tuple] = []
        self.credit_logs: list[tuple] = []
        self.fetched: list[str] = []

        def capture_preview(request, url, kind="image"):
            self.previews.append((url, kind))
            used = getattr(request.state, "log_credits_used", None)
            if used is not None:
                self.credit_logs.append(
                    (used, getattr(request.state, "log_credits_source", None))
                )
        self.cdn_budgets: list[float | None] = []

        def fake_fetch(url, headers=None, max_seconds=None):
            self.fetched.append(url)
            self.cdn_budgets.append(max_seconds)
            return _Resp(cdn_bytes)

        def retry_runner(*, run_once, token_type="adobe", account_id=None, **kwargs):
            del kwargs, account_id
            token = self.tokens.get_available(token_type=token_type)
            return run_once(token)

        api = FastAPI()
        api.include_router(
            build_gemini_native_router(
                config_manager=self.config,
                client=self.adobe,
                generated_dir=tmp_path,
                run_with_token_retries=retry_runner,
                set_request_error_detail=lambda request, **k: "ERR",
                set_request_task_progress=lambda request, **k: None,
                set_request_logging_fields=lambda request, model, prompt: None,
                set_request_credit_context=lambda request, model, res: self.credit_contexts.append(
                    (model, res)
                ),
                set_request_preview=capture_preview,
                public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
                on_generated_file_written=lambda p, old, new: self.accounted.append(
                    (p, old, new)
                ),
                quota_error_cls=QuotaExhaustedError,
                auth_error_cls=AuthError,
                upstream_temp_error_cls=UpstreamTemporaryError,
                adobe_error_cls=AdobeRequestError,
                logger=_FakeLogger(),
                token_manager=self.tokens,
                leonardo_client=self.leo,
                fetch_cdn_image=fake_fetch,
            )
        )
        self.http = TestClient(api)


class _FakeLogger:
    def exception(self, message: str):
        pass


def image_request(*, text="draw a cat", ratio="1:1", size="1K", inline_image=None):
    parts = [{"text": text}]
    if inline_image is not None:
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(inline_image).decode("ascii"),
                }
            }
        )
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "imageConfig": {"aspectRatio": ratio, "imageSize": size},
            "candidateCount": 1,
        },
    }


def post(h: Harness, model: str, action: str, body: dict):
    return h.http.post(
        f"/v1beta/models/{model}:{action}",
        json=body,
        headers={"x-goog-api-key": "test-key"},
    )


def test_leo_pool_pro_routes_to_leonardo(tmp_path):
    h = Harness(tmp_path)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())

    assert resp.status_code == 200, resp.text
    # 未碰 Adobe 客户端
    assert h.adobe.generate_calls == []
    # 走 Leonardo，slug/uuid 正确
    assert len(h.leo.create_calls) == 1
    call = h.leo.create_calls[0]
    assert call["model_slug"] == "gemini-image-2"
    assert call["model_id"] == PRO_UUID
    assert call["aspect"] == "1:1"
    assert call["token"] == "leo-token-1"
    # 选的是 leonardo 类型 token
    assert h.tokens.selected == ["leonardo"]
    # 抓了 CDN，响应内联 CDN 字节
    assert h.fetched == ["https://cdn.leonardo.ai/out.png"]
    payload = resp.json()
    part = payload["candidates"][0]["content"]["parts"][0]
    assert base64.b64decode(part["inlineData"]["data"]) == b"leo-image-bytes"
    assert payload["modelVersion"] == "gemini-3-pro-image"


def test_leo_pool_flash_routes_to_leonardo(tmp_path):
    h = Harness(tmp_path)
    resp = post(h, "gemini-3.1-flash-image", "generateContent", image_request())

    assert resp.status_code == 200, resp.text
    call = h.leo.create_calls[0]
    assert call["model_slug"] == "nano-banana-2"
    assert call["model_id"] == FLASH_UUID


def test_leo_pool_preview_variants_route_to_leonardo(tmp_path):
    for model, uuid_ in (
        ("gemini-3-pro-image-preview", PRO_UUID),
        ("gemini-3.1-flash-image-preview", FLASH_UUID),
    ):
        h = Harness(tmp_path)
        resp = post(h, model, "generateContent", image_request())
        assert resp.status_code == 200, resp.text
        assert h.leo.create_calls[0]["model_id"] == uuid_


def test_leo_pool_rejects_input_image(tmp_path):
    h = Harness(tmp_path)
    # 一张最小 PNG 头即可（内容会被拒，不进 Leonardo）
    resp = post(
        h,
        "gemini-3-pro-image",
        "generateContent",
        image_request(inline_image=b"\x89PNG\r\n\x1a\n" + b"0" * 32),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert h.leo.create_calls == []


def test_leo_pool_rejects_unsupported_ratio(tmp_path):
    h = Harness(tmp_path)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request(ratio="21:9"))
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert h.leo.create_calls == []


def test_leo_pool_auto_ratio_maps_to_1x1(tmp_path):
    h = Harness(tmp_path)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request(ratio="auto"))
    assert resp.status_code == 200, resp.text
    assert h.leo.create_calls[0]["aspect"] == "1:1"


def test_adobe_pool_pro_stays_on_adobe(tmp_path):
    h = Harness(tmp_path, leonardo=False)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 200, resp.text
    # 关：仍走 Adobe，不碰 Leonardo
    assert len(h.adobe.generate_calls) == 1
    assert h.leo.create_calls == []


# --- #1 错误分类映射：不再一刀切 500 ---

def _leo_err(msg):
    from core.leonardo_client import LeonardoError

    return LeonardoError(msg)


def test_leo_pool_invalid_token_maps_to_401(tmp_path):
    h = Harness(tmp_path, raise_on_create=_leo_err("Could not verify JWT: invalid"))
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["status"] == "UNAUTHENTICATED"


def test_leo_pool_quota_maps_to_429(tmp_path):
    h = Harness(tmp_path, raise_on_create=_leo_err("insufficient balance"))
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 429, resp.text


def test_leo_pool_transport_maps_to_503(tmp_path):
    h = Harness(tmp_path, raise_on_create=_leo_err("graphql HTTP 500"))
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 503, resp.text


# --- #5 mime 按实际字节声明 ---

def test_leo_pool_jpeg_declared_as_jpeg(tmp_path):
    h = Harness(tmp_path, cdn_bytes=b"\xff\xd8\xff\xe0JFIF-bytes")
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 200, resp.text
    part = resp.json()["candidates"][0]["content"]["parts"][0]
    assert part["inlineData"]["mimeType"] == "image/jpeg"


def test_leo_pool_png_declared_as_png(tmp_path):
    h = Harness(tmp_path, cdn_bytes=b"\x89PNG\r\n\x1a\nrest")
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    part = resp.json()["candidates"][0]["content"]["parts"][0]
    assert part["inlineData"]["mimeType"] == "image/png"


# --- #4 Leonardo 拿不到真 4K → 明确拒绝(400) ---

def test_leo_pool_4k_request_rejected(tmp_path):
    h = Harness(tmp_path)
    resp = post(
        h, "gemini-3-pro-image", "generateContent", image_request(size="4K")
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert h.leo.create_calls == []


def test_leo_pool_4x3_rejected_not_silently_squared(tmp_path):
    # 实测：nano-banana 系上游没有 4:3，硬发 2048x1536 会回 2048x2048 方图。
    # 必须 400 拦下，绝不能生成一张与请求比例不符的图。
    h = Harness(tmp_path)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request(ratio="4:3"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert h.leo.create_calls == []


def test_leo_pool_lists_only_achievable_ratios(tmp_path):
    h = Harness(tmp_path)
    r = h.http.get("/v1beta/models", headers={"x-goog-api-key": "test-key"})
    assert r.status_code == 200
    by_id = {m["name"].split("/")[-1]: m for m in r.json()["models"]}
    for name in ("gemini-3-pro-image", "gemini-3.1-flash-image"):
        ratios = set(by_id[name].get("supportedAspectRatios") or [])
        if ratios:  # 该字段存在时不得声明 4:3
            assert "4:3" not in ratios, (name, ratios)


def test_leo_pool_image_size_threaded_to_upstream(tmp_path):
    # imageSize 之前对 Leonardo 完全无效(恒 1536² 被降到 1024²)；现在必须透传，
    # 由尺寸表决定 1K→1024²、2K→2048²。
    for size in ("1K", "2K"):
        h = Harness(tmp_path)
        resp = post(
            h, "gemini-3-pro-image", "generateContent", image_request(size=size)
        )
        assert resp.status_code == 200, resp.text
        assert h.leo.create_calls[0]["output_resolution"] == size


def test_leo_pool_records_exact_credit_cost(tmp_path):
    # 上游 Generate 回报的 apiCreditCost 必须落到请求日志（精确值，来源 upstream）
    h = Harness(tmp_path)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 200, resp.text
    assert h.credit_logs and h.credit_logs[-1] == (250.0, "upstream")


def test_leo_pool_no_credit_cost_when_upstream_silent(tmp_path):
    h = Harness(tmp_path, credit_cost=None)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 200, resp.text
    assert h.credit_logs == []


def test_leo_pool_2k_request_ok(tmp_path):
    # 2K 仍受理（Leonardo 实际 ~1536–2752）
    h = Harness(tmp_path)
    resp = post(
        h, "gemini-3-pro-image", "generateContent", image_request(size="2K")
    )
    assert resp.status_code == 200, resp.text
    assert h.credit_contexts == [("gemini-3-pro-image", "2K")]


# --- #6 生成超时受 deadline 约束 ---

def test_leo_pool_generate_timeout_bounded_by_deadline(tmp_path):
    h = Harness(tmp_path, deadline=30)
    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())
    assert resp.status_code == 200, resp.text
    # deadline 30s < 固定 300s → 传给 Leonardo 的超时被钳到 ~30s
    assert h.leo.wait_timeouts and h.leo.wait_timeouts[0] <= 30


def test_leo_pool_threads_absolute_deadline_to_client(tmp_path):
    h = Harness(tmp_path, deadline=30)

    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())

    assert resp.status_code == 200, resp.text
    assert h.leo.create_deadlines[0] is not None
    assert h.leo.create_deadlines[0] == h.leo.wait_deadlines[0]
    assert h.cdn_budgets[0] is not None
    assert 0 < h.cdn_budgets[0] <= 30


def test_leo_pool_does_not_fetch_cdn_after_deadline(tmp_path, monkeypatch):
    import api.routes.gemini_native as native_mod

    clock = {"t": 100.0}
    monkeypatch.setattr(native_mod.time, "monotonic", lambda: clock["t"])
    h = Harness(tmp_path, deadline=1)

    def finish_after_deadline(
        token, gen_id, timeout=300, poll_interval=4, deadline=None
    ):
        clock["t"] = 102.0
        return {"success": True, "images": h.leo.images}

    h.leo.wait_for_completion = finish_after_deadline

    resp = post(h, "gemini-3-pro-image", "generateContent", image_request())

    assert resp.status_code == 500, resp.text
    assert h.fetched == []
