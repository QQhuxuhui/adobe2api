# Gemini Native Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 adobe2api 同端口增加与 sub2api gemini APIKey 账号兼容的 Gemini 原生模型查询、`generateContent`、`streamGenerateContent` 和 `countTokens` 入口，同时复用现有 Adobe token 池、重试、落盘、存储统计和后台日志。

**Architecture:** 保持仓库现有的 FastAPI router builder 结构。协议解析、模型白名单、Google 错误和响应装配集中在新路由 `api/routes/gemini_native.py`；计费画像集中在纯函数模块 `core/models/gemini_usage.py`；AdobeClient 只增加可选 absolute deadline，不改变默认调用；`app.py` 负责共享重试参数、路由注入和动态日志识别。所有阻塞 Adobe 调用通过 Starlette threadpool 执行，SSE 仅在完整生成成功后创建。

**Tech Stack:** Python 3.10+、FastAPI、Starlette、Pydantic v2、requests、curl_cffi、pytest、httpx TestClient。

## Global Constraints

- 以 `docs/superpowers/specs/2026-07-16-gemini-native-endpoint-design.md` v5 为唯一产品合同。若实现细节与设计冲突，先修订设计并得到确认，不得静默偏离。
- 当前工作区已有用户修改：`README.md`、`api/routes/admin.py`、`app.py`、`core/config_mgr.py`、`core/refresh_mgr.py`、`core/token_mgr.py`、`static/*` 和设计文档。实现前后都要运行 `git diff -- <path>`，只叠加本功能修改，不覆盖或格式化用户修改。
- 对已经存在用户修改的文件，不执行整文件 `git add` 或自动提交。下文 commit 命令只在用户修改已经独立提交或隔离后执行；否则以 `git diff --check` 和测试通过作为检查点。
- `reraise_domain=False`、`deadline=None` 必须保持现有 OpenAI、视频和 entity 路径行为。不得为 Gemini 修改公共 `_normalize_image_mime` 的宽松回退行为。
- Gemini 错误响应始终使用 Google JSON 结构。SSE 生成失败必须在创建 `StreamingResponse` 之前返回普通 Google JSON 错误。
- Gemini 日志只保存 URL 中的 model 和解析后的 prompt preview；不得保存 raw request body、`inlineData.data`、解码图片或响应 base64。
- 不修改 sub2api 源码，不实现 OAuth、Code Assist 包裹、`fileData`、`0.5K` 或多候选。`candidateCount` 大于 1 必须返回 400。
- 不改静态管理后台。新 deadline 配置通过默认配置、示例 JSON 和现有管理配置 API 暴露。
- 每个任务严格执行 RED、GREEN、REGRESSION：先写测试并看到预期失败，再实现最小代码，最后运行任务级测试和已有测试。

## File Map

**Create**

- `core/models/gemini_usage.py`：Gemini prompt、image、candidate 和 total token 的单一真源。
- `api/routes/gemini_native.py`：模型表、有限请求体读取、结构解析、输入图限制、Google 错误、路由和响应装配。
- `tests/test_gemini_usage.py`：usage 公式和字段身份测试。
- `tests/test_gemini_parser.py`：请求体限流、深层类型、扁平化、图片资源和模型族参数测试。
- `tests/test_adobe_deadline.py`：requests/curl 固定超时裁剪和 deadline 透传测试。
- `tests/test_token_retry_deadline.py`：领域异常重抛、池空和退避预算测试。
- `tests/test_gemini_native.py`：路由、生成、落盘、SSE、错误映射和大响应集成测试。
- `requirements-test.txt`：固定与生产 FastAPI 版本兼容的 TestClient 依赖。

**Modify**

- `core/adobe_client.py`：为 image upload/generate 所经网络调用增加 optional deadline。
- `core/config_mgr.py`：增加 `gemini_native_deadline_seconds=500` 默认值。
- `config/config.example.json`：记录 deadline 示例值。
- `api/schemas.py`：允许管理配置 API 接收 deadline。
- `api/routes/admin.py`：校验 deadline 为正整数。
- `app.py`：共享重试支持 domain re-raise/deadline、注册 router、动态日志和 state 回填。
- `README.md`：记录原生端点、模型、鉴权、sub2api 128 MiB 和超时部署门槛。

## Task 0: Freeze the Baseline and Test Tooling

**Files:**

- Create: `requirements-test.txt`
- Inspect only: every pre-existing modified path listed in Global Constraints

- [ ] **Step 1: Record the exact starting state**

Run:

```bash
git status --short
git diff -- README.md api/routes/admin.py app.py core/config_mgr.py core/refresh_mgr.py core/token_mgr.py static/admin.css static/admin.html static/admin.js static/login.html docs/superpowers/specs/2026-07-16-gemini-native-endpoint-design.md
pytest -q
```

Expected: the listed user edits remain visible; baseline reports `3 passed`.

- [ ] **Step 2: Add reproducible test dependencies**

Create `requirements-test.txt`:

```text
-r requirements.txt
pytest>=8.3,<10
httpx>=0.27,<0.28
```

The `<0.28` bound is required by the Starlette version selected by the pinned production `fastapi==0.109.2`. Do not add pytest/httpx to production `requirements.txt`.

- [ ] **Step 3: Verify TestClient under the declared test environment**

Run in a clean virtual environment or CI image:

```bash
python -m pip install -r requirements-test.txt
python -c "from fastapi import FastAPI; from fastapi.testclient import TestClient; TestClient(FastAPI()); print('TestClient OK')"
```

Expected: `TestClient OK`.

- [ ] **Step 4: Checkpoint**

```bash
git diff --check
git add requirements-test.txt
git diff --cached --check
git commit -m "test: add reproducible FastAPI test dependencies"
```

This commit is safe only if `requirements-test.txt` is the sole staged file.

## Task 1: Implement the Gemini Usage Single Source

**Files:**

- Create: `core/models/gemini_usage.py`
- Create: `tests/test_gemini_usage.py`
- Reference: `core/models/resolver.py:39`

- [ ] **Step 1: Write failing usage tests**

Cover these exact assertions in `tests/test_gemini_usage.py`:

