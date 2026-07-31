# leonardo_client (GraphQL protocol layer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给定一个 Leonardo 的 Bearer JWT，通过 `api.leonardo.ai/v1/graphql` 完成文生图：查额度、提交生成、轮询、取图 URL。

**Architecture:** 单模块 `core/leonardo_client.py`，自底向上：JWT 工具 → 额度解析 → Generate 载荷/解析 → 轮询/取图解析 → `LeonardoClient` 类（把上述纯函数用一个可注入的 `gql(token, payload)->dict` 调用器串起来，含 `wait_for_completion` 轮询循环）。所有网络与时间通过注入接缝暴露，单元测试零网络。移植自 `../leoapi/app/leonardo_client.py`，额度字段按 `../create-canva-new` 对 Canva-SSO 账号的处理扩展。

**Tech Stack:** Python 3.10、`requests`（项目已依赖）、`pytest`。**不引入新依赖。**

## Global Constraints

- **Python 3.10**；仅标准库 + `requests`，**无新依赖**。
- **GraphQL 端点固定**：`https://api.leonardo.ai/v1/graphql`。
- **Generate 请求包裹层 `model` 恒为 `"nano-banana-2"`**（与 leoapi 一致）；真正的模型走 `parameters.modelId`（动态入参）。
- **额度求和字段**：`subscriptionTokens + paidTokens + rolloverTokens + apiCredit + streamTokens`——Canva-SSO 账号用 `apiCredit`（约 8500），必须计入。
- **比例→分辨率固定**：`16:9→2752x1536`，`9:16→1536x2752`，`1:1→1536x1536`，`4:3→2048x1536`；未知比例回退 `1:1`。
- **不做**：真实 HTTP `_gql` 的网络单测（薄封装，属集成）、图片上传（S3，参考图暂不做）、cookie→JWT 的 better-auth 桥接（待 spike 定型）。这些不在本计划。
- **本层不碰凭据落盘/日志**：Bearer 只作入参透传，不打印。

---

### Task 1: JWT 工具 + 异常基类

**Files:**
- Create: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- Produces: `LeonardoError(Exception)`；`decode_jwt_payload(token)->dict`；`token_exp(token)->int`；`is_fresh_token(token, min_ttl_seconds=120, *, now=time.time)->bool`；`is_likely_leonardo_token(token)->bool`。供后续任务使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_client.py
import base64
import json
import time

from core.leonardo_client import (
    LeonardoError,
    decode_jwt_payload,
    token_exp,
    is_fresh_token,
    is_likely_leonardo_token,
)


def _jwt(payload: dict) -> str:
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"{seg({'alg':'none'})}.{seg(payload)}.sig"


