# Leonardo Route Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `core/leonardo_client.py` + `core/leonardo_generation.py` 接到账号池和 `/v1/images/generations` 路由，让 Leonardo 类型的 Bearer 通过标准 OpenAI 兼容端点出图。

**Architecture:** 四处注入式改动，不重构现有流程：(1) `token_mgr` 加 `type` 字段和按类型选号；(2) `MODEL_CATALOG` 注册 Leonardo 模型，`upstream_model` 前缀 `"leonardo:"` 区分类型；(3) `leonardo_generation.generate_images` 对已提交后的失败抛专用 `LeonardoGenerationError`（重试会重复扣费，必须与提交前失败区分）；(4) 路由 `openai_generate()` 检测 Leonardo 模型后分叉到专用 `run_once`（模块级工厂 `_build_leonardo_run_once`）+ 专用 `token_selector`，`LeonardoError`/`LeonardoGenerationError` 映射为现有 Adobe 异常体系走通已有重试/日志/错误上报链路。Leonardo token 无 refresh profile → `handle_auth_failure` 自然 `report_invalid`，不触发 Adobe cookie 刷新。

**Tech Stack:** Python 3.10、pytest、`requests`（下载 CDN 图片）。复用 `core/leonardo_client.py`（`is_likely_leonardo_token`, `LeonardoClient`, `LeonardoError`）、`core/leonardo_generation.py`（`generate_images`, `to_aspect`）。

## Global Constraints

- **Python 3.10**；无新依赖。
- **不重构 `_run_with_token_retries` 签名**：通过 `token_selector` 参数（已存在）和异常映射让现有重试/日志/错误上报逻辑零改动运行。
- **默认选号排除 Leonardo**：`get_available()` 默认 `token_type="adobe"`（即 `type != "leonardo"`）——现有所有 Adobe 路由（含直接调 `get_available` 的 `api/generate` runner、entity、video_tasks）行为不变，不会误选 Leonardo token 打 Adobe API。Leonardo 路径显式传 `token_type="leonardo"`。
- **重试安全（不重复扣费）**：`Generate` mutation 已提交后（`wait_for_completion` 轮询超时 / 状态 FAILED）抛 `LeonardoGenerationError` → 映射为非重试 `AdobeRequestError` → 500，绝不换号重发；提交前失败（invalid JWT / HTTP 错误 / 传输异常重试耗尽）才可换号重试。CDN 图片下载失败同样不可重试（生成已成功，重试 = 再次扣费）。
- **向后兼容**：`token_mgr` 现有 token 不标 type → 默认 `"adobe"`。`get_available` 现有调用零改动（默认值行为等价）。MODEL_CATALOG 现有条目不受影响。
- **b64_json 模式**：从 Leonardo CDN URL 下载图片 → base64 编码，与 Adobe 路径行为一致。
- **测试用 monkeypatch**：零真实网络。Leonardo 路径不测 `_run_with_token_retries` 循环（已由现有测试覆盖），只测路由分叉 + 异常映射 + `run_once` 正确性。

---

### Task 1: Token pool — Leonardo type tagging + filtered selection

**Files:**
- Modify: `core/token_mgr.py`
- Test: `tests/test_token_mgr.py`（新建）

**Interfaces:**
- `TokenManager.add(value, meta)`：调用时自动用 `is_likely_leonardo_token(value)` 检测，tag `type: "leonardo"` 存入 token dict（`meta` 显式含 `type` 时以 meta 为准）。
- `TokenManager.get_available(strategy="round_robin", token_type="adobe")`：默认 `"adobe"`（选 `type != "leonardo"`，含未标 type 的）；`"leonardo"` 只选 `type == "leonardo"`；`None` 不过滤。现有调用全部等价于 `"adobe"`，行为不变。
- `TokenManager.list_active_account_tokens()`：返回 dict 增加 `type` 字段。
- 已有方法不受影响：`upsert_auto_refresh_token` 不标记 type（auto_refresh 永远是 Adobe cookie→access_token 流程）；`account_id_from_token` 对 Leonardo Cognito JWT 返回 `sub`（Cognito user UUID，稳定可去重）。