```python
import core.models.gemini_usage as usage


def test_empty_text_does_not_create_a_text_token():
    total, details = usage.build_prompt_usage("", 1, "pro")
    assert total == 560
    assert details == [{"modality": "IMAGE", "tokenCount": 560}]


def test_pro_2k_usage_has_pro_identity(monkeypatch):
    values = iter([90, 155])
    monkeypatch.setattr(usage, "gemini_usage_rand", lambda low, high: next(values))
    result = usage.build_image_usage_metadata("abcd", 2, "pro", "2K")
    assert result["promptTokenCount"] == 1121
    assert result["candidatesTokenCount"] == 1210
    assert result["thoughtsTokenCount"] == 155
    assert result["totalTokenCount"] == 2486
    assert result["candidatesTokensDetails"] == [
        {"modality": "IMAGE", "tokenCount": 1120}
    ]
    assert result["serviceTier"] == "standard"
    assert "trafficType" not in result


def test_flash_2k_usage_has_flash_identity(monkeypatch):
    monkeypatch.setattr(usage, "gemini_usage_rand", lambda low, high: 411)
    result = usage.build_image_usage_metadata("abcd", 1, "flash", "2K")
    assert result["promptTokenCount"] == 1121
    assert result["candidatesTokenCount"] == 2091
    assert result["totalTokenCount"] == 3212
    assert result["candidatesTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": 411},
        {"modality": "IMAGE", "tokenCount": 1680},
    ]
    assert result["trafficType"] == "ON_DEMAND"
    assert "thoughtsTokenCount" not in result
    assert "serviceTier" not in result


def test_canned_usage_is_deterministic():
    result = usage.build_canned_usage_metadata("ping")
    assert usage.CANNED_TEXT == "ok"
    assert result["candidatesTokenCount"] == 1
    assert result["totalTokenCount"] == result["promptTokenCount"] + 1
    assert result["serviceTier"] == "standard"
    assert "thoughtsTokenCount" not in result
    assert "candidatesTokensDetails" not in result
```

Add parameterized checks for every output band:

- pro IMAGE tokens: 1K=1120, 2K=1120, 4K=2000.
- flash IMAGE tokens: 1K=1120, 2K=1680, 4K=2520.
- pro random bounds: text 78-92/80-100/92-112; thoughts 115-140/145-165/150-170.
- flash random bounds: 250-320/380-440/520-600.
- input image price: pro=560 and flash=1120 per image.
- every `totalTokenCount` equals prompt + candidates + optional thoughts.
- `build_count_tokens_response` returns prompt-side totals only and omits TEXT detail for empty text.

- [ ] **Step 2: Run the test and confirm RED**

```bash
pytest -q tests/test_gemini_usage.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'core.models.gemini_usage'`.

- [ ] **Step 3: Implement the complete usage module**

Use this public surface in `core/models/gemini_usage.py`:

```python
from __future__ import annotations

from random import randint
from typing import Callable

from core.models.resolver import _count_text_tokens

CANNED_TEXT = "ok"
gemini_usage_rand: Callable[[int, int], int] = randint

_INPUT_IMAGE_TOKENS = {"pro": 560, "flash": 1120}
_IMAGE_OUTPUT_TOKENS = {
    "pro": {"1K": 1120, "2K": 1120, "4K": 2000},
    "flash": {"1K": 1120, "2K": 1680, "4K": 2520},
}
_PRO_TEXT_BOUNDS = {"1K": (78, 92), "2K": (80, 100), "4K": (92, 112)}
_PRO_THOUGHT_BOUNDS = {"1K": (115, 140), "2K": (145, 165), "4K": (150, 170)}
_FLASH_TEXT_BOUNDS = {"1K": (250, 320), "2K": (380, 440), "4K": (520, 600)}


def count_text_tokens(text: str) -> int:
    value = str(text or "")
    return 0 if not value else _count_text_tokens(value)


def build_prompt_usage(
    prompt: str, image_count: int, family: str
) -> tuple[int, list[dict[str, int | str]]]:
    text_tokens = count_text_tokens(prompt)
    safe_image_count = max(0, int(image_count or 0))
    image_tokens = safe_image_count * _INPUT_IMAGE_TOKENS.get(family, 0)
    details: list[dict[str, int | str]] = []
    if text_tokens > 0:
        details.append({"modality": "TEXT", "tokenCount": text_tokens})
    if image_tokens > 0:
        details.append({"modality": "IMAGE", "tokenCount": image_tokens})
    return text_tokens + image_tokens, details


def build_count_tokens_response(prompt: str, image_count: int, family: str) -> dict:
    total, details = build_prompt_usage(prompt, image_count, family)
    result = {"totalTokens": total}
    if details:
        result["promptTokensDetails"] = details
    return result


def build_image_usage_metadata(
    prompt: str, image_count: int, family: str, image_size: str
) -> dict:
    normalized_size = str(image_size or "1K").upper()
    if family not in _IMAGE_OUTPUT_TOKENS or normalized_size not in {"1K", "2K", "4K"}:
        raise ValueError("unsupported Gemini usage profile")

    prompt_tokens, prompt_details = build_prompt_usage(prompt, image_count, family)
    image_output = _IMAGE_OUTPUT_TOKENS[family][normalized_size]
    result: dict[str, object] = {"promptTokenCount": prompt_tokens}
    if prompt_details:
        result["promptTokensDetails"] = prompt_details

    if family == "pro":
        text_output = gemini_usage_rand(*_PRO_TEXT_BOUNDS[normalized_size])
        thoughts = gemini_usage_rand(*_PRO_THOUGHT_BOUNDS[normalized_size])
        candidates = image_output + text_output
        result.update(
            {
                "candidatesTokenCount": candidates,
                "candidatesTokensDetails": [
                    {"modality": "IMAGE", "tokenCount": image_output}
                ],
                "thoughtsTokenCount": thoughts,
                "totalTokenCount": prompt_tokens + candidates + thoughts,
                "serviceTier": "standard",
            }
        )
        return result

    text_output = gemini_usage_rand(*_FLASH_TEXT_BOUNDS[normalized_size])
    candidates = image_output + text_output
    result.update(
        {
            "candidatesTokenCount": candidates,
            "candidatesTokensDetails": [
                {"modality": "TEXT", "tokenCount": text_output},
                {"modality": "IMAGE", "tokenCount": image_output},
            ],
            "totalTokenCount": prompt_tokens + candidates,
            "trafficType": "ON_DEMAND",
        }
    )
    return result


def build_canned_usage_metadata(prompt: str) -> dict:
    prompt_tokens, prompt_details = build_prompt_usage(prompt, 0, "text")
    candidate_tokens = count_text_tokens(CANNED_TEXT)
    result: dict[str, object] = {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": candidate_tokens,
        "totalTokenCount": prompt_tokens + candidate_tokens,
        "serviceTier": "standard",
    }
    if prompt_details:
        result["promptTokensDetails"] = prompt_details
    return result
```

- [ ] **Step 4: Run GREEN and regression**

```bash
pytest -q tests/test_gemini_usage.py
pytest -q tests/test_sse_usage.py
```

