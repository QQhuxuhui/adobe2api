"""OpenAI 兼容端点：池里只有 Leonardo token 时 gpt-image-2 自动改由 Leonardo 出图。

有 Adobe token 时 gpt-image-2 维持 Adobe Firefly——由 test_images_edits/test_openai_responses 守护。
"""
import base64
import logging
import time
from pathlib import Path

import requests as req_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.generation import build_generation_router, _leonardo_public_backend
from core.models import (
    MODEL_CATALOG,
    SUPPORTED_RATIOS,
    VIDEO_MODEL_CATALOG,
    resolve_image_geometry,
    resolve_model,
)

GPT2_UUID = "135b2740-a20b-48c8-8f86-6f68199e06c5"


class DomainError(Exception):
    pass


# --- 别名决策(纯函数) ---

def test_public_backend_maps_gpt_image_2_when_enabled():
    assert _leonardo_public_backend("gpt-image-2", True) == "leonardo-gpt-image-2"


def test_public_backend_none_when_disabled():
    assert _leonardo_public_backend("gpt-image-2", False) is None


def test_public_backend_none_for_other_models():
    assert _leonardo_public_backend("gpt-image-1", True) is None
    assert _leonardo_public_backend("firefly-gpt-image", True) is None
    assert _leonardo_public_backend(None, True) is None


# --- 整路由:开关开时 gpt-image-2 → Leonardo ---

class FakeTokenManager:
    def __init__(self, *, leonardo: bool):
        self.selected = []
        self._leonardo = leonardo

    def get_available(self, token_type=None, strategy=None):
        self.selected.append(token_type)
        return "leo-token" if token_type == "leonardo" else "adobe-token"

    def has_active_token(self, token_type=None):
        if token_type == "leonardo":
            return self._leonardo
        if token_type == "adobe":
            return not self._leonardo
        return True


def _make_router(tmp_path: Path, *, leonardo: bool):
    credit_contexts: list[tuple] = []
    tokens = FakeTokenManager(leonardo=leonardo)

    def retry_runner(*, run_once, token_selector=None, **kwargs):
        token = token_selector() if token_selector is not None else "adobe-token"
        return run_once(token)

    api = FastAPI()
    api.include_router(
        build_generation_router(
            store=object(),
            token_manager=tokens,
            client=object(),
            credits_tracker=_Noop(),
            request_log_store=_Noop(),
            generated_dir=tmp_path,
            model_catalog=MODEL_CATALOG,
            video_model_catalog=VIDEO_MODEL_CATALOG,
            supported_ratios=SUPPORTED_RATIOS,
            resolve_model=resolve_model,
            resolve_image_geometry=resolve_image_geometry,
            require_service_api_key=lambda request: None,
            set_request_task_progress=lambda request, **k: None,
            set_request_credit_context=lambda request, model, res: credit_contexts.append(
                (model, res)
            ),
            run_with_token_retries=retry_runner,
            set_request_error_detail=lambda request, **k: "ERR",
            set_request_preview=lambda request, url, kind="image": None,
            public_image_url=lambda request, job_id: f"/generated/{job_id}.png",
            public_generated_url=lambda request, filename: f"/generated/{filename}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=lambda messages: [],
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, model, prompt: None,
            prepare_video_source_image=lambda image, ratio, res: (image, "image/png"),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "draw this",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda path, old, new: None,
            quota_error_cls=DomainError,
            auth_error_cls=DomainError,
            upstream_temp_error_cls=DomainError,
            logger=logging.getLogger("test-public-alias"),
        )
    )
    return TestClient(api), credit_contexts, tokens


class _Noop:
    def begin(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass

    def complete(self, **k):
        pass

    def upsert(self, *a, **k):
        pass


def test_gpt_image_2_routes_to_leonardo_when_enabled(tmp_path, monkeypatch):
    import core.leonardo_generation as lg

    captured = {}

    def fake_generate_images(**kw):
        captured.update(kw)
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-x", "aspect_ratio": "1:1", "model_id": "x"},
        }

    class _Resp:
        content = b"leo-bytes"
        status_code = 200

        def raise_for_status(self):
            return None

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _Resp())

    client, credit_contexts, tokens = _make_router(tmp_path, leonardo=True)
    resp = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-2", "prompt": "a fox", "response_format": "b64_json"},
    )

    assert resp.status_code == 200, resp.text
    # 走 Leonardo：slug/uuid 正确，选 leonardo 类型 token
    assert captured["model_slug"] == "gpt-image-2"
    assert captured["model_id"] == GPT2_UUID
    assert tokens.selected == ["leonardo"]
    body = resp.json()
    # 对外仍显示公开名 gpt-image-2（计费/响应）
    assert body["model"] == "gpt-image-2"
    assert credit_contexts and credit_contexts[0][0] == "gpt-image-2"
    assert body["data"][0]["b64_json"] == base64.b64encode(b"leo-bytes").decode()


def test_gpt_image_2_stays_adobe_when_disabled(tmp_path, monkeypatch):
    import core.leonardo_generation as lg

    def _boom(**kw):
        raise AssertionError("Leonardo must not be called when flag is off")

    monkeypatch.setattr(lg, "generate_images", _boom)

    client, _, tokens = _make_router(tmp_path, leonardo=False)
    # 关：gpt-image-2 仍解析为 Adobe(upstream openai:firefly:gpt-image)
    assert not str(resolve_model("gpt-image-2")["upstream_model"]).startswith("leonardo:")
    # 未选 leonardo 类型 token（走默认 Adobe token 选择）
    assert "leonardo" not in tokens.selected
