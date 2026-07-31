# leonardo_generation (OpenAI-shaped orchestration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 OpenAI 风格的图像请求（prompt / size|aspect_ratio / n / 已解析的 model_id）编排成 `LeonardoClient` 调用，返回 OpenAI 风格响应 `{created, data:[{url}], provider}`。

**Architecture:** 单模块 `core/leonardo_generation.py`，纯编排层，站在已测好的 `core.leonardo_client.LeonardoClient` 之上。两步：请求归一化（size/aspect→aspect 字符串、n 夹取）→ `generate_images(...)` 用注入的 client 走 create→wait→整形。所有时间/网络通过注入接缝，单测零网络（注入 fake client + fake now）。对标 adobe2api 现有 `core/image_generation.py` 的定位。

**Tech Stack:** Python 3.10、`pytest`。仅 import `core.leonardo_client`，**不引入新依赖**。

## Global Constraints

- **Python 3.10**；无新依赖；只 import 标准库 + `core.leonardo_client`。
- **Bearer/token 只作入参透传**，不打印、不落盘。
- **aspect 字符串必须是 leonardo_client 支持的四种之一**：`16:9 / 9:16 / 1:1 / 4:3`；无法识别一律回退 `1:1`。
- **n 夹取到 [1,4]**（Leonardo quantity 上限；与 leonardo_client `build_generate_payload` 的夹取一致）。
- **model_id 由调用方解析后传入**（本层不做别名→UUID 映射，避免臆造模型 ID）；`model_id` 为空时抛 `LeonardoError`。
- **响应形状**：成功 `{"created": int, "data": [{"url": <str>}...], "provider": {"generation_id": <str>, "aspect_ratio": <str>, "model_id": <str>}}`；生成失败/超时抛 `LeonardoError`。
- **不做**：account 池选号、cookie→bearer 获取、路由层、图片上传/参考图——均属后续（依赖 spike 定型）。

---

### Task 1: 请求归一化（纯函数）

**Files:**
- Create: `core/leonardo_generation.py`
- Test: `tests/test_leonardo_generation.py`

**Interfaces:**
- Produces: `to_aspect(size: Optional[str] = None, aspect_ratio: Optional[str] = None) -> str`（`aspect_ratio` 优先；否则按 `size` 映射；都无/不识别→`"1:1"`）；`clamp_quantity(n) -> int`（非法/越界夹到 [1,4]，默认 1）。供 Task 2 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_generation.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_generation.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'core.leonardo_generation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_generation.py
from typing import Any, Dict, List, Optional

_SUPPORTED_ASPECTS = {"16:9", "9:16", "1:1", "4:3"}
_SIZE_TO_ASPECT = {
    "1024x1024": "1:1", "512x512": "1:1", "256x256": "1:1",
    "1792x1024": "16:9", "1536x1024": "16:9",
    "1024x1792": "9:16", "1024x1536": "9:16",
    "2048x1536": "4:3",
}


def to_aspect(size: Optional[str] = None, aspect_ratio: Optional[str] = None) -> str:
    ratio = (aspect_ratio or "").strip()
    if ratio in _SUPPORTED_ASPECTS:
        return ratio
    mapped = _SIZE_TO_ASPECT.get((size or "").strip().lower())
    return mapped or "1:1"