Expected: all tests pass.

- [ ] **Step 5: Checkpoint**

```bash
git diff --check
git add core/models/gemini_usage.py tests/test_gemini_usage.py
git diff --cached --check
git commit -m "feat: add Gemini usage metadata profiles"
```

## Task 2: Parse Gemini Requests Safely and Deterministically

**Files:**

- Create: `api/routes/gemini_native.py`
- Create: `tests/test_gemini_parser.py`
- Reference: `core/models/catalog.py:19`
- Reference: `core/models/payloads.py`

- [ ] **Step 1: Write parser tests before route handlers**

Build direct unit tests for exported `read_limited_body`, `resolve_model_action` and `parse_gemini_request`. Use `asyncio.run` for the async body reader so no pytest async plugin is required.

The test matrix must include:

- raw top-level `[]`, `null`, string and number.
- malformed JSON and invalid UTF-8.
- missing, empty, object or string `contents`.
- non-object content, non-array parts, non-object part.
- non-string text.
- non-object inlineData and empty/non-string `data` or `mimeType`.
- valid unknown part objects such as `fileData` are ignored.
- systemInstruction text is placed before every content text; all content turns are preserved in order.
- empty text plus one image is valid; empty text plus no image is 400.
- camelCase `inlineData` and snake_case `inline_data` both work.
- only the first six structurally valid images are decoded and returned.
- allowed MIME values normalize `image/jpg` to `image/jpeg`; every other MIME is 400.
- bad base64, decoded image above 10 MiB and total decoded images above 30 MiB are 400.
- encoded data above `4 * ceil(10 MiB / 3)` is rejected before `base64.b64decode` is invoked.
- pro permits only 1:1, 16:9, 9:16, 4:3, 3:4.
- flash permits the pro ratios plus 1:8, 1:4, 4:1, 8:1.
- default aspect ratio is 1:1; default image size is 1K.
- imageSize is case-insensitive; 0.5K and unknown values are 400.
- candidateCount absent or integer 1 succeeds; boolean, zero, string, and values above 1 are 400.
- all four image aliases and all four canned aliases resolve; unknown model/action is 404.
- a fake Content-Length above 48 MiB fails before receive is consumed.
- chunked or missing-length content crossing 48 MiB fails during stream accumulation.

Use a Request helper with an exact receive sequence:

```python
import asyncio

from starlette.requests import Request


def read_chunks(chunks: list[bytes], content_length: int | None = None) -> bytes:
    pending = list(chunks)

    async def receive():
        body = pending.pop(0) if pending else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(pending),
        }

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1beta/models/test:generateContent",
            "headers": headers,
            "query_string": b"",
        },
        receive,
    )
    return asyncio.run(read_limited_body(request))
```

- [ ] **Step 2: Run RED**

```bash
pytest -q tests/test_gemini_parser.py
```

Expected: imports from `api.routes.gemini_native` fail because the module has not been implemented.

- [ ] **Step 3: Add the protocol constants and typed records**

Define these exact concepts in `api/routes/gemini_native.py`:

```python
from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass
from typing import Any, Sequence

from fastapi import Request

GEMINI_NATIVE_MAX_BODY_BYTES = 48 * 1024 * 1024
GEMINI_MAX_IMAGE_BYTES = 10 * 1024 * 1024
GEMINI_MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
GEMINI_MAX_IMAGES = 6
GEMINI_MAX_ENCODED_IMAGE_CHARS = 4 * math.ceil(GEMINI_MAX_IMAGE_BYTES / 3)

PRO_RATIOS = frozenset({"1:1", "16:9", "9:16", "4:3", "3:4"})
FLASH_RATIOS = frozenset({*PRO_RATIOS, "1:8", "1:4", "4:1", "8:1"})
TEST_TEXT_MODELS = frozenset(
    {
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    }
)


@dataclass(frozen=True)
class GeminiModelSpec:
    model_id: str
    display_name: str
    family: str
    upstream_model_id: str | None
    upstream_model_version: str | None
    aspect_ratios: frozenset[str]


@dataclass(frozen=True)
class ParsedGeminiRequest:
    prompt: str
    images: Sequence[tuple[bytes, str]]
    aspect_ratio: str
    image_size: str
    candidate_count: int


class GeminiNativeError(Exception):
    def __init__(self, code: int, message: str, status: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.status = str(status)
```

Populate `GEMINI_MODELS` with exactly these mappings:

| Model IDs | family | upstream modelId | upstream modelVersion | ratios |
|---|---|---|---|---|
| `gemini-3-pro-image`, `gemini-3-pro-image-preview` | pro | gemini-flash | nano-banana-2 | PRO_RATIOS |
| `gemini-3.1-flash-image`, `gemini-3.1-flash-image-preview` | flash | gemini-flash | nano-banana-3 | FLASH_RATIOS |
| every TEST_TEXT_MODELS entry | text | null | null | empty |

- [ ] **Step 4: Implement bounded body reading**

Use this complete behavior:

```python
async def read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > GEMINI_NATIVE_MAX_BODY_BYTES:
            raise GeminiNativeError(400, "Request body is too large", "INVALID_ARGUMENT")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > GEMINI_NATIVE_MAX_BODY_BYTES:
            raise GeminiNativeError(400, "Request body is too large", "INVALID_ARGUMENT")
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    request._body = raw_body
    return raw_body
```

Do not call `request.body()` or `request.json()` anywhere in the Gemini route.

- [ ] **Step 5: Implement structural validation and flattening**

`parse_gemini_request(raw_body, model_spec)` must perform operations in this order:

1. Decode UTF-8 and call `json.loads`; translate `UnicodeDecodeError` and `json.JSONDecodeError` to `GeminiNativeError(400, "Invalid JSON request body", "INVALID_ARGUMENT")`.
2. Validate the root object and the complete nested type matrix before extracting values.
3. Validate `generationConfig` as an object. Validate its nested `imageConfig` as an object. Read aspect ratio and image size only from `generationConfig.imageConfig`.
4. Add `systemInstruction.parts[].text` first, then every `contents[].parts[].text`, preserving order and joining non-empty text with newline.
5. For every content part, accept `inlineData` or `inline_data`. Validate `data` and `mimeType` strings on every occurrence; decode only the first six images.
6. Check encoded length, MIME, strict base64, decoded single size and running total in that order.
7. Normalize `image/jpg` to `image/jpeg` only after whitelist validation.
8. Reject an empty prompt only when no accepted input image exists.
9. Reject unsupported ratios, sizes and candidateCount without fallback.

The image decoder is a separate complete helper so the pre-decode test can monkeypatch it:

```python
def decode_inline_image(data: str, mime_type: str) -> tuple[bytes, str]:
    normalized_mime = str(mime_type).strip().lower()
    if normalized_mime not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
        raise GeminiNativeError(400, "Unsupported inline image MIME type", "INVALID_ARGUMENT")
    if len(data) > GEMINI_MAX_ENCODED_IMAGE_CHARS:
        raise GeminiNativeError(400, "Inline image exceeds 10 MiB", "INVALID_ARGUMENT")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise GeminiNativeError(400, "Invalid inline image base64", "INVALID_ARGUMENT")
    if len(decoded) > GEMINI_MAX_IMAGE_BYTES:
        raise GeminiNativeError(400, "Inline image exceeds 10 MiB", "INVALID_ARGUMENT")
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"
    return decoded, normalized_mime
```

- [ ] **Step 6: Run GREEN and scan payload ratio compatibility**

```bash
pytest -q tests/test_gemini_parser.py
pytest -q tests/test_gemini_usage.py tests/test_sse_usage.py
rg -n '"1:1"|"16:9"|"9:16"|"4:3"|"3:4"|"1:8"|"1:4"|"4:1"|"8:1"' core/models/payloads.py
```

Expected: parser tests pass and every ratio exposed by the route has an exact nano payload size mapping.

- [ ] **Step 7: Checkpoint**

```bash
git diff --check
git add api/routes/gemini_native.py tests/test_gemini_parser.py
git diff --cached --check
git commit -m "feat: parse bounded Gemini native requests"
```

## Task 3: Propagate an Absolute Deadline Through AdobeClient

**Files:**

- Modify: `core/adobe_client.py:298`
- Modify: `core/adobe_client.py:371`
- Modify: `core/adobe_client.py:437`
- Modify: `core/adobe_client.py:511`
- Modify: `core/adobe_client.py:602`
- Modify: `core/adobe_client.py:641`
- Modify: `core/adobe_client.py:1470`
- Create: `tests/test_adobe_deadline.py`

- [ ] **Step 1: Write failing deadline tests**

Tests must prove:

- `_post_json`, `_post_bytes`, `_get` and `_download_to_file` accept `deadline`.
- deadline=None keeps fixed 60/30 second values.
- an absolute deadline 0.25 seconds away passes a positive timeout no greater than 0.25.
- an expired deadline raises `UpstreamTemporaryError(error_type="timeout")` before requests or curl is called.
- a fake CurlSession constructor receives the cropped timeout.
- the requests fallback after curl status 451 recomputes remaining time instead of reusing 60.
- `upload_image` forwards deadline to `_post_bytes`.
- `generate` forwards the same deadline to submit, every poll, and file download.
- `generate` with deadline exhausted raises UpstreamTemporaryError; `generate` with no deadline and local polling timeout retains AdobeRequestError.
- polling sleep never exceeds request deadline remaining time.

Run:

```bash
pytest -q tests/test_adobe_deadline.py
```

Expected RED: unexpected `deadline` keyword or missing timeout helper.

- [ ] **Step 2: Add one timeout calculation helper and parameterize CurlSession**

Add to `AdobeClient`:

```python
@staticmethod
def _timeout_for_deadline(timeout: float, deadline: Optional[float]) -> float:
    fixed_timeout = max(0.001, float(timeout))
    if deadline is None:
        return fixed_timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise UpstreamTemporaryError(
            "Gemini native request deadline exceeded",
            status_code=503,
            error_type="timeout",
        )
    return min(fixed_timeout, remaining)


def _session(self, timeout: float = 60, deadline: Optional[float] = None):
    if CurlSession is None:
        return None
    effective_timeout = self._timeout_for_deadline(timeout, deadline)
    kwargs = {"impersonate": self.impersonate, "timeout": effective_timeout}
    if self.proxy:
        kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
    return CurlSession(**kwargs)
```

- [ ] **Step 3: Thread optional deadline through only the image-generation network path**

Use these signatures and preserve every existing default:

```python
def _post_json(
    self,
    url: str,
    headers: dict,
    payload: dict,
    deadline: Optional[float] = None,
):

def _post_bytes(
    self,
    url: str,
    headers: dict,
    payload: bytes,
    deadline: Optional[float] = None,
):

def _get(
    self,
    url: str,
    headers: dict,
    timeout: int = 60,
    deadline: Optional[float] = None,
):

def _download_to_file(
    self,
    url: str,
    headers: Optional[dict],
    out_path: Path,
    timeout: int = 60,
    chunk_size: int = 1024 * 1024,
    deadline: Optional[float] = None,
) -> int:

def upload_image(
    self,
    token: str,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    deadline: Optional[float] = None,
) -> str:
```

At every requests call, calculate timeout immediately before the call:

```python
effective_timeout = self._timeout_for_deadline(60, deadline)
```

For `_get` and `_download_to_file`, use their `timeout` parameter instead of literal 60. For curl, pass the same fixed timeout and deadline into `_session`. During `_download_to_file` chunk iteration, call `_timeout_for_deadline(timeout, deadline)` before writing each chunk so a long streaming download cannot silently run past the absolute budget.

- [ ] **Step 4: Add deadline to image generate without changing legacy local timeout semantics**

Add `deadline: Optional[float] = None` after `progress_cb` in `generate`. Pass it to submit, poll, memory download and file download.

Use `time.monotonic()` for budget checks, including replacing `start = time.time()` with `start = time.monotonic()`. Check request deadline before local generate timeout:

```python
if deadline is not None and time.monotonic() >= deadline:
    raise UpstreamTemporaryError(
        "Gemini native request deadline exceeded",
        status_code=503,
        error_type="timeout",
    )
if time.monotonic() - start > timeout:
    raise AdobeRequestError("generation timed out")
```

Before polling sleep, calculate both remaining budgets and sleep only the smallest positive interval:

```python
local_remaining = max(0.0, float(timeout) - (time.monotonic() - start))
sleep_for = min(sleep_time, local_remaining)
if deadline is not None:
    request_remaining = max(0.0, deadline - time.monotonic())
    sleep_for = min(sleep_for, request_remaining)
if sleep_for > 0:
    time.sleep(sleep_for)
```

When `deadline=None`, submit/upload/poll/download keep their current fixed network timeouts and a local generation timeout remains `AdobeRequestError`.

- [ ] **Step 5: Run GREEN and regression**

```bash
pytest -q tests/test_adobe_deadline.py
pytest -q
python -m compileall -q core/adobe_client.py
```

Expected: all tests pass and no syntax errors.

- [ ] **Step 6: Checkpoint**