def test_decode_and_exp():
    tok = _jwt({"exp": 1900000000, "iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})
    assert decode_jwt_payload(tok)["exp"] == 1900000000
    assert token_exp(tok) == 1900000000


def test_decode_non_jwt_returns_empty():
    assert decode_jwt_payload("not-a-jwt") == {}
    assert token_exp("nope") == 0


def test_is_fresh_uses_injected_now():
    tok = _jwt({"exp": 1000})
    assert is_fresh_token(tok, now=lambda: 800) is True      # 1000 > 800+... no: 800+120=920 < 1000
    assert is_fresh_token(tok, now=lambda: 950) is False     # 950+120=1070 > 1000 -> not fresh
    assert is_fresh_token("no-dots", now=lambda: 0) is False


def test_is_fresh_true_when_no_exp():
    assert is_fresh_token(_jwt({"sub": "x"}), now=lambda: 0) is True


def test_is_likely_leonardo_token_cognito_signals():
    assert is_likely_leonardo_token(_jwt({"iss": "https://cognito-idp.us-east-1.amazonaws.com/x"})) is True
    assert is_likely_leonardo_token(_jwt({"token_use": "access"})) is True
    assert is_likely_leonardo_token(_jwt({"cognito:username": "u"})) is True
    assert is_likely_leonardo_token(_jwt({"foo": "bar"})) is False


def test_leonardo_error_is_exception():
    assert issubclass(LeonardoError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'core.leonardo_client'`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_client.py
import base64
import json
import time
from typing import Any, Dict, List, Optional


class LeonardoError(Exception):
    """leonardo_client 统一异常。"""


def _b64url_json(segment: str) -> Dict[str, Any]:
    try:
        pad = "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))
    except Exception:
        return {}


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    data = _b64url_json(parts[1])
    return data if isinstance(data, dict) else {}


def token_exp(token: str) -> int:
    exp = decode_jwt_payload(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def is_fresh_token(token: str, min_ttl_seconds: int = 120, *, now=time.time) -> bool:
    if (token or "").count(".") != 2:
        return False
    exp = token_exp(token)
    if not exp:
        return True
    return exp > int(now()) + max(30, int(min_ttl_seconds))


def is_likely_leonardo_token(token: str) -> bool:
    payload = decode_jwt_payload(token)
    if not payload:
        return False
    if "cognito-idp" in str(payload.get("iss", "")).lower():
        return True
    if str(payload.get("token_use", "")).lower() in {"id", "access"}:
        return True
    if "cognito:username" in payload:
        return True
    aud = payload.get("aud")
    return isinstance(aud, str) and aud.startswith("https://cognito-idp")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: PASS（6 用例）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "feat(leonardo): JWT helpers and error base"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 2: 额度解析 + 查询串

**Files:**
- Modify: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- Consumes: 无（纯字典处理）。
- Produces: `TOKEN_BALANCE_QUERY: dict`（GraphQL 查询体常量）；`sum_credits(details: dict) -> int`；`parse_token_balance(resp: dict) -> Optional[int]`。供 Task 5 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_client.py （追加）
from core.leonardo_client import TOKEN_BALANCE_QUERY, sum_credits, parse_token_balance


def test_sum_credits_includes_apicredit_and_stream():
    details = {"subscriptionTokens": 100, "paidTokens": 5, "rolloverTokens": 0,
               "apiCredit": 8500, "streamTokens": 3}
    assert sum_credits(details) == 8608


def test_sum_credits_ignores_missing_and_nonnumeric():
    assert sum_credits({"subscriptionTokens": 10, "paidTokens": None, "apiCredit": "x"}) == 10


def test_parse_token_balance_from_response():
    resp = {"data": {"user_details": [{"subscriptionTokens": 850, "apiCredit": 0}]}}
    assert parse_token_balance(resp) == 850


def test_parse_token_balance_empty_returns_none():
    assert parse_token_balance({"data": {"user_details": []}}) is None
    assert parse_token_balance({}) is None


def test_token_balance_query_shape():
    assert TOKEN_BALANCE_QUERY["operationName"] == "GetTokenBalance"
    assert "user_details" in TOKEN_BALANCE_QUERY["query"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_client.py -k "credits or token_balance" -v`
Expected: FAIL —— `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_client.py （追加）
_CREDIT_FIELDS = ("subscriptionTokens", "paidTokens", "rolloverTokens", "apiCredit", "streamTokens")

TOKEN_BALANCE_QUERY = {
    "operationName": "GetTokenBalance",
    "variables": {},
    "query": (
        "query GetTokenBalance { user_details { "
        "subscriptionTokens paidTokens rolloverTokens apiCredit streamTokens __typename } }"
    ),
}


def sum_credits(details: Dict[str, Any]) -> int:
    total = 0
    for key in _CREDIT_FIELDS:
        value = (details or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def parse_token_balance(resp: Dict[str, Any]) -> Optional[int]:
    rows = ((resp or {}).get("data") or {}).get("user_details") or []
    if not rows:
        return None
    return sum_credits(rows[0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: PASS（6 + 5 = 11）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "feat(leonardo): credit balance parsing (incl apiCredit)"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 3: Generate 载荷 + 比例映射 + 解析 generationId

**Files:**
- Modify: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- Consumes: `LeonardoError`（Task 1）。
- Produces: `ASPECT_TO_SIZE: dict`；`aspect_to_size(aspect: str) -> Tuple[int,int]`；`build_generate_payload(prompt, model_id, width, height, quantity=1, init_image_ids=None) -> dict`；`parse_generation_id(resp: dict) -> str`（失败抛 `LeonardoError`）。供 Task 5 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_client.py （追加）
import pytest
from core.leonardo_client import (
    ASPECT_TO_SIZE, aspect_to_size, build_generate_payload, parse_generation_id, LeonardoError,
)


def test_aspect_to_size_known_and_default():
    assert aspect_to_size("16:9") == (2752, 1536)
    assert aspect_to_size("9:16") == (1536, 2752)
    assert aspect_to_size("weird") == (1536, 1536)  # 回退 1:1


def test_build_generate_payload_core_fields():
    p = build_generate_payload("  a cat  ", "MODEL-123", 1536, 1536, quantity=9)
    assert p["operationName"] == "Generate"
    req = p["variables"]["request"]
    assert req["model"] == "nano-banana-2"           # 包裹层恒定
    params = req["parameters"]
    assert params["modelId"] == "MODEL-123"          # 动态模型
    assert params["prompt"] == "a cat"               # trim
    assert params["quantity"] == 4                   # 9 被夹到 [1,4]
    assert params["dimensions"] == "1536x1536"
    assert "guidances" not in params                 # 无参考图


def test_build_generate_payload_with_reference_images():
    p = build_generate_payload("x", "M", 1536, 1536, init_image_ids=["img-1"])
    ref = p["variables"]["request"]["parameters"]["guidances"]["image_reference"]
    assert ref == [{"image": {"id": "img-1", "type": "UPLOADED"}, "strength": "MID"}]


def test_parse_generation_id_success():
    assert parse_generation_id({"data": {"generate": {"generationId": "gen-9"}}}) == "gen-9"


def test_parse_generation_id_raises_on_error():
    with pytest.raises(LeonardoError):
        parse_generation_id({"errors": [{"message": "quota exhausted"}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_client.py -k "aspect or generate or generation_id" -v`
Expected: FAIL —— `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_client.py （追加；顶部 import 补上 Tuple）
from typing import Tuple  # 合并进已有 typing import，勿重复

ASPECT_TO_SIZE = {
    "16:9": (2752, 1536),
    "9:16": (1536, 2752),
    "1:1": (1536, 1536),
    "4:3": (2048, 1536),
}
_STYLE_IDS = ["111dc692-d470-4eec-b791-3475abac4c46"]
_GENERATE_QUERY = (
    "mutation Generate($request: CreateGenerationRequest!) { "
    "generate(request: $request) { apiCreditCost generationId __typename } }"
)


def aspect_to_size(aspect: str) -> Tuple[int, int]:
    return ASPECT_TO_SIZE.get(aspect, ASPECT_TO_SIZE["1:1"])


def build_generate_payload(
    prompt: str,
    model_id: str,
    width: int,
    height: int,
    quantity: int = 1,
    init_image_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "width": width,
        "height": height,
        "prompt": (prompt or "").strip(),
        "quantity": max(1, min(4, int(quantity))),
        "style_ids": list(_STYLE_IDS),
        "prompt_enhance": "ON",
        "dimensions": f"{width}x{height}",
        "modelId": model_id,
        "negative_prompt": "",
        "guidance_scale": 7.0,
        "num_inference_steps": 30,
    }
    if init_image_ids:
        params["guidances"] = {
            "image_reference": [
                {"image": {"id": image_id, "type": "UPLOADED"}, "strength": "MID"}
                for image_id in init_image_ids
            ]
        }
    return {
        "operationName": "Generate",
        "variables": {"request": {"model": "nano-banana-2", "parameters": params, "public": True}},
        "query": _GENERATE_QUERY,
    }


def parse_generation_id(resp: Dict[str, Any]) -> str:
    gen_id = (((resp or {}).get("data") or {}).get("generate") or {}).get("generationId")
    if gen_id:
        return gen_id
    errors = [e.get("message", "") for e in (resp or {}).get("errors", []) if isinstance(e, dict)]
    raise LeonardoError(", ".join([m for m in errors if m]) or "Generate failed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: PASS（11 + 5 = 16）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "feat(leonardo): Generate payload, aspect map, generationId parse"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 4: 轮询状态 + 取图 URL 解析

**Files:**
- Modify: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- Produces: `build_status_query(gen_id)->dict`；`build_feed_query(gen_id)->dict`；`parse_generation_status(resp)->str`（无数据回 `"PENDING"`）；`parse_image_urls(resp)->List[str]`。供 Task 5 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_client.py （追加）
from core.leonardo_client import (
    build_status_query, build_feed_query, parse_generation_status, parse_image_urls,
)


def test_status_and_feed_query_shape():
    assert build_status_query("gen-1")["operationName"] == "GetAIGenerationFeedStatuses"
    assert build_status_query("gen-1")["variables"]["where"]["id"]["_eq"] == "gen-1"
    assert build_feed_query("gen-1")["operationName"] == "GetAIGenerationFeed"


def test_parse_status():
    resp = {"data": {"generations": [{"id": "g", "status": "COMPLETE"}]}}
    assert parse_generation_status(resp) == "COMPLETE"


def test_parse_status_pending_when_empty():
    assert parse_generation_status({"data": {"generations": []}}) == "PENDING"
    assert parse_generation_status({}) == "PENDING"


def test_parse_image_urls():
    resp = {"data": {"generations": [{"generated_images": [
        {"url": "https://cdn/x1.jpg"}, {"url": None}, {"url": "https://cdn/x2.jpg"}]}]}}
    assert parse_image_urls(resp) == ["https://cdn/x1.jpg", "https://cdn/x2.jpg"]


def test_parse_image_urls_empty():
    assert parse_image_urls({"data": {"generations": []}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_client.py -k "status or feed or image_urls" -v`
Expected: FAIL —— `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_client.py （追加）
def build_status_query(gen_id: str) -> Dict[str, Any]:
    return {
        "operationName": "GetAIGenerationFeedStatuses",
        "variables": {"where": {"id": {"_eq": gen_id}}},
        "query": (
            "query GetAIGenerationFeedStatuses($where: generations_bool_exp = {}) { "
            "generations(where: $where) { id status __typename } }"
        ),
    }


def build_feed_query(gen_id: str) -> Dict[str, Any]:
    return {
        "operationName": "GetAIGenerationFeed",
        "variables": {"where": {"id": {"_eq": gen_id}}, "limit": 1},
        "query": (
            "query GetAIGenerationFeed($where: generations_bool_exp = {}, $limit: Int) { "
            "generations(where: $where, limit: $limit) { "
            "generated_images(order_by: [{url: desc}]) { url id __typename } __typename } }"
        ),
    }


def parse_generation_status(resp: Dict[str, Any]) -> str:
    gens = ((resp or {}).get("data") or {}).get("generations") or []
    return gens[0].get("status", "PENDING") if gens else "PENDING"


def parse_image_urls(resp: Dict[str, Any]) -> List[str]:
    gens = ((resp or {}).get("data") or {}).get("generations") or []
    if not gens:
        return []
    return [img.get("url") for img in gens[0].get("generated_images", []) if img.get("url")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: PASS（16 + 5 = 21）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "feat(leonardo): status/feed queries and response parsing"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 5: `LeonardoClient` 类（串联 + 轮询完成）

**Files:**
- Modify: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- Consumes: 前四个任务的全部函数/常量。
- Produces: `GRAPHQL_URL: str`；`LeonardoClient` 类，方法：`get_credits(token)`、`create_generation(token, prompt, model_id, aspect_ratio, quantity=1, init_image_ids=None)->gen_id`、`poll_status(token, gen_id)`、`get_image_urls(token, gen_id)`、`wait_for_completion(token, gen_id, *, timeout=300, poll_interval=4, sleep=time.sleep, now=time.time)->dict`。构造器接受 `gql=None` 注入接缝：`LeonardoClient(gql=fake)`；未注入时用内部 `_http_gql`（真实 requests 调用，本计划不单测）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_client.py （追加）
from core.leonardo_client import LeonardoClient, GRAPHQL_URL


def test_create_generation_uses_gql_and_returns_id():
    seen = {}

    def fake_gql(token, payload):
        seen["op"] = payload["operationName"]
        return {"data": {"generate": {"generationId": "gen-42"}}}

    client = LeonardoClient(gql=fake_gql)
    gid = client.create_generation("TOK", "a cat", "M1", "1:1")
    assert gid == "gen-42"
    assert seen["op"] == "Generate"


def test_get_credits():
    client = LeonardoClient(gql=lambda t, p: {"data": {"user_details": [{"apiCredit": 8500}]}})
    assert client.get_credits("TOK") == 8500


def test_wait_for_completion_polls_then_succeeds():
    seq = iter(["PENDING", "COMPLETED"])

    def fake_gql(token, payload):
        op = payload["operationName"]
        if op == "GetAIGenerationFeedStatuses":
            return {"data": {"generations": [{"id": "g", "status": next(seq)}]}}
        if op == "GetAIGenerationFeed":
            return {"data": {"generations": [{"generated_images": [{"url": "https://cdn/final.jpg"}]}]}}
        return {}

    client = LeonardoClient(gql=fake_gql)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    result = client.wait_for_completion("TOK", "g", timeout=60, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: next(ticks))
    assert result == {"success": True, "images": ["https://cdn/final.jpg"]}


def test_wait_for_completion_failed_status():
    client = LeonardoClient(gql=lambda t, p: {"data": {"generations": [{"status": "FAILED"}]}})
    result = client.wait_for_completion("TOK", "g", timeout=60, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: 0.0)
    assert result["success"] is False


def test_wait_for_completion_timeout():
    client = LeonardoClient(gql=lambda t, p: {"data": {"generations": [{"status": "PENDING"}]}})
    ticks = iter([0.0, 100.0, 200.0])
    result = client.wait_for_completion("TOK", "g", timeout=30, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: next(ticks))
    assert result["success"] is False


def test_graphql_url_constant():
    assert GRAPHQL_URL == "https://api.leonardo.ai/v1/graphql"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_client.py -k "LeonardoClient or wait_for or get_credits or create_generation or graphql_url" -v`
Expected: FAIL —— `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_client.py （追加；顶部 import 补 requests）
import requests  # 合并进已有 import，勿重复

GRAPHQL_URL = "https://api.leonardo.ai/v1/graphql"
_BASE_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://app.leonardo.ai",
    "referer": "https://app.leonardo.ai/",
    "x-leo-schema-version": "latest",
}


class LeonardoClient:
    def __init__(self, *, gql=None):
        self._gql_fn = gql  # 可注入；None 时用真实 HTTP

    def _http_gql(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = dict(_BASE_HEADERS)
        headers["authorization"] = f"Bearer {token}"
        resp = requests.post(GRAPHQL_URL, headers=headers, json=payload, timeout=60)
        if not resp.ok:
            raise LeonardoError(f"graphql HTTP {resp.status_code}")
        return resp.json()

    def _call(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        fn = self._gql_fn or self._http_gql
        return fn(token, payload)

    def get_credits(self, token: str) -> Optional[int]:
        return parse_token_balance(self._call(token, TOKEN_BALANCE_QUERY))

    def create_generation(
        self, token, prompt, model_id, aspect_ratio, quantity=1, init_image_ids=None
    ) -> str:
        width, height = aspect_to_size(aspect_ratio)
        payload = build_generate_payload(prompt, model_id, width, height, quantity, init_image_ids)
        return parse_generation_id(self._call(token, payload))

    def poll_status(self, token: str, gen_id: str) -> str:
        return parse_generation_status(self._call(token, build_status_query(gen_id)))

    def get_image_urls(self, token: str, gen_id: str) -> List[str]:
        return parse_image_urls(self._call(token, build_feed_query(gen_id)))

    def wait_for_completion(
        self, token, gen_id, *, timeout=300, poll_interval=4, sleep=time.sleep, now=time.time
    ) -> Dict[str, Any]:
        deadline = now() + timeout
        while now() < deadline:
            status = self.poll_status(token, gen_id)
            if status == "COMPLETED":
                return {"success": True, "images": self.get_image_urls(token, gen_id)}
            if status in ("FAILED", "ERROR"):
                return {"success": False, "error": "generation failed"}
            sleep(poll_interval)
        return {"success": False, "error": "generation timeout"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: PASS（21 + 6 = 27）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "feat(leonardo): LeonardoClient with injectable gql and wait_for_completion"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## Self-Review

- **Spec coverage**：对应 leonardo-type spec §7.1 的 `core/leonardo_client.py`（协议层）。覆盖 §4.1 的 Generate/查额度/轮询/取图；额度按 Global Constraints 含 `apiCredit`。**明确不覆盖**：真实 HTTP `_gql`（薄封装，注入接缝已留）、图片上传、cookie→JWT 桥接——文首 Global Constraints 已声明，属后续计划。
- **Placeholder scan**：无 TODO/TBD；每步均含可运行代码。
- **Type consistency**：`aspect_to_size`/`build_generate_payload`/`parse_generation_id`（T3）、`parse_token_balance`（T2）、`parse_generation_status`/`parse_image_urls`（T4）被 `LeonardoClient`（T5）按同名调用；`LeonardoError`（T1）在 T3/T5 复用；`gql` 注入接缝签名 `(token, payload)->dict` 全程一致。
- **注意事项给实现者**：Task 3/5 会向 `core/leonardo_client.py` 顶部 import 追加 `Tuple`、`requests`——必须合并进已有 `from typing import ...` / import 区，不得重复行。