def clamp_quantity(n) -> int:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return 1
    return max(1, min(4, value))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_generation.py -v`
Expected: PASS（5 用例）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_generation.py tests/test_leonardo_generation.py
git commit -m "feat(leonardo-gen): request normalization (size/aspect, quantity clamp)"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 2: `generate_images` 编排

**Files:**
- Modify: `core/leonardo_generation.py`
- Test: `tests/test_leonardo_generation.py`

**Interfaces:**
- Consumes: `to_aspect`、`clamp_quantity`（Task 1）；`core.leonardo_client.LeonardoError`。
- Produces: `generate_images(client, token, *, prompt, model_id, size=None, aspect_ratio=None, n=1, timeout=300, poll_interval=4, now=time.time) -> Dict[str, Any]`。用 `client.create_generation(...)` + `client.wait_for_completion(...)` 出图，整形为 OpenAI 响应；`model_id` 为空或生成失败抛 `LeonardoError`。`client` 是 duck-typed（真实为 `LeonardoClient`，测试注入 fake）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leonardo_generation.py （追加）
from core.leonardo_generation import generate_images
from core.leonardo_client import LeonardoError


class _FakeClient:
    def __init__(self, *, gen_id="gen-1", result=None, credits=8500):
        self._gen_id = gen_id
        self._result = result or {"success": True, "images": ["https://cdn/a.jpg"]}
        self._credits = credits
        self.calls = {}

    def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1, init_image_ids=None):
        self.calls["create"] = dict(token=token, prompt=prompt, model_id=model_id,
                                    aspect_ratio=aspect_ratio, quantity=quantity)
        return self._gen_id

    def wait_for_completion(self, token, gen_id, **kwargs):
        self.calls["wait"] = dict(token=token, gen_id=gen_id, kwargs=kwargs)
        return self._result


def test_generate_images_happy_path():
    client = _FakeClient(gen_id="gen-9", result={"success": True, "images": ["https://cdn/x.jpg", "https://cdn/y.jpg"]})
    out = generate_images(client, "TOK", prompt="a cat", model_id="M1",
                          size="1792x1024", n=2, now=lambda: 1700000000)
    assert out["created"] == 1700000000
    assert out["data"] == [{"url": "https://cdn/x.jpg"}, {"url": "https://cdn/y.jpg"}]
    assert out["provider"] == {"generation_id": "gen-9", "aspect_ratio": "16:9", "model_id": "M1"}
    # 归一化正确传给 client
    assert client.calls["create"]["aspect_ratio"] == "16:9"
    assert client.calls["create"]["quantity"] == 2


def test_generate_images_requires_model_id():
    with pytest.raises(LeonardoError):
        generate_images(_FakeClient(), "TOK", prompt="x", model_id="")


def test_generate_images_raises_on_failed_result():
    client = _FakeClient(result={"success": False, "error": "generation failed"})
    with pytest.raises(LeonardoError):
        generate_images(client, "TOK", prompt="x", model_id="M1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_leonardo_generation.py -k generate_images -v`
Expected: FAIL —— `ImportError: cannot import name 'generate_images'`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/leonardo_generation.py （顶部 import 追加；文件内追加函数）
import time

from core.leonardo_client import LeonardoError


def generate_images(
    client,
    token: str,
    *,
    prompt: str,
    model_id: str,
    size: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    n: int = 1,
    timeout: int = 300,
    poll_interval: int = 4,
    now=time.time,
) -> Dict[str, Any]:
    if not (model_id or "").strip():
        raise LeonardoError("model_id is required")

    aspect = to_aspect(size=size, aspect_ratio=aspect_ratio)
    quantity = clamp_quantity(n)

    gen_id = client.create_generation(
        token, prompt, model_id, aspect, quantity=quantity
    )
    result = client.wait_for_completion(
        token, gen_id, timeout=timeout, poll_interval=poll_interval
    )
    if not result.get("success"):
        raise LeonardoError(str(result.get("error") or "generation failed"))

    urls: List[str] = result.get("images") or []
    return {
        "created": int(now()),
        "data": [{"url": url} for url in urls],
        "provider": {
            "generation_id": gen_id,
            "aspect_ratio": aspect,
            "model_id": model_id,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_leonardo_generation.py -v`
Expected: PASS（5 + 3 = 8）。

- [ ] **Step 5: Commit**

```bash
git add core/leonardo_generation.py tests/test_leonardo_generation.py
git commit -m "feat(leonardo-gen): generate_images orchestration over LeonardoClient"
```

Commit trailer (exact last line):
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## Self-Review

- **Spec coverage**：对应 leonardo-type spec §7.1 的编排层（OpenAI 兼容出图，站在 `leonardo_client` 之上）。**明确不覆盖**：account 池选号、cookie→bearer、路由、图片上传——Global Constraints 已声明，属后续（依赖 spike）。
- **Placeholder scan**：无 TODO/TBD；每步含可运行代码。
- **Type consistency**：`to_aspect`/`clamp_quantity`（T1）被 `generate_images`（T2）按同名调用；`LeonardoError` 来自 `core.leonardo_client`（已存在）；`client` duck-typed，方法名 `create_generation`/`wait_for_completion` 与 `LeonardoClient`（已实现）一致。
- **给实现者**：Task 2 向顶部 import 追加 `import time` 与 `from core.leonardo_client import LeonardoError`——与 Task 1 的 `from typing import ...` 合并/并列，勿重复行。