```bash
git diff --check
git add core/adobe_client.py tests/test_adobe_deadline.py
git diff --cached --check
git commit -m "feat: bound Adobe image calls by absolute deadline"
```

## Task 4: Preserve Domain Errors and Bound Retry Backoff

**Files:**

- Modify: `app.py:599`
- Create: `tests/test_token_retry_deadline.py`
- Modify: `core/config_mgr.py:17`
- Modify: `config/config.example.json`
- Modify: `api/schemas.py:29`
- Modify: `api/routes/admin.py:430`

- [ ] **Step 1: Write retry compatibility tests**

Patch the `app.client` and `app.token_manager` globals with deterministic fakes. Test all of these:

- `reraise_domain=True` ends with the original QuotaExhaustedError.
- `reraise_domain=True` ends with the original AuthError after all accounts are invalid.
- `reraise_domain=True` ends with the original UpstreamTemporaryError.
- empty pool with `reraise_domain=True` raises a new UpstreamTemporaryError with status 503.
- empty pool with the default flag still raises FastAPI HTTPException 503.
- `reraise_domain=False` retains current quota/auth/temp HTTP mappings.
- an AdobeRequestError in domain mode is re-raised so the Gemini router can emit Google INTERNAL; default mode remains HTTPException.
- expired deadline is checked before token selection and before `run_once`.
- retry delay is cropped to remaining time, then budget exhaustion raises UpstreamTemporaryError without a new attempt.
- token success/exhaustion/invalid reporting and attempt log calls still occur.

Run:

```bash
pytest -q tests/test_token_retry_deadline.py
```

Expected RED: `_run_with_token_retries` rejects the new keyword arguments.

- [ ] **Step 2: Extend the retry signature and deadline guard**

Use this signature:

```python
def _run_with_token_retries(
    request: Request,
    operation_name: str,
    run_once: Callable[[str], Any],
    set_request_error_detail: Optional[Callable] = None,
    token_selector: Optional[Callable[[], Optional[str]]] = None,
    reraise_domain: bool = False,
    deadline: Optional[float] = None,
) -> Any:
```

Inside the function add a local guard and call it at the top of each outer attempt, before selecting a token, and immediately before `run_once(token)`:

```python
def _ensure_deadline() -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise UpstreamTemporaryError(
            "Gemini native request deadline exceeded",
            status_code=503,
            error_type="timeout",
        )
```

Replace the retry sleep block with:

```python
if delay > 0:
    sleep_for = delay
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _ensure_deadline()
        sleep_for = min(sleep_for, remaining)
    if sleep_for > 0:
        time.sleep(sleep_for)
    _ensure_deadline()
```

- [ ] **Step 3: Preserve domain exceptions only when explicitly requested**

In the AdobeRequestError catch, retain existing logging and then branch:

```python
if reraise_domain:
    raise
raise HTTPException(status_code=status_code, detail=detail)
```

Replace the function tail with this complete branch:

```python
if last_exc is not None:
    if reraise_domain and isinstance(
        last_exc, (AuthError, QuotaExhaustedError, UpstreamTemporaryError)
    ):
        raise last_exc
    if isinstance(last_exc, AuthError):
        raise HTTPException(
            status_code=401, detail="All available tokens are invalid or expired"
        )
    if isinstance(last_exc, (QuotaExhaustedError, UpstreamTemporaryError)):
        raise HTTPException(
            status_code=503,
            detail="Upstream is temporarily unavailable. Please retry later.",
        )
    raise last_exc
if reraise_domain:
    raise UpstreamTemporaryError(
        "No active tokens available in the pool",
        status_code=503,
        error_type="upstream_unavailable",
    )
raise HTTPException(status_code=503, detail="No active tokens available in the pool")
```

- [ ] **Step 4: Add the positive deadline configuration**

Add to the ConfigManager defaults:

```python
"gemini_native_deadline_seconds": 500,
```

Add to `config/config.example.json` between `generate_timeout` and `refresh_interval_hours`:

```json
"generate_timeout": 300,
"gemini_native_deadline_seconds": 500,
"refresh_interval_hours": 15
```

Add to `ConfigUpdateRequest`:

```python
gemini_native_deadline_seconds: Optional[int] = None
```

Add this exact validation to `update_config`:

```python
if "gemini_native_deadline_seconds" in incoming:
    try:
        gemini_deadline = int(incoming["gemini_native_deadline_seconds"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="gemini_native_deadline_seconds must be a positive integer",
        )
    if gemini_deadline <= 0:
        raise HTTPException(
            status_code=400,
            detail="gemini_native_deadline_seconds must be a positive integer",
        )
    update_data["gemini_native_deadline_seconds"] = gemini_deadline
```

Do not add a static admin form field in this feature.

- [ ] **Step 5: Run GREEN and legacy regression**

```bash
pytest -q tests/test_token_retry_deadline.py tests/test_adobe_deadline.py
pytest -q
python -m compileall -q app.py api core
```

Expected: retry compatibility assertions and all previous tests pass.

- [ ] **Step 6: Dirty-file checkpoint**

```bash
git diff --check
git diff -- app.py core/config_mgr.py api/routes/admin.py api/schemas.py config/config.example.json
```

Do not stage these paths while they still contain pre-existing user edits. After those edits are independently isolated, the intended commit is:

```bash
git add app.py core/config_mgr.py api/routes/admin.py api/schemas.py config/config.example.json tests/test_token_retry_deadline.py
git diff --cached --check
git commit -m "feat: add Gemini retry and deadline controls"
```

## Task 5: Implement Gemini Native Routes and Generation

**Files:**

- Modify: `api/routes/gemini_native.py`
- Create: `tests/test_gemini_native.py`
- Reference: `api/routes/generation.py:18`
- Reference: `api/routes/generation.py:730`

- [ ] **Step 1: Write isolated router integration tests**

Build a small FastAPI app around `build_gemini_native_router` with fake config, client, retry function, generated directory and callbacks. Do not import global `app.py` in this test file.

Required cases:

- header key and query key succeed; missing/wrong key returns 401 UNAUTHENTICATED.
- GET list returns exactly eight models, prefixed with `models/`, with three supported methods.
- GET known model returns the same registry entry; unknown returns 404 NOT_FOUND.
- unknown POST model and unknown action return 404 before body parsing or Adobe calls.
- countTokens uses all text turns plus image input price and never calls upload/generate.
- all four text whitelist models return `CANNED_TEXT="ok"` for non-stream and one SSE event for stream.
- text whitelist plus inlineData returns 400 and never calls Adobe.
- pro non-stream response contains PNG inlineData, requested modelVersion, responseId, STOP, index 0 and pro usage.
- flash response omits candidate index and has flash usage identity.
- stream success returns exactly one `data: <json>\n\n`, with no keepalive and no `[DONE]`.
- stream generation error returns ordinary HTTP 503 Google JSON and Content-Type is not event-stream.
- Quota/Auth/UpstreamTemporary/AdobeRequestError map to 429/401/503/500 and correct status enums.
- uploads occur inside `run_once`: fake first token failure, second token success, and both tokens receive upload calls.
- generate receives out_path and deadline; the fake writes a real file and returns `(None, metadata)`.
- successful route reads the file back, calls storage accounting once, and sets image preview.
- failed generate deletes a partial file and does not call storage accounting.
- a generated payload whose base64 is at least 8 MiB returns complete JSON in-process.