**注意（先读）**：`core/token_mgr.py` 当前不 import `core.leonardo_client`。`core/leonardo_client.py` 只 import `requests`/stdlib，无循环 import，可以放模块顶层 import。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_token_mgr.py
import base64
import json
import threading
import time

import pytest

from core.token_mgr import TokenManager


def _jwt(payload: dict) -> str:
    """构造可被 base64 解码的 JWT（header.sig 用固定占位，payload 是真实 base64url）。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{body}.sig"


def _leonardo_cognito_jwt() -> str:
    return _jwt({
        "sub": "a6fbcd6a-039f-445c-83e6-6822b7e113d5",   # Cognito user UUID
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123",
        "cognito:username": "canva-user-123",
        "exp": int(time.time()) + 3600,
    })


def _adobe_jwt() -> str:
    return _jwt({
        "user_id": "ADOBE_USER_123",
        "exp": int(time.time()) + 3600,
    })


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    import core.token_mgr as tm_mod
    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "tokens_legacy.json")
    return TokenManager()


def test_add_auto_tags_leonardo_token(fresh_tm):
    token = _leonardo_cognito_jwt()
    t = fresh_tm.add(token)
    assert t.get("type") == "leonardo"


def test_add_does_not_tag_adobe_token(fresh_tm):
    t = fresh_tm.add(_adobe_jwt())
    assert t.get("type") != "leonardo"


def test_add_meta_type_overrides_auto_detect(fresh_tm):
    t = fresh_tm.add(_leonardo_cognito_jwt(), meta={"type": "custom"})
    assert t.get("type") == "custom"


def test_upsert_auto_refresh_does_not_tag_leonardo(fresh_tm):
    t = fresh_tm.upsert_auto_refresh_token(_leonardo_cognito_jwt(), profile_id="p1")
    assert t.get("type") != "leonardo"


def test_get_available_leonardo_type_filter(fresh_tm):
    leo = fresh_tm.add(_leonardo_cognito_jwt())
    fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available(token_type="leonardo")
    assert result == leo["value"]


def test_get_available_default_excludes_leonardo(fresh_tm):
    fresh_tm.add(_leonardo_cognito_jwt())
    adobe = fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available()  # 默认 adobe，排除 leonardo
    assert result == adobe["value"]


def test_get_available_none_filter_returns_any(fresh_tm):
    leo = fresh_tm.add(_leonardo_cognito_jwt())
    result = fresh_tm.get_available(token_type=None)  # 显式不过滤
    assert result == leo["value"]


def test_get_available_leonardo_filter_returns_none_when_no_match(fresh_tm):
    fresh_tm.add(_adobe_jwt())
    result = fresh_tm.get_available(token_type="leonardo")
    assert result is None


def test_account_id_from_leonardo_token(fresh_tm):
    token = _leonardo_cognito_jwt()
    # account_id_from_token 取 sub（Cognito user UUID），稳定可去重
    assert fresh_tm.account_id_from_token(token) == "a6fbcd6a-039f-445c-83e6-6822b7e113d5"


def test_list_active_account_tokens_includes_type(fresh_tm):
    fresh_tm.add(_leonardo_cognito_jwt())
    items = fresh_tm.list_active_account_tokens()
    leo_items = [i for i in items if i.get("token", "").startswith("x.")]
    assert len(leo_items) == 1
    assert leo_items[0].get("type") == "leonardo"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_token_mgr.py -v`
Expected: 5 FAIL —— `test_add_auto_tags_leonardo_token`（无 type）、`test_get_available_leonardo_type_filter`/`test_get_available_default_excludes_leonardo`/`test_get_available_leonardo_filter_returns_none_when_no_match`（`get_available` 无 `token_type` 参数 → TypeError）、`test_list_active_account_tokens_includes_type`（输出无 type）。其余 5 个是回归护栏，改动前已 PASS。

- [ ] **Step 3: Write minimal implementation**

(a) `core/token_mgr.py` 模块顶部（`from core.config_mgr import ...` 同区，`from pathlib import Path` 之后）加：

```python
from core.leonardo_client import is_likely_leonardo_token
```

（`core/leonardo_client.py` 只 import requests/stdlib，无循环 import。）

(b) `add()` 方法，在 `new_token = {...}` 与 `if meta:` 之间加：

```python
            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": time.time(),
                "error_until": 0,
            }
            # 未显式指定 type 时按 token 形态自动判定；meta 含 type 则以其为准
            if not meta or "type" not in meta:
                if is_likely_leonardo_token(value):
                    new_token["type"] = "leonardo"
            if meta:
                new_token.update(meta)
            self.tokens.append(new_token)
            self.save()
            return new_token
```

(c) `_pick_active_token_locked` 增加 `token_type` 过滤：

```python
    def _pick_active_token_locked(
        self, strategy: str = "round_robin", token_type: Optional[str] = None
    ) -> Optional[Dict]:
        active = [t for t in self.tokens if t.get("status") in {"active", "error"}]
        if token_type == "leonardo":
            active = [t for t in active if t.get("type") == "leonardo"]
        elif token_type == "adobe":
            active = [t for t in active if t.get("type") != "leonardo"]
        if not active:
            return None
        ...
```

注意：**不加 `error_until` 过滤**——现状就不过滤，超出本任务范围（现有测试已覆盖现状语义）。

(d) `get_available` 签名与传参：

```python
    def get_available(
        self,
        strategy: str = "round_robin",
        token_type: Optional[str] = "adobe",
    ) -> Optional[str]:
        with self._lock:
            chosen = self._pick_active_token_locked(strategy=strategy, token_type=token_type)
            return chosen["value"] if chosen is not None else None
```

默认 `"adobe"`：现有调用 `get_available(strategy=...)` 语义等价（排除 leonardo），零改动。

(e) `list_active_account_tokens()` 返回 dict 加：

```python
                    "account_id": aid,
                    "type": str(t.get("type") or ""),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_token_mgr.py -v`
Expected: 10 PASS。

- [ ] **Step 5: Commit**

```bash
git add core/token_mgr.py tests/test_token_mgr.py
git commit -m "feat(token-mgr): Leonardo type 标注与按类型选号(默认排除)" \
  -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Model catalog — Leonardo 模型注册

**Files:**
- Modify: `core/models/catalog.py`
- Test: `tests/test_models.py`（新建）

**Interfaces:**
- 新增模型 ID：`leonardo-nano-banana-2`（动态分辨率/比例，base model）。
- 模型 conf 关键字段：`upstream_model: "leonardo:nano-banana-2"`（路由用此前缀判断）、`dynamic: True`、`supports_auto_aspect_ratio: True`。
- 支持的比例：Leonardo 原生 4 种——`("1:1", "16:9", "9:16", "4:3")`。请求里其他比例（如 OpenAI 默认 21:9）由 resolver 落到最近的受支持比例（`supports_auto` 为 True 时 unsupported 比例落 `1:1`）。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
def test_leonardo_model_in_catalog():
    from core.models.catalog import MODEL_CATALOG
    assert "leonardo-nano-banana-2" in MODEL_CATALOG
    conf = MODEL_CATALOG["leonardo-nano-banana-2"]
    assert conf["upstream_model"] == "leonardo:nano-banana-2"
    assert conf["dynamic"] is True
    assert conf["supports_auto_aspect_ratio"] is True
    assert set(conf["supported_aspect_ratios"]) == {"1:1", "16:9", "9:16", "4:3"}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_models.py -v`
Expected: 3 FAIL（模型不在 catalog）——`test_resolve_leonardo_unsupported_ratio_falls_back` 因 resolver 现有语义可能 PASS。

- [ ] **Step 3: Write minimal implementation**

在 `core/models/catalog.py` 的 `_register_base_model` 调用区块末尾添加：

```python
LEONARDO_SUPPORTED_RATIOS = ("1:1", "16:9", "9:16", "4:3")

_register_base_model(
    "leonardo-nano-banana-2",
    upstream_model="leonardo:nano-banana-2",
    upstream_model_id="leonardo",
    upstream_model_version="nano-banana-2",
    label="Leonardo Nano Banana 2",
    supports_auto_aspect_ratio=True,
    supported_aspect_ratios=LEONARDO_SUPPORTED_RATIOS,
)
```

`_register_base_model` 的 `supported_aspect_ratios` 类型是 `tuple[str, ...]`，字面量符合。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -v`
Expected: 4 PASS。

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `python -m pytest -q`
Expected: 原有测试继续 PASS；新增 4 PASS。

- [ ] **Step 6: Commit**

```bash
git add core/models/catalog.py tests/test_models.py
git commit -m "feat(catalog): 注册 Leonardo Nano Banana 2 模型" \
  -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `leonardo_generation` — 已提交后失败抛专用异常

**Files:**
- Modify: `core/leonardo_generation.py`
- Test: `tests/test_leonardo_generation.py`

**Interfaces:**
- 新增异常类 `LeonardoGenerationError(LeonardoError)`：表示生成**已提交**后的失败（轮询超时 / 上游状态 FAILED）。换号重试会重复扣费，必须与提交前失败（`LeonardoError`）区分。
- `generate_images()` 中 `if not result.get("success")` 分支改抛 `LeonardoGenerationError`（原抛 `LeonardoError`）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_generation.py （追加到文件末尾）
def test_generate_images_raises_generation_error_on_timeout():
    import pytest
    from core.leonardo_client import LeonardoError
    from core.leonardo_generation import LeonardoGenerationError, generate_images

    class _FakeClient:
        def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
            return "gen-1"

        def wait_for_completion(self, token, gen_id, *, timeout, poll_interval, sleep, now):
            return {"success": False, "error": "generation timeout"}

    with pytest.raises(LeonardoGenerationError) as excinfo:
        generate_images(
            _FakeClient(), "tok", prompt="p", model_id="m", timeout=5
        )
    # 与提交前失败同族，但类型可区分
    assert isinstance(excinfo.value, LeonardoError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_generation.py -k generation_error -v`
Expected: FAIL —— 现有 `generate_images` 抛 `LeonardoError` 而非 `LeonardoGenerationError`。

- [ ] **Step 3: Write minimal implementation**

`core/leonardo_generation.py`，`LeonardoError` import 行后加：

```python
from core.leonardo_client import LeonardoError


class LeonardoGenerationError(LeonardoError):
    """生成已提交后失败(轮询超时/上游 FAILED)。重试会重复扣费，不得自动重发。"""
```

`generate_images()` 中：

```python
    if not result.get("success"):
        raise LeonardoError(str(result.get("error") or "generation failed"))
```

改为：

```python
    if not result.get("success"):
        raise LeonardoGenerationError(str(result.get("error") or "generation failed"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_generation.py -v`
Expected: 9 PASS（原 8 + 新增 1）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_generation.py tests/test_leonardo_generation.py
git commit -m "fix(leonardo-gen): 已提交后失败抛 LeonardoGenerationError(不可重试)" \
  -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Route — /v1/images/generations Leonardo 分支

**Files:**
- Modify: `api/routes/generation.py`
- Test: `tests/test_leonardo_route.py`（新建）

**Interfaces:**
- `build_generation_router` 新增可选参数 `leonardo_client=None`（`LeonardoClient` 实例）。不传时行为不变（路由内 `LeonardoClient()`）。
- `openai_generate()` 在模型解析后检测 `model_conf.get("upstream_model", "").startswith("leonardo:")`，是则走 Leonardo 分支：用模块级工厂 `_build_leonardo_run_once(...)` 造 `run_once` + `token_selector=lambda: token_manager.get_available(token_type="leonardo")`，调 `run_with_token_retries`。
- Adobe 分支 `token_selector=None`（默认），`_run_with_token_retries` 内部 `get_available()` 默认已排除 leonardo（Task 1），Adobe 请求不会误选 Leonardo token。
- 异常映射：模块级 `_map_leonardo_error(exc)`——`LeonardoGenerationError` → `AdobeRequestError`（非重试，500）；`LeonardoError` 按关键词 → `AuthError` / `QuotaExhaustedError` / `UpstreamTemporaryError`（可换号重试）。CDN 下载失败直接抛 `AdobeRequestError`。
- 响应格式：`b64_json` 下载 CDN URL 后 base64 编码；`url` 存到 generated_dir 并返回 public URL。两种格式都带 `revised_prompt`（与 Adobe 路径一致）。
- `usage` 字段：`build_image_usage(prompt, output_resolution, final_aspect, ())`。
- 模块级常量 `DEFAULT_LEONARDO_MODEL_ID = "7418e71f-4133-4e1b-9895-bee19f48f2ce"`（Nano Banana 2）。

**注意（先读）**：
- `_map_leonardo_error` / `_build_leonardo_run_once` 必须是**模块级**函数（测试直接 import），异常类从 `core.adobe_client` 直接 import（app.py 布线给 router 的正是这几个类）。
- `public_image_url(request, job_id)` 依赖 `request`（无 override/config 时回退 `request.base_url`），`_build_leonardo_run_once` 必须接收 `request` 参数，不能传 None。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_leonardo_route.py
import base64
import time
from unittest.mock import MagicMock

import pytest

import requests as req_mod

from core.adobe_client import (
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)
from core.leonardo_client import LeonardoError
from core.leonardo_generation import LeonardoGenerationError
from api.routes.generation import _build_leonardo_run_once, _map_leonardo_error


# --- 异常映射 ---

@pytest.mark.parametrize("message,expected_cls", [
    ("Could not verify JWT: JWSError JWSInvalidSignature", AuthError),
    ("invalid token", AuthError),
    ("unauthorized", AuthError),
    ("insufficient balance", QuotaExhaustedError),
    ("token balance exhausted", QuotaExhaustedError),
    ("graphql HTTP 500", UpstreamTemporaryError),
    ("graphql GetTokenBalance failed after 3 attempts: connection reset", UpstreamTemporaryError),
])
def test_leonardo_error_mapping(message, expected_cls):
    mapped = _map_leonardo_error(LeonardoError(message))
    assert type(mapped) is expected_cls


def test_generation_error_maps_to_non_retryable():
    # 已提交后的失败不可换号重试（会重复扣费）→ AdobeRequestError → 500
    mapped = _map_leonardo_error(LeonardoGenerationError("generation timeout"))
    assert isinstance(mapped, AdobeRequestError)
    mapped = _map_leonardo_error(LeonardoGenerationError("generation failed"))
    assert isinstance(mapped, AdobeRequestError)


# --- _build_leonardo_run_once 成功路径 ---

class _FakeLeoClient:
    def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
        return "gen-abc"


def _fake_img_resp(content: bytes = b"\x89PNG fake image bytes"):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    return resp


def _build_run_once(**overrides):
    base = {
        "leo_client": _FakeLeoClient(),
        "request": MagicMock(),
        "prompt": "a red fox",
        "model_id": "7418e71f-4133-4e1b-9895-bee19f48f2ce",
        "size": None,
        "aspect_ratio": "1:1",
        "n": 1,
        "timeout": 300,
        "response_format": "b64_json",
        "resolved_model_id": "leonardo-nano-banana-2",
        "output_resolution": "2K",
        "public_image_url": lambda req, job_id: f"https://example.com/generated/{job_id}",
        "generated_dir": None,
        "on_generated_file_written": None,
        "set_request_preview": None,
    }
    base.update(overrides)
    return _build_leonardo_run_once(**base)


def test_run_once_success_b64(monkeypatch):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _fake_img_resp())

    result = _build_run_once()("leo-token")
    assert result["model"] == "leonardo-nano-banana-2"
    assert len(result["data"]) == 1
    assert result["data"][0]["b64_json"] == base64.b64encode(b"\x89PNG fake image bytes").decode()
    assert result["data"][0]["revised_prompt"] == "a red fox"
    assert result["usage"]["total_tokens"] > 0


def test_run_once_success_url(monkeypatch, tmp_path):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", lambda url, timeout, headers: _fake_img_resp())

    gen_dir = tmp_path / "generated"
    gen_dir.mkdir()
    run_once = _build_run_once(
        response_format="url", generated_dir=gen_dir,
        on_generated_file_written=lambda p, a, b: None,
    )
    result = run_once("leo-token")
    assert result["data"][0]["url"].startswith("https://example.com/generated/")
    assert len(list(gen_dir.iterdir())) == 1  # CDN 图片确实落盘


def test_run_once_auth_error_mapped(monkeypatch):
    import core.leonardo_generation as lg

    def boom(**kw):
        raise LeonardoError("Could not verify JWT: JWSError JWSInvalidSignature")

    monkeypatch.setattr(lg, "generate_images", boom)
    with pytest.raises(AuthError):
        _build_run_once()("leo-token")


def test_run_once_generation_error_mapped_non_retryable(monkeypatch):
    import core.leonardo_generation as lg

    def boom(**kw):
        raise LeonardoGenerationError("generation timeout")

    monkeypatch.setattr(lg, "generate_images", boom)
    with pytest.raises(AdobeRequestError):
        _build_run_once()("leo-token")


def test_run_once_cdn_fetch_failure_non_retryable(monkeypatch):
    import core.leonardo_generation as lg

    def fake_generate_images(**kw):
        return {
            "created": int(time.time()),
            "data": [{"url": "https://cdn.leonardo.ai/generations/x/img-0.jpg"}],
            "provider": {"generation_id": "gen-abc", "aspect_ratio": "1:1", "model_id": "x"},
        }

    def boom(url, timeout, headers):
        raise req_mod.exceptions.ConnectionError("cdn down")

    monkeypatch.setattr(lg, "generate_images", fake_generate_images)
    monkeypatch.setattr(req_mod, "get", boom)
    # 生成已成功但下载失败 → 不可重试 → AdobeRequestError
    with pytest.raises(AdobeRequestError):
        _build_run_once()("leo-token")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_leonardo_route.py -v`
Expected: 9 FAIL —— `_map_leonardo_error` / `_build_leonardo_run_once` 未定义（ImportError）。

- [ ] **Step 3: Write minimal implementation**

(a) `api/routes/generation.py` 模块顶部 import 增加：

```python
import requests

from core.adobe_client import (
    AdobeRequestError,
    AuthError,
    QuotaExhaustedError,
    UpstreamTemporaryError,
)
from core.leonardo_client import LeonardoClient
```

（`core.adobe_client` 已由 app.py 布线为 `quota_error_cls`/`auth_error_cls`/`upstream_temp_error_cls`，此处直接 import 同名类。）

(b) 模块级常量（`build_generation_router` 之前）：

```python
DEFAULT_LEONARDO_MODEL_ID = "7418e71f-4133-4e1b-9895-bee19f48f2ce"  # Nano Banana 2
```

(c) 模块级异常映射函数（`build_generation_router` 之前）：

```python
def _map_leonardo_error(exc: Exception) -> Exception:
    """Leonardo 异常 → Adobe 异常体系，让 _run_with_token_retries 零改动处理。

    LeonardoGenerationError = 生成已提交后失败（轮询超时/上游 FAILED），换号重试会
    重复扣费 → 非重试 AdobeRequestError → 500。
    LeonardoError = 提交前失败（invalid JWT/HTTP/传输），可换号重试。
    """
    from core.leonardo_generation import LeonardoGenerationError

    if isinstance(exc, LeonardoGenerationError):
        return AdobeRequestError(str(exc))
    message = str(exc).lower()
    if any(kw in message for kw in ("invalid", "jwt", "unauthorized", "verify", "signature")):
        return AuthError(str(exc))
    if any(kw in message for kw in ("quota", "insufficient", "balance", "exhausted", "credits")):
        return QuotaExhaustedError(str(exc))
    return UpstreamTemporaryError(str(exc))
```

(d) 模块级 run_once 工厂（`build_generation_router` 之前）：

```python
def _build_leonardo_run_once(
    *,
    leo_client,
    request: Request,
    prompt: str,
    model_id: str,
    size: Any,
    aspect_ratio: str,
    n: int,
    timeout: int,
    response_format: str,
    resolved_model_id: str,
    output_resolution: str,
    public_image_url: Callable,
    generated_dir: Path,
    on_generated_file_written: Optional[Callable] = None,
    set_request_preview: Optional[Callable] = None,
) -> Callable[[str], dict]:
    from core.leonardo_generation import generate_images, to_aspect

    final_aspect = to_aspect(size=size, aspect_ratio=aspect_ratio)
    _cdn_headers = {"User-Agent": "adobe2api/1.0", "Accept": "image/*"}

    def _run_once(token: str) -> dict:
        from core.leonardo_client import LeonardoError

        try:
            result = generate_images(
                client=leo_client,
                token=token,
                prompt=prompt,
                model_id=model_id,
                size=size,
                aspect_ratio=final_aspect,
                n=n,
                timeout=timeout,
            )
        except LeonardoError as exc:
            raise _map_leonardo_error(exc) from exc

        data_items = []
        for i, item in enumerate(result.get("data") or []):
            url = str(item.get("url") or "").strip()
            try:
                img_resp = requests.get(url, timeout=30, headers=_cdn_headers)
                img_resp.raise_for_status()
            except Exception as fetch_exc:
                # 生成已成功，下载失败不可重试（重试 = 再次扣费）
                raise AdobeRequestError(
                    f"failed to fetch generated image from CDN: {fetch_exc}"
                ) from fetch_exc
            if response_format == "url":
                job_id = f"{result['provider']['generation_id']}-{i}"
                out_path = generated_dir / f"{job_id}.jpg"
                old_size = 0
                try:
                    if out_path.exists():
                        old_size = int(out_path.stat().st_size)
                except Exception:
                    old_size = 0
                out_path.write_bytes(img_resp.content)
                new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                if on_generated_file_written:
                    on_generated_file_written(out_path, old_size, new_size)
                data_item = {"url": public_image_url(request, job_id)}
            else:
                data_item = {"b64_json": base64.b64encode(img_resp.content).decode()}
            data_item["revised_prompt"] = prompt
            data_items.append(data_item)

        if data_items and set_request_preview is not None:
            first = data_items[0]
            if "url" in first:
                set_request_preview(request, first["url"], kind="image")

        return {
            "created": int(time.time()),
            "model": resolved_model_id,
            "data": data_items,
            "usage": build_image_usage(prompt, output_resolution, final_aspect, ()),
        }

    return _run_once
```

(e) `build_generation_router` 签名末尾加可选参数：

```python
    leonardo_client=None,
```

(f) `openai_generate()` 内，`model_conf = resolve_model(resolved_model_id)` 与 `set_request_credit_context(...)` 之后、`try:` 之前加：

```python
        is_leonardo = str(model_conf.get("upstream_model") or "").startswith("leonardo:")
```

`try:` 内 `set_request_task_progress(...)` 之后，把现有结构改为：

```python
            if is_leonardo:
                leo_client = leonardo_client or LeonardoClient()
                run_once = _build_leonardo_run_once(
                    leo_client=leo_client,
                    request=request,
                    prompt=prompt,
                    model_id=DEFAULT_LEONARDO_MODEL_ID,
                    size=data.get("size"),
                    aspect_ratio=ratio,
                    n=data.get("n", 1),
                    timeout=int(data.get("timeout") or 300),
                    response_format=response_format,
                    resolved_model_id=resolved_model_id,
                    output_resolution=output_resolution,
                    public_image_url=public_image_url,
                    generated_dir=generated_dir,
                    on_generated_file_written=on_generated_file_written,
                    set_request_preview=set_request_preview,
                )
                token_selector = lambda: token_manager.get_available(token_type="leonardo")
            else:
                def run_once(token: str):
                    ...  # 现有 Adobe 逻辑原样保留

                token_selector = None

            return run_with_token_retries(
                request=request,
                operation_name="images.generations",
                run_once=run_once,
                token_selector=token_selector,
            )
```

`timeout=int(data.get("timeout") or 300)` 若 `timeout` 非数字会抛 `ValueError` → 被通用 `except Exception` 接住回 500；客户端传脏 timeout 属输入错误，可接受。`n=data.get("n", 1)` 原样传，`clamp_quantity` 负责钳制到 [1,4]。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_leonardo_route.py -v`
Expected: 9 PASS。

- [ ] **Step 5: Run the full regression suite**

Run: `python -m pytest -q`
Expected: 全量 PASS。新增 token_mgr 10 + catalog 4 + leonardo_generation 1 + leonardo_route 9 = 24 个新测试，原有 667 不受影响。

- [ ] **Step 6: Commit**

```bash
git add api/routes/generation.py tests/test_leonardo_route.py
git commit -m "feat(route): /v1/images/generations 支持 Leonardo 模型出图" \
  -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage**：4 个 task 覆盖完整集成链路——token 类型标注与按类型选号（含默认排除）→ 模型注册 → 已提交后失败异常细分（重试安全）→ 路由分叉与出图。Leonardo token 无 refresh profile → `handle_auth_failure` 自然 `report_invalid`，不触发 Adobe cookie 刷新。
- **重试安全（与「Generate 不重试」决策一致）**：`LeonardoGenerationError`（轮询超时/FAILED）与 CDN 下载失败都映射为非重试 `AdobeRequestError` → 500，绝不换号重发；只有提交前失败（invalid JWT/HTTP 错误/只读请求传输异常）才映射为可换号重试的类型。这正是实测烧钱得出的教训。
- **不破坏既有**：`get_available()` 默认 `"adobe"` 使所有现有 Adobe 调用语义等价（现无 type 标注的 token 全被归入 adobe）；`_run_with_token_retries` 签名零改动；`build_generation_router` 新增可选参数默认 None；MODEL_CATALOG 新增条目不影响现有解析；`openai_generate` 只加 if/else 分叉，except 块与 Adobe `run_once` 原样保留。
- **交叉污染防护**：Adobe 所有路由（含直接调 `get_available` 的 `api/generate` runner、entity.py、video_tasks.py）默认排除 leonardo，不会拿 Leonardo Bearer 打 Adobe Firefly 浪费重试/误标 invalid。
- **b64_json 模式**：下载 CDN URL 图片后 base64 编码，`revised_prompt` 两种格式都带，与 Adobe 路径一致。
- **Placeholder scan**：无 TBD/TODO。所有代码块可运行。
- **给实现者**：`_map_leonardo_error` / `_build_leonardo_run_once` 是模块级函数（测试直接 import），异常类从 `core.adobe_client` 直接 import（app.py 布线给 router 的正是 `AdobeRequestError`/`AuthError`/`QuotaExhaustedError`/`UpstreamTemporaryError`）。`core.leonardo_client` 只 import requests，token_mgr 顶部 import 它无循环风险。