Run:

```bash
pytest -q tests/test_gemini_native.py
```

Expected RED: router builder is not present.

- [ ] **Step 2: Add Google response and error builders**

Use compact JSON for the single SSE line:

```python
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.models.gemini_usage import (
    CANNED_TEXT,
    build_canned_usage_metadata,
    build_count_tokens_response,
    build_image_usage_metadata,
)


def google_error(error: GeminiNativeError) -> JSONResponse:
    return JSONResponse(
        status_code=error.code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "status": error.status,
            }
        },
    )


def model_resource(spec: GeminiModelSpec) -> dict:
    return {
        "name": f"models/{spec.model_id}",
        "displayName": spec.display_name,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
            "countTokens",
        ],
    }


def build_image_response(
    spec: GeminiModelSpec,
    parsed: ParsedGeminiRequest,
    image_bytes: bytes,
) -> dict:
    candidate = {
        "content": {
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            ],
            "role": "model",
        },
        "finishReason": "STOP",
    }
    if spec.family == "pro":
        candidate["index"] = 0
    return {
        "candidates": [candidate],
        "usageMetadata": build_image_usage_metadata(
            parsed.prompt,
            len(parsed.images),
            spec.family,
            parsed.image_size,
        ),
        "modelVersion": spec.model_id,
        "responseId": str(uuid.uuid4()),
    }


def build_canned_response(spec: GeminiModelSpec, prompt: str) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": CANNED_TEXT}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": build_canned_usage_metadata(prompt),
        "modelVersion": spec.model_id,
        "responseId": str(uuid.uuid4()),
    }


def sse_response(payload: dict) -> StreamingResponse:
    event = f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    return StreamingResponse(iter([event]), media_type="text/event-stream")
```

Import `uuid` in the module. The builders above make pro the only family with candidate `index: 0`; flash omits it and canned text has no image-family identity field.

- [ ] **Step 3: Implement the router builder boundary**

Use this dependency surface so tests remain isolated and app wiring remains explicit:

```python
def build_gemini_native_router(
    *,
    config_manager,
    client,
    generated_dir,
    run_with_token_retries,
    set_request_error_detail,
    set_request_task_progress,
    set_request_logging_fields,
    set_request_preview,
    public_image_url,
    on_generated_file_written,
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    adobe_error_cls,
    logger,
):
```

Inside the builder:

- authorize every endpoint by comparing `x-goog-api-key` and `?key=` candidates against `config_manager.get("api_key")`.
- define GET `/v1beta/models` before GET `/v1beta/models/{model}`.
- define POST `/v1beta/models/{model_action}` and split with `rpartition(":")`.
- authorize and resolve model/action before reading the body.
- read via `read_limited_body`, parse once, then call `set_request_logging_fields` with model and prompt.
- countTokens and canned paths return without entering threadpool or token pool.
- reject images on family `text` before building canned output.
- accept `alt=sse` on stream calls but never emit a terminator event.

- [ ] **Step 4: Implement the per-token image generation closure**

Calculate the request deadline once:

```python
try:
    deadline_seconds = float(config_manager.get("gemini_native_deadline_seconds", 500))
except (TypeError, ValueError):
    deadline_seconds = 0
if deadline_seconds <= 0:
    raise GeminiNativeError(500, "Invalid Gemini native deadline configuration", "INTERNAL")
deadline = time.monotonic() + deadline_seconds
```

The synchronous `run_once` must follow this order:

```python
def run_once(token: str) -> dict:
    source_ids = [
        client.upload_image(token, image_bytes, mime_type, deadline=deadline)
        for image_bytes, mime_type in parsed.images
    ]
    job_id = uuid.uuid4().hex
    out_path = generated_dir / f"{job_id}.png"
    old_size = int(out_path.stat().st_size) if out_path.exists() else 0

    def progress(update: dict) -> None:
        set_request_task_progress(
            request,
            task_status=str(update.get("task_status") or "IN_PROGRESS"),
            task_progress=update.get("task_progress"),
            upstream_job_id=update.get("upstream_job_id"),
            retry_after=update.get("retry_after"),
            error=update.get("error"),
        )

    try:
        client.generate(
            token=token,
            prompt=parsed.prompt,
            aspect_ratio=parsed.aspect_ratio,
            output_resolution=parsed.image_size,
            upstream_model_id=str(spec.upstream_model_id),
            upstream_model_version=str(spec.upstream_model_version),
            source_image_ids=source_ids,
            timeout=client.generate_timeout,
            out_path=out_path,
            progress_cb=progress,
            deadline=deadline,
        )
        image_bytes = out_path.read_bytes()
        new_size = int(out_path.stat().st_size)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    on_generated_file_written(out_path, old_size, new_size)
    set_request_preview(request, public_image_url(request, job_id), kind="image")
    return build_image_response(spec, parsed, image_bytes)
```

Call the blocking retry loop in the threadpool:

```python
payload = await run_in_threadpool(
    lambda: run_with_token_retries(
        request=request,
        operation_name=f"gemini.{action}",
        run_once=run_once,
        set_request_error_detail=set_request_error_detail,
        reraise_domain=True,
        deadline=deadline,
    )
)
```

Only after this await succeeds may the stream path call `sse_response(payload)`.

- [ ] **Step 5: Map every exception before a response is opened**

Use this table in a single helper that also writes `request.state.log_error`:

| Exception | HTTP | Google status |
|---|---:|---|
| GeminiNativeError | embedded | embedded |
| quota_error_cls | 429 | RESOURCE_EXHAUSTED |
| auth_error_cls | 401 | UNAUTHENTICATED |
| upstream_temp_error_cls | 503 | UNAVAILABLE |
| adobe_error_cls | 500 | INTERNAL |
| any other Exception | 500 | INTERNAL |

Implement the helper inside the router builder so it can use injected exception classes:

```python
def error_response(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, GeminiNativeError):
        error = exc
    elif isinstance(exc, quota_error_cls):
        error = GeminiNativeError(429, "Resource exhausted", "RESOURCE_EXHAUSTED")
    elif isinstance(exc, auth_error_cls):
        error = GeminiNativeError(401, "Authentication failed", "UNAUTHENTICATED")
    elif isinstance(exc, upstream_temp_error_cls):
        error = GeminiNativeError(
            503, "Upstream temporarily unavailable", "UNAVAILABLE"
        )
    elif isinstance(exc, adobe_error_cls):
        error = GeminiNativeError(500, "Image generation failed", "INTERNAL")
    else:
        set_request_error_detail(
            request,
            error=exc,
            status_code=500,
            error_type="server_error",
            include_traceback=True,
        )
        logger.exception("Unhandled Gemini native request error")
        error = GeminiNativeError(500, "Internal server error", "INTERNAL")
    request.state.log_error = error.message
    return google_error(error)
```

Every route body uses `try` and `except Exception as exc: return error_response(request, exc)`. Never place traceback or raw Adobe response text in the Google message.

- [ ] **Step 6: Run GREEN and all pure tests**

```bash
pytest -q tests/test_gemini_native.py
pytest -q tests/test_gemini_parser.py tests/test_gemini_usage.py tests/test_adobe_deadline.py tests/test_token_retry_deadline.py
python -m compileall -q api/routes/gemini_native.py core/models/gemini_usage.py
```

Expected: all isolated route and unit tests pass.

- [ ] **Step 7: Checkpoint**

```bash
git diff --check
git add api/routes/gemini_native.py tests/test_gemini_native.py
git diff --cached --check
git commit -m "feat: add Gemini native generation routes"
```

## Task 6: Register the Router and Integrate Observability

**Files:**

- Modify: `app.py:23`
- Modify: `app.py:132`
- Modify: `app.py:190`
- Modify: `app.py:420`
- Modify: `app.py:1271`
- Modify: `tests/test_gemini_native.py`

- [ ] **Step 1: Add failing full-app logging tests**

Import the full app only in a dedicated test section after monkeypatching `refresh_manager.start` to a no-op when necessary. Replace global log stores with in-memory captures.

Test these exact outcomes:

- GET models logs `gemini.models.list`; GET one logs `gemini.models.get`.
- generate/count/stream paths log `gemini.generateContent`, `gemini.countTokens`, `gemini.streamGenerateContent`.
- model is parsed from the URL before route execution.
- prompt preview is populated from the flattened validated prompt after route parsing.
- canned, countTokens and parser error requests all produce final log records.
- retry attempt records carry the Gemini operation name.
- neither successful log payloads nor ErrorDetailRecord serialized text contains a submitted base64 marker.
- monkeypatching `starlette.requests.Request.body` to raise does not break a Gemini request, proving middleware does not pre-read it.
- existing `/v1/chat/completions` logging still reads and restores its body as before.

Run:

```bash
pytest -q tests/test_gemini_native.py -k logging
```

Expected RED: Gemini paths are not registered or resolved by middleware.

- [ ] **Step 2: Centralize operation and URL model resolution**

Add a pure helper used by both middleware and `_set_request_error_detail`:

```python
def _resolve_request_operation(method: str, path: str) -> str:
    normalized_method = str(method or "").upper()
    if path == "/v1/chat/completions":
        return "chat.completions"
    if path == "/v1/images/generations":
        return "images.generations"
    if path == "/api/v1/generate":
        return "api.generate"
    if path == "/v1/entities" and normalized_method == "POST":
        return "entities.create"
    if path == "/v1beta/models" and normalized_method == "GET":
        return "gemini.models.list"
    prefix = "/v1beta/models/"
    if not path.startswith(prefix):
        return ""
    tail = path[len(prefix):]
    if normalized_method == "GET" and tail and ":" not in tail:
        return "gemini.models.get"
    if normalized_method != "POST":
        return ""
    model, separator, action = tail.rpartition(":")
    if not separator or not model:
        return ""
    return {
        "generateContent": "gemini.generateContent",
        "streamGenerateContent": "gemini.streamGenerateContent",
        "countTokens": "gemini.countTokens",
    }.get(action, "")


def _gemini_model_from_path(path: str) -> Optional[str]:
    prefix = "/v1beta/models/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix):]
    model = tail.rpartition(":")[0] if ":" in tail else tail
    return model or None
```

- [ ] **Step 3: Let the route update validated logging fields**

Add and inject this helper:

```python
def _set_request_logging_fields(
    request: Request, model: Optional[str], prompt: Optional[str]
) -> None:
    normalized_prompt = str(prompt or "").replace("\r", " ").replace("\n", " ").strip()
    request.state.log_model = str(model or "").strip() or None
    request.state.log_prompt_preview = normalized_prompt[:180] or None
    _upsert_live_request(
        request,
        {
            "model": request.state.log_model,
            "prompt_preview": request.state.log_prompt_preview,
            "ts": time.time(),
        },
    )
```

- [ ] **Step 4: Refactor middleware initialization without changing legacy body logging**

At middleware entry:

- obtain operation from `_resolve_request_operation`.
- initialize `request.state.log_id` and the live record for every recognized operation, including GET.
- initialize Gemini model from `_gemini_model_from_path`.
- call `await request.body()` only for recognized non-Gemini POST/PUT/PATCH paths.
- leave Gemini body untouched for the route's finite stream reader.

At final record construction use state first:

```python
final_model = getattr(request.state, "log_model", None) or body_meta.get("model")
final_prompt = getattr(request.state, "log_prompt_preview", None) or body_meta.get(
    "prompt_preview"
)
```

Pass `final_model` and `final_prompt` into RequestLogRecord. Replace the hard-coded op_map in `_set_request_error_detail` with `_resolve_request_operation(request.method, path)`.

- [ ] **Step 5: Register the router with all shared dependencies**

Import `build_gemini_native_router` and include it with:

```python
app.include_router(
    build_gemini_native_router(
        config_manager=config_manager,
        client=client,
        generated_dir=GENERATED_DIR,
        run_with_token_retries=_run_with_token_retries,
        set_request_error_detail=_set_request_error_detail,
        set_request_task_progress=_set_request_task_progress,
        set_request_logging_fields=_set_request_logging_fields,
        set_request_preview=_set_request_preview,
        public_image_url=_public_image_url,
        on_generated_file_written=_on_generated_file_written,
        quota_error_cls=QuotaExhaustedError,
        auth_error_cls=AuthError,
        upstream_temp_error_cls=UpstreamTemporaryError,
        adobe_error_cls=AdobeRequestError,
        logger=logger,
    )
)
```

Register it next to the generation router. No existing route path conflicts with `/v1beta`.

- [ ] **Step 6: Run full integration regression**

```bash
pytest -q tests/test_gemini_native.py
pytest -q
python -m compileall -q app.py api core
```

Expected: all Gemini and existing tests pass.

- [ ] **Step 7: Dirty-file checkpoint**

```bash
git diff --check
git diff -- app.py
```

Verify the diff preserves the user's current app changes. Only after those changes are isolated:

```bash
git add app.py tests/test_gemini_native.py
git diff --cached --check
git commit -m "feat: wire Gemini routes into shared logging"
```

## Task 7: Deployment Documentation and End-to-End Gates

**Files:**

- Modify: `README.md`
- Verify: `Dockerfile`
- Verify: `docker-compose.yml`

- [ ] **Step 1: Add a focused Gemini native README section**

Document all of the following without changing unrelated README text:

- five endpoint forms and both API key locations.
- four image aliases, four canned health-check aliases, and upstream mapping.
- default aspectRatio 1:1, imageSize 1K, candidateCount 1.
- input limits: 48 MiB request, six images, 10 MiB each, 30 MiB total.
- `gemini_native_deadline_seconds` default 500.
- sub2api `gateway.response_header_timeout >= gemini_native_deadline_seconds + 60`; default therefore at least 560 seconds.
- sub2api `gateway.upstream_response_read_max_bytes >= 134217728`.
- non-stream embeds base64; stream emits one final event and is safer for large proxy payloads.
- no OAuth, fileData, 0.5K or multi-candidate support.

Include runnable local examples for list, countTokens, non-stream generation and stream generation. Use `x-goog-api-key` in two and `?key=` in one so both auth forms are exercised.

- [ ] **Step 2: Verify pinned production dependencies and container import**

```bash
docker build -t adobe2api:gemini-native .
docker run --rm adobe2api:gemini-native python -c "import app; print('app import OK')"
```

Expected: image builds with `requirements.txt` pins and app imports successfully.

- [ ] **Step 3: Run local endpoint smoke tests**

Start a development server on an unused port:

```bash
uvicorn app:app --host 127.0.0.1 --port 6001
```

In a second shell:

```bash
curl -sS -H 'x-goog-api-key: YOUR_KEY' http://127.0.0.1:6001/v1beta/models
curl -sS -H 'content-type: application/json' -H 'x-goog-api-key: YOUR_KEY' -d '{"contents":[{"parts":[{"text":"ping"}]}]}' http://127.0.0.1:6001/v1beta/models/gemini-2.0-flash:countTokens
curl -N -H 'content-type: application/json' -H 'x-goog-api-key: YOUR_KEY' -d '{"contents":[{"parts":[{"text":"ping"}]}]}' 'http://127.0.0.1:6001/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse'
```

Expected: official model objects, local count result, and one SSE `data:` event containing `ok`.

- [ ] **Step 4: Run the real sub2api large-response deployment gate**

Before the test, confirm the deployed sub2api effective config contains:

```yaml
gateway:
  response_header_timeout: 560s
  upstream_response_read_max_bytes: 134217728
```

If `gemini_native_deadline_seconds` differs from 500, replace 560 with deadline + 60.

Send a 4K non-stream image request through the sub2api buyer-facing Gemini route and save the response:

```bash
curl -sS --max-time 620 -H 'content-type: application/json' -H 'x-goog-api-key: BUYER_KEY' -d '{"contents":[{"parts":[{"text":"A highly detailed dense technical cutaway illustration with fine texture"}]}],"generationConfig":{"imageConfig":{"aspectRatio":"1:1","imageSize":"4K"},"candidateCount":1}}' 'https://SUB2API_HOST/v1beta/models/gemini-3-pro-image:generateContent' -o /tmp/gemini-via-sub2api.json
python -c "import base64,json,pathlib; p=pathlib.Path('/tmp/gemini-via-sub2api.json'); d=json.loads(p.read_text()); s=d['candidates'][0]['content']['parts'][0]['inlineData']['data']; base64.b64decode(s, validate=True); assert len(s) >= 8*1024*1024, len(s); print(len(s))"
```

Expected: valid JSON, valid base64, encoded image at least 8 MiB, no truncation. If the generated PNG compresses below 8 MiB, repeat with a denser prompt; do not lower the assertion.

Then exercise the same model via `streamGenerateContent?alt=sse` and confirm exactly one complete data event arrives before the configured response-header deadline.

- [ ] **Step 5: Final automated verification**

```bash
pytest -q
python -m compileall -q app.py api core tests
git diff --check
rg -n "inlineData|inline_data" app.py api/routes/gemini_native.py core tests
rg -n "request\.body\(\)|request\.json\(\)" app.py api/routes/gemini_native.py
```

Review the search output manually:

- Gemini middleware branch must not call request.body or request.json.
- runtime logging must not store inlineData data.
- base64 occurrences are limited to bounded decode, response assembly and tests.

- [ ] **Step 6: Final spec coverage review**

Check every section of the v5 design against tests and implementation:

| Design area | Required proof |
|---|---|
| endpoints/auth/model map | route tests + local curl |
| Google errors | parameterized error mapping tests |
| 48 MiB pre-parse limit | direct stream reader tests |
| deep structure and image limits | parser matrix |
| retry re-upload and domain errors | router + retry tests |
| absolute deadline | Adobe + retry tests |
| disk readback/accounting/cleanup | router temp-path tests |
| pro/flash/canned usage identity | usage tests |
| one-event SSE | route and real smoke |
| dynamic logging/no base64 | full-app logging tests + rg review |
| sub2api 128 MiB/timeout deployment | README + real-chain smoke |

- [ ] **Step 7: Documentation checkpoint**

```bash
git diff --check
git diff -- README.md
```

Only after the current README user edits are isolated:

```bash
git add README.md
git diff --cached --check
git commit -m "docs: document Gemini native deployment requirements"
```

## Completion Criteria

- All automated tests pass in the current environment and under dependencies installed from `requirements-test.txt`.
- Docker image built from pinned production requirements imports app successfully.
- Existing OpenAI paths retain their exception mapping, network timeout and request logging behavior.
- All Gemini error responses have Google structure; stream failures are non-SSE.
- No request path can allocate an unbounded Gemini request body before the 48 MiB check.
- No Gemini base64 request or response data appears in request/error logs.
- The real sub2api non-stream large-response smoke passes with at least 8 MiB encoded image data.
- The deployed sub2api settings satisfy the 128 MiB response cap and deadline + 60 seconds header timeout.
- Final diff contains no unrelated rewrite of pre-existing user changes and no sub2api source modification.
