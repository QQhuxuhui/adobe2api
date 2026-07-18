# Adobe2api OpenAI Responses Image Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native Adobe-backed `POST /v1/responses` image endpoint that returns standard `image_generation_call` output for non-streaming and streaming clients.

**Architecture:** Pure helpers in `api/openai_responses.py` parse the supported Responses request shapes and serialize standard JSON/SSE output. A shared executor in `core/image_generation.py` centralizes Adobe generation and artifact persistence; existing image routes and the new Responses route call it through the current dependency-injected generation router.

**Tech Stack:** Python 3, FastAPI/Starlette, Pillow 10.4, pytest, existing Adobe client/token retry/request logging infrastructure.

## Global Constraints

- Implement the approved specification in `docs/superpowers/specs/2026-07-18-openai-responses-image-design.md`.
- Preserve existing `/v1/images/generations`, `/v1/images/edits`, `/v1/chat/completions`, and `/api/v1/generate` behavior.
- Do not modify sub2api, new-api, or remote account settings during local implementation.
- Do not persist request base64 input, response base64 output, or complete request bodies in logs.
- `/v1/responses/compact`, WebSocket mode, Files API, masks, and partial preview images remain unsupported.
- Use ASCII in new source files except for existing user-facing Chinese text in surrounding files.
- The worktree already contains unrelated and overlapping user changes; preserve them and patch against the current file contents.
- The current `.git` metadata is read-only. Attempt each scoped commit only if `.git` becomes writable; otherwise record the intended commit boundary and continue without changing permissions.

---

## File Map

- Create `api/openai_responses.py`: request normalization, validation, result encoding, Responses JSON construction, and SSE serialization.
- Create `core/image_generation.py`: shared Adobe image execution and generated artifact persistence.
- Modify `api/routes/generation.py`: adopt the shared executor and add `POST /v1/responses`.
- Modify `app.py`: request operation and prompt logging support for Responses input.
- Create `tests/test_openai_responses_protocol.py`: pure protocol contract tests.
- Create `tests/test_image_generation.py`: shared executor unit tests.
- Create `tests/test_openai_responses.py`: route, streaming, input-image, error, retry, preview, and credit-context tests.
- Create `tests/test_openai_responses_logging.py`: request operation and metadata extraction regression tests.
- Modify `README.md`: document endpoint shapes and current limitations.

---

### Task 1: Responses Request and Output Protocol Helpers

**Files:**
- Create: `api/openai_responses.py`
- Create: `tests/test_openai_responses_protocol.py`

**Interfaces:**
- Produces: `ResponsesRequestError`, `ResponsesImageRequest`, `parse_responses_image_request(data, image_model_ids)`, `encode_image_result(image_bytes, output_format, output_compression)`, `build_responses_image_response(...)`, and `iter_responses_image_sse(response)`.
- Consumes: only standard-library types plus Pillow for deterministic in-memory format conversion.

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_openai_responses_protocol.py` with the request-shape tests below. Use a fixed model set so the protocol module has no catalog dependency:

```python
import base64
import io
import json

import pytest
from PIL import Image

from api.openai_responses import (
    ResponsesRequestError,
    build_responses_image_response,
    encode_image_result,
    iter_responses_image_sse,
    parse_responses_image_request,
)


IMAGE_MODELS = {"gpt-image-1", "gpt-image-2", "firefly-gpt-image"}


def test_parse_image_only_model_with_string_input():
    parsed = parse_responses_image_request(
        {
            "model": "gpt-image-2",
            "input": "draw a blue square",
            "size": "1024x1024",
            "quality": "low",
        },
        IMAGE_MODELS,
    )
    assert parsed.inbound_model == "gpt-image-2"
    assert parsed.image_model == "gpt-image-2"
    assert parsed.prompt == "draw a blue square"
    assert parsed.size == "1024x1024"
    assert parsed.quality == "low"
    assert parsed.stream is False


def test_parse_official_image_tool_defaults_backend_model_and_tool_fields_win():
    parsed = parse_responses_image_request(
        {
            "model": "gpt-5.4-mini",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "edit this"},
                        {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
                    ],
                }
            ],
            "size": "1024x1024",
            "quality": "low",
            "tools": [
                {
                    "type": "image_generation",
                    "size": "1536x1024",
                    "quality": "high",
                    "output_format": "webp",
                    "output_compression": 73,
                    "action": "edit",
                }
            ],
            "tool_choice": "required",
            "stream": True,
        },
        IMAGE_MODELS,
    )
    assert parsed.inbound_model == "gpt-5.4-mini"
    assert parsed.image_model == "gpt-image-2"
    assert parsed.prompt == "edit this"
    assert parsed.input_image_urls == ("data:image/png;base64,YQ==",)
    assert parsed.size == "1536x1024"
    assert parsed.quality == "high"
    assert parsed.output_format == "webp"
    assert parsed.output_compression == 73
    assert parsed.action == "edit"
    assert parsed.stream is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"model": "gpt-image-2", "input": ""}, "input is required"),
        ({"model": "gpt-5.4-mini", "input": "draw"}, "image_generation tool is required"),
        ({"model": "gpt-image-2", "input": "draw", "tool_choice": "none"}, "tool_choice"),
        ({"model": "gpt-image-2", "input": "draw", "background": "transparent"}, "transparent"),
        ({"model": "gpt-image-2", "input": "draw", "partial_images": 1}, "partial_images"),
        (
            {
                "model": "gpt-image-2",
                "input": [{"role": "user", "content": [{"type": "input_image", "file_id": "file_1"}]}],
                "prompt": "edit",
            },
            "file_id",
        ),
        (
            {
                "model": "gpt-image-2",
                "input": "draw",
                "tools": [{"type": "image_generation", "input_fidelity": "high"}],
            },
            "input_fidelity",
        ),
    ],
)
def test_parse_rejects_unsupported_requests(payload, message):
    with pytest.raises(ResponsesRequestError, match=message):
        parse_responses_image_request(payload, IMAGE_MODELS)
```

- [ ] **Step 2: Run parser tests and verify RED**

Run:

```bash
pytest -q tests/test_openai_responses_protocol.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'api.openai_responses'`.

- [ ] **Step 3: Implement request normalization and validation**

Create `api/openai_responses.py` with these exact public types and rules:

```python
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from PIL import Image


class ResponsesRequestError(ValueError):
    def __init__(self, message: str, param: str | None = None):
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class ResponsesImageRequest:
    inbound_model: str
    image_model: str
    prompt: str
    input_image_urls: tuple[str, ...]
    stream: bool
    size: str | None
    quality: str | None
    output_format: str
    output_compression: int | None
    background: str
    moderation: str | None
    action: str
    partial_images: int

    def image_loader_messages(self) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}}
                    for url in self.input_image_urls
                ],
            }
        ]


_TOOL_FIELDS = {
    "type", "model", "size", "quality", "output_format",
    "output_compression", "background", "moderation", "action",
    "partial_images", "input_fidelity", "input_image_mask",
}
_PARAM_FIELDS = _TOOL_FIELDS - {"type", "model"}


def _last_user_content(input_value: Any) -> tuple[str, tuple[str, ...]]:
    if isinstance(input_value, str):
        return input_value.strip(), ()
    if not isinstance(input_value, list):
        return "", ()
    for item in reversed(input_value):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content.strip(), ()
        texts: list[str] = []
        urls: list[str] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                part_type = str(part.get("type") or "")
                if part_type in {"input_text", "text"}:
                    text = str(part.get("text") or "").strip()
                    if text:
                        texts.append(text)
                elif part_type == "input_image":
                    if part.get("file_id"):
                        raise ResponsesRequestError("file_id input images are not supported", "input")
                    url = str(part.get("image_url") or "").strip()
                    if not url:
                        raise ResponsesRequestError("input_image.image_url is required", "input")
                    urls.append(url)
        if len(urls) > 6:
            raise ResponsesRequestError("at most 6 input images are supported", "input")
        return "\n".join(texts).strip(), tuple(urls)
    return "", ()


def _image_tool(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    tools = data.get("tools")
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise ResponsesRequestError("tools must be an array", "tools")
    for tool in tools:
        if isinstance(tool, Mapping) and str(tool.get("type") or "") == "image_generation":
            unknown = set(tool) - _TOOL_FIELDS
            if unknown:
                field = sorted(unknown)[0]
                raise ResponsesRequestError(f"unsupported image_generation field: {field}", f"tools.{field}")
            return tool
    return None


def _validate_tool_choice(choice: Any) -> None:
    if choice is None:
        return
    if isinstance(choice, str):
        if choice in {"auto", "required"}:
            return
        if choice == "none":
            raise ResponsesRequestError("tool_choice must allow image_generation", "tool_choice")
        raise ResponsesRequestError("tool_choice must select image_generation", "tool_choice")
    if isinstance(choice, Mapping) and str(choice.get("type") or "") == "image_generation":
        return
    raise ResponsesRequestError("tool_choice must select image_generation", "tool_choice")


def parse_responses_image_request(
    data: Mapping[str, Any], image_model_ids: Iterable[str]
) -> ResponsesImageRequest:
    if not isinstance(data, Mapping):
        raise ResponsesRequestError("request body must be an object")
    inbound_model = str(data.get("model") or "").strip()
    if not inbound_model:
        raise ResponsesRequestError("model is required", "model")
    models = set(image_model_ids)
    tool = _image_tool(data)
    if tool and "input_image_mask" in tool:
        raise ResponsesRequestError("input_image_mask is not supported", "tools.input_image_mask")
    if tool and "input_fidelity" in tool:
        raise ResponsesRequestError("input_fidelity is not supported", "tools.input_fidelity")
    image_model = str((tool or {}).get("model") or "").strip()
    if not image_model and inbound_model in models:
        image_model = inbound_model
    if not image_model and tool is not None:
        image_model = "gpt-image-2"
    if not image_model:
        raise ResponsesRequestError("image_generation tool is required for this model", "tools")
    if image_model not in models:
        raise ResponsesRequestError(f"unsupported image model: {image_model}", "model")
    _validate_tool_choice(data.get("tool_choice"))
    prompt, input_urls = _last_user_content(data.get("input"))
    if not prompt:
        prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise ResponsesRequestError("input is required", "input")
    effective = {key: data.get(key) for key in _PARAM_FIELDS if key in data}
    if tool:
        effective.update({key: tool[key] for key in _PARAM_FIELDS if key in tool})
    output_format = str(effective.get("output_format") or "png").lower().strip()
    if output_format == "jpg":
        output_format = "jpeg"
    if output_format not in {"png", "jpeg", "webp"}:
        raise ResponsesRequestError("output_format must be png, jpeg, or webp", "output_format")
    compression = effective.get("output_compression")
    if compression is not None:
        if isinstance(compression, bool) or not isinstance(compression, int) or not 0 <= compression <= 100:
            raise ResponsesRequestError("output_compression must be an integer from 0 to 100", "output_compression")
    background = str(effective.get("background") or "auto").lower().strip()
    if background == "transparent":
        raise ResponsesRequestError("transparent backgrounds are not supported", "background")
    if background not in {"auto", "opaque"}:
        raise ResponsesRequestError("background must be auto or opaque", "background")
    partial_images = effective.get("partial_images", 0)
    if isinstance(partial_images, bool) or not isinstance(partial_images, int) or partial_images != 0:
        raise ResponsesRequestError("partial_images must be 0", "partial_images")
    action = str(effective.get("action") or "auto").lower().strip()
    if action not in {"auto", "generate", "edit"}:
        raise ResponsesRequestError("action must be auto, generate, or edit", "action")
    if action == "edit" and not input_urls:
        raise ResponsesRequestError("action=edit requires an input image", "action")
    return ResponsesImageRequest(
        inbound_model=inbound_model,
        image_model=image_model,
        prompt=prompt,
        input_image_urls=input_urls,
        stream=bool(data.get("stream", False)),
        size=str(effective["size"]).strip() if effective.get("size") is not None else None,
        quality=str(effective["quality"]).strip() if effective.get("quality") is not None else None,
        output_format=output_format,
        output_compression=compression,
        background=background,
        moderation=str(effective["moderation"]).strip() if effective.get("moderation") is not None else None,
        action=action,
        partial_images=partial_images,
    )
```

- [ ] **Step 4: Run parser tests and verify GREEN**

Run:

```bash
pytest -q tests/test_openai_responses_protocol.py -k parse
```

Expected: all parser tests pass.

- [ ] **Step 5: Add failing image encoder and response/SSE tests**

Append:

```python
def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp"])
def test_encode_image_result_returns_decodable_requested_format(output_format):
    result = encode_image_result(_png_bytes(), output_format, 80)
    with Image.open(io.BytesIO(base64.b64decode(result))) as decoded:
        assert decoded.format.lower() == output_format


def test_build_response_and_sse_use_image_generation_call():
    response = build_responses_image_response(
        response_id="resp_test",
        item_id="ig_test",
        created_at=123,
        model="gpt-image-2",
        result_b64="aW1hZ2U=",
        usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    assert response["output"] == [
        {
            "id": "ig_test",
            "type": "image_generation_call",
            "status": "completed",
            "result": "aW1hZ2U=",
        }
    ]
    chunks = list(iter_responses_image_sse(response))
    events = [chunk.split("\n", 1)[0] for chunk in chunks[:-1]]
    assert events == [
        "event: response.created",
        "event: response.output_item.added",
        "event: response.output_item.done",
        "event: response.completed",
    ]
    assert chunks[-1] == "data: [DONE]\n\n"
    completed = json.loads(chunks[-2].split("data: ", 1)[1])
    assert completed["response"]["output"][0]["type"] == "image_generation_call"
```

- [ ] **Step 6: Run new tests and verify RED**

Run:

```bash
pytest -q tests/test_openai_responses_protocol.py -k 'encode or response_and_sse'
```

Expected: failures report missing `encode_image_result`,
`build_responses_image_response`, or `iter_responses_image_sse`.

- [ ] **Step 7: Implement encoding, response construction, and SSE serialization**

Add these functions to `api/openai_responses.py`:

```python
def encode_image_result(
    image_bytes: bytes, output_format: str, output_compression: int | None
) -> str:
    if output_format == "png":
        encoded = image_bytes
    else:
        with Image.open(io.BytesIO(image_bytes)) as source:
            output = io.BytesIO()
            if output_format == "jpeg":
                source.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=90 if output_compression is None else output_compression,
                )
            else:
                source.save(
                    output,
                    format="WEBP",
                    quality=90 if output_compression is None else output_compression,
                )
            encoded = output.getvalue()
    return base64.b64encode(encoded).decode("ascii")


def _responses_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
    }
    result["total_tokens"] = int(
        usage.get("total_tokens") or result["input_tokens"] + result["output_tokens"]
    )
    for key in ("input_tokens_details", "output_tokens_details"):
        if isinstance(usage.get(key), Mapping):
            result[key] = dict(usage[key])
    return result


def build_responses_image_response(
    *,
    response_id: str,
    item_id: str,
    created_at: int,
    model: str,
    result_b64: str,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": item_id,
                "type": "image_generation_call",
                "status": "completed",
                "result": result_b64,
            }
        ],
        "usage": _responses_usage(usage),
    }


def _sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def iter_responses_image_sse(response: Mapping[str, Any]):
    final_response = dict(response)
    final_item = dict(final_response["output"][0])
    pending_response = dict(final_response)
    pending_response["status"] = "in_progress"
    pending_response["output"] = []
    pending_response.pop("usage", None)
    pending_item = {key: value for key, value in final_item.items() if key != "result"}
    pending_item["status"] = "in_progress"
    events = [
        ("response.created", {"type": "response.created", "sequence_number": 0, "response": pending_response}),
        ("response.output_item.added", {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": pending_item}),
        ("response.output_item.done", {"type": "response.output_item.done", "sequence_number": 2, "output_index": 0, "item": final_item}),
        ("response.completed", {"type": "response.completed", "sequence_number": 3, "response": final_response}),
    ]
    for event_name, payload in events:
        yield _sse(event_name, payload)
    yield "data: [DONE]\n\n"
```

- [ ] **Step 8: Run all protocol tests**

Run:

```bash
pytest -q tests/test_openai_responses_protocol.py
```

Expected: all tests pass without warnings.

- [ ] **Step 9: Commit the protocol unit**

```bash
git add api/openai_responses.py tests/test_openai_responses_protocol.py
git commit -m "feat: add responses image protocol helpers"
```

If `.git` remains read-only, verify `git diff --check` for both paths and record
this intended commit boundary without attempting permission changes.

---

### Task 2: Shared Adobe Image Artifact Executor

**Files:**
- Create: `core/image_generation.py`
- Create: `tests/test_image_generation.py`

**Interfaces:**
- Produces: immutable `GeneratedImageArtifact(job_id, path, image_bytes, metadata)` and `generate_image_artifact(...)`.
- Consumes: an Adobe client exposing `generate(**kwargs)`, existing model config dictionaries, a generated directory, progress callback, and generated-file accounting callback.

- [ ] **Step 1: Write failing executor tests**

Create `tests/test_image_generation.py`:

```python
from pathlib import Path

from core.image_generation import generate_image_artifact


class ReturningClient:
    generate_timeout = 60
    gpt_image_quality = "low"

    def __init__(self):
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return b"returned-image", {"progress": 100}


class FileWritingClient(ReturningClient):
    def generate(self, **kwargs):
        self.kwargs = kwargs
        kwargs["out_path"].write_bytes(b"file-image")
        return None, {"progress": 100}


def _model_config():
    return {
        "upstream_model_id": "gpt-image",
        "upstream_model_version": "2",
        "detail_level": "high",
    }


def test_executor_writes_returned_bytes_and_passes_adobe_options(tmp_path: Path):
    client = ReturningClient()
    writes = []
    artifact = generate_image_artifact(
        client=client,
        token="token",
        prompt="draw",
        aspect_ratio="1:1",
        output_resolution="1K",
        model_config=_model_config(),
        generated_dir=tmp_path,
        source_image_ids=["source-1"],
        progress_cb=lambda update: None,
        on_generated_file_written=lambda path, old, new: writes.append((path, old, new)),
        job_id="fixed",
    )
    assert artifact.image_bytes == b"returned-image"
    assert artifact.path.read_bytes() == b"returned-image"
    assert client.kwargs["quality_level"] == "low"
    assert client.kwargs["source_image_ids"] == ["source-1"]
    assert writes == [(tmp_path / "fixed.png", 0, len(b"returned-image"))]


def test_executor_reads_file_when_client_streams_to_out_path(tmp_path: Path):
    artifact = generate_image_artifact(
        client=FileWritingClient(),
        token="token",
        prompt="draw",
        aspect_ratio="1:1",
        output_resolution="1K",
        model_config=_model_config(),
        generated_dir=tmp_path,
        source_image_ids=[],
        progress_cb=None,
        on_generated_file_written=lambda path, old, new: None,
        job_id="fixed",
    )
    assert artifact.image_bytes == b"file-image"
```

- [ ] **Step 2: Run executor tests and verify RED**

Run:

```bash
pytest -q tests/test_image_generation.py
```

Expected: collection fails because `core.image_generation` does not exist.

- [ ] **Step 3: Implement the executor**

Create `core/image_generation.py`:

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class GeneratedImageArtifact:
    job_id: str
    path: Path
    image_bytes: bytes
    metadata: dict[str, Any]


def generate_image_artifact(
    *,
    client,
    token: str,
    prompt: str,
    aspect_ratio: str,
    output_resolution: str,
    model_config: Mapping[str, Any],
    generated_dir: Path,
    source_image_ids: Sequence[str],
    progress_cb: Callable[[dict], None] | None,
    on_generated_file_written: Callable[[Path, int, int], None],
    job_id: str | None = None,
) -> GeneratedImageArtifact:
    resolved_job_id = job_id or uuid.uuid4().hex
    path = generated_dir / f"{resolved_job_id}.png"
    try:
        old_size = int(path.stat().st_size) if path.exists() else 0
    except OSError:
        old_size = 0
    image_bytes, metadata = client.generate(
        token=token,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        output_resolution=output_resolution,
        upstream_model_id=str(model_config.get("upstream_model_id") or "gemini-flash"),
        upstream_model_version=str(model_config.get("upstream_model_version") or "nano-banana-2"),
        quality_level=(
            client.gpt_image_quality
            if str(model_config.get("upstream_model_id") or "") == "gpt-image"
            else None
        ),
        detail_level=model_config.get("detail_level"),
        source_image_ids=list(source_image_ids),
        timeout=client.generate_timeout,
        out_path=path,
        progress_cb=progress_cb,
    )
    if image_bytes is not None:
        path.write_bytes(image_bytes)
    final_bytes = path.read_bytes()
    on_generated_file_written(path, old_size, len(final_bytes))
    return GeneratedImageArtifact(
        job_id=resolved_job_id,
        path=path,
        image_bytes=final_bytes,
        metadata=dict(metadata or {}),
    )
```

- [ ] **Step 4: Run executor tests and verify GREEN**

Run:

```bash
pytest -q tests/test_image_generation.py
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit the executor unit**

```bash
git add core/image_generation.py tests/test_image_generation.py
git commit -m "refactor: centralize image artifact generation"
```

If commits are unavailable, run `git diff --check` for both files.

---

### Task 3: Migrate Existing Image Routes to the Shared Executor

**Files:**
- Modify: `api/routes/generation.py`
- Test: `tests/test_service.py`
- Test: `tests/test_images_edits.py`
- Test: `tests/test_generation_credit_context.py`

**Interfaces:**
- Consumes: `generate_image_artifact(...)` from Task 2.
- Produces: unchanged external behavior for Images generations, Images edits, and image Chat Completions.

- [ ] **Step 1: Establish the pre-refactor regression baseline**

Run:

```bash
pytest -q tests/test_images_edits.py tests/test_generation_credit_context.py tests/test_service.py
```

Expected: all existing tests pass. If a test already fails, record the failure
before editing and do not attribute it to this refactor.

- [ ] **Step 2: Import and use the executor in Images generations**

Add:

```python
from core.image_generation import generate_image_artifact
```

Replace the `/v1/images/generations` block that creates `job_id`, `out_path`,
calls `client.generate`, writes bytes, and calls `on_generated_file_written`
with:

```python
artifact = generate_image_artifact(
    client=client,
    token=token,
    prompt=prompt,
    aspect_ratio=ratio,
    output_resolution=output_resolution,
    model_config=model_conf,
    generated_dir=generated_dir,
    source_image_ids=[],
    progress_cb=_image_progress_cb,
    on_generated_file_written=on_generated_file_written,
)
image_url = public_image_url(request, artifact.job_id)
```

Keep the existing preview, usage, retry, and response code unchanged.

- [ ] **Step 3: Use the executor in Images edits**

After uploading the existing `source_image_ids`, replace its duplicated
generation block with:

```python
artifact = generate_image_artifact(
    client=client,
    token=token,
    prompt=prompt,
    aspect_ratio=ratio,
    output_resolution=output_resolution,
    model_config=model_conf,
    generated_dir=generated_dir,
    source_image_ids=source_image_ids,
    progress_cb=_image_progress_cb,
    on_generated_file_written=on_generated_file_written,
)
image_url = public_image_url(request, artifact.job_id)
set_request_preview(request, image_url, kind="image")
item = (
    {"b64_json": base64.b64encode(artifact.image_bytes).decode("ascii")}
    if response_format == "b64_json"
    else {"url": image_url}
)
```

Do not alter multipart validation or mask handling.

- [ ] **Step 4: Use the executor in image Chat Completions**

Inside the non-video branch, keep input image uploads and replace only the
duplicated Adobe generation block:

```python
artifact = generate_image_artifact(
    client=client,
    token=token,
    prompt=prompt,
    aspect_ratio=ratio,
    output_resolution=output_resolution,
    model_config=image_model_conf,
    generated_dir=generated_dir,
    source_image_ids=source_image_ids,
    progress_cb=_image_progress_cb,
    on_generated_file_written=on_generated_file_written,
)
image_url = public_image_url(request, artifact.job_id)
set_request_preview(request, image_url, kind="image")
response_content = f"![Generated Image]({image_url})"
```

- [ ] **Step 5: Run targeted route regression tests**

Run:

```bash
pytest -q tests/test_images_edits.py tests/test_generation_credit_context.py tests/test_service.py
```

Expected: the same tests that passed in Step 1 remain green.

- [ ] **Step 6: Run static diff checks**

```bash
python -m py_compile api/routes/generation.py core/image_generation.py
git diff --check -- api/routes/generation.py core/image_generation.py
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit the route migration**

```bash
git add api/routes/generation.py
git commit -m "refactor: share image generation across routes"
```

If `.git` remains read-only, preserve the intended boundary without staging.

---

### Task 4: Native `/v1/responses` Route

**Files:**
- Modify: `api/routes/generation.py`
- Create: `tests/test_openai_responses.py`

**Interfaces:**
- Consumes: all Task 1 protocol helpers, Task 2 shared executor, existing router callbacks, existing `resolve_ratio_and_resolution`, `resolve_model`, `load_input_images`, `run_with_token_retries`, and `build_image_usage`.
- Produces: authenticated `POST /v1/responses` with JSON or SSE output and the existing error semantics.

- [ ] **Step 1: Create a route test harness and failing non-streaming test**

Create `tests/test_openai_responses.py` with a valid PNG fake and injected
captures. The complete harness must pass every current `build_generation_router`
dependency rather than importing the global app:

```python
import base64
import io
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api.routes.generation import build_generation_router
from api.streaming import sse_chat_stream
from core.models import (
    MODEL_CATALOG,
    SUPPORTED_RATIOS,
    VIDEO_MODEL_CATALOG,
    resolve_model,
    resolve_ratio_and_resolution,
)


class QuotaError(Exception):
    pass


class AuthError(Exception):
    pass


class UpstreamError(Exception):
    pass


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


class FakeAdobeClient:
    generate_timeout = 60
    gpt_image_quality = "low"

    def __init__(self):
        self.generate_kwargs = None
        self.uploads = []
        self.error = None

    def upload_image(self, token, image_bytes, mime):
        self.uploads.append((token, image_bytes, mime))
        return f"source-{len(self.uploads)}"

    def generate(self, **kwargs):
        if self.error:
            raise self.error
        self.generate_kwargs = kwargs
        return png_bytes(), {"progress": 100}


class Harness:
    def __init__(self, tmp_path: Path, require_key=lambda request: None):
        self.adobe = FakeAdobeClient()
        self.credit_contexts = []
        self.previews = []
        self.retry_calls = []
        api = FastAPI()

        def run_with_token_retries(**kwargs):
            self.retry_calls.append(kwargs["operation_name"])
            return kwargs["run_once"]("token-value")

        api.include_router(build_generation_router(
            store=object(), token_manager=object(), client=self.adobe,
            credits_tracker=object(), request_log_store=object(),
            generated_dir=tmp_path, model_catalog=MODEL_CATALOG,
            video_model_catalog=VIDEO_MODEL_CATALOG,
            supported_ratios=SUPPORTED_RATIOS, resolve_model=resolve_model,
            resolve_ratio_and_resolution=resolve_ratio_and_resolution,
            require_service_api_key=require_key,
            set_request_task_progress=lambda request, **kwargs: None,
            set_request_credit_context=lambda request, model, resolution: self.credit_contexts.append((model, resolution)),
            run_with_token_retries=run_with_token_retries,
            set_request_error_detail=lambda request, **kwargs: "ERR-TEST",
            set_request_preview=lambda request, url, kind="image": self.previews.append((url, kind)),
            public_image_url=lambda request, job_id: f"https://images.test/{job_id}.png",
            public_generated_url=lambda request, filename: f"https://images.test/{filename}",
            resolve_video_options=lambda data: (True, "", "frame"),
            load_input_images=lambda messages: [],
            normalize_image_mime=lambda mime: str(mime or "image/jpeg"),
            set_request_logging_fields=lambda request, model, prompt: None,
            prepare_video_source_image=lambda image, ratio, resolution: (image, "image/png"),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "",
            sse_chat_stream=sse_chat_stream,
            on_generated_file_written=lambda path, old, new: None,
            quota_error_cls=QuotaError, auth_error_cls=AuthError,
            upstream_temp_error_cls=UpstreamError,
            logger=logging.getLogger("test-openai-responses"),
        ))
        self.http = TestClient(api)


def test_non_streaming_response_returns_image_generation_call(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post("/v1/responses", json={
        "model": "gpt-image-2",
        "input": "draw a blue square",
        "size": "1024x1024",
        "quality": "low",
        "stream": False,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["type"] == "image_generation_call"
    assert base64.b64decode(payload["output"][0]["result"]) == png_bytes()
    assert harness.credit_contexts == [("gpt-image-2", "1K")]
    assert harness.retry_calls == ["responses.create"]
    assert harness.previews[0][1] == "image"


def test_responses_requires_service_authentication(tmp_path: Path):
    def reject(request):
        raise HTTPException(status_code=401, detail="invalid api key")

    harness = Harness(tmp_path, require_key=reject)
    response = harness.http.post(
        "/v1/responses",
        json={"model": "gpt-image-2", "input": "draw"},
    )
    assert response.status_code == 401
```

- [ ] **Step 2: Run the non-streaming test and verify RED**

Run:

```bash
pytest -q tests/test_openai_responses.py::test_non_streaming_response_returns_image_generation_call
```

Expected: the non-streaming test fails on HTTP 404 because the route is not
registered. The authentication test may already return 404 rather than 401;
after route registration it must return 401.

- [ ] **Step 3: Implement the non-streaming route**

Import the Task 1 and Task 2 helpers in `api/routes/generation.py`, then add the
route after `_openai_image_error_response` so it can reuse that error mapper:

```python
from api.openai_responses import (
    ResponsesRequestError,
    build_responses_image_response,
    encode_image_result,
    iter_responses_image_sse,
    parse_responses_image_request,
)
from core.image_generation import generate_image_artifact
```

```python
    @router.post("/v1/responses")
    def openai_responses(data: dict, request: Request):
        require_service_api_key(request)
        try:
            parsed = parse_responses_image_request(data, model_catalog.keys())
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                {"size": parsed.size, "quality": parsed.quality}, parsed.image_model
            )
            model_conf = resolve_model(resolved_model_id)
            input_images = load_input_images(parsed.image_loader_messages())
        except ResponsesRequestError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "param": exc.param,
                    }
                },
            )
        except Exception as exc:
            return _openai_image_error_response(
                request, exc, endpoint="/v1/responses", model_label=str(data.get("model") or "")
            )

        set_request_logging_fields(request, resolved_model_id, parsed.prompt)
        set_request_credit_context(request, resolved_model_id, output_resolution)
        set_request_task_progress(request, task_status="IN_PROGRESS", task_progress=0.0)

        def _run_once(token: str):
            source_image_ids = [
                client.upload_image(token, image_bytes, image_mime)
                for image_bytes, image_mime in input_images
            ]

            def _image_progress_cb(update: dict):
                set_request_task_progress(
                    request,
                    task_status=str(update.get("task_status") or "IN_PROGRESS"),
                    task_progress=update.get("task_progress"),
                    upstream_job_id=update.get("upstream_job_id"),
                    retry_after=update.get("retry_after"),
                    error=update.get("error"),
                )

            artifact = generate_image_artifact(
                client=client, token=token, prompt=parsed.prompt,
                aspect_ratio=ratio, output_resolution=output_resolution,
                model_config=model_conf, generated_dir=generated_dir,
                source_image_ids=source_image_ids, progress_cb=_image_progress_cb,
                on_generated_file_written=on_generated_file_written,
            )
            image_url = public_image_url(request, artifact.job_id)
            set_request_preview(request, image_url, kind="image")
            usage = build_image_usage(
                parsed.prompt, output_resolution, ratio, len(source_image_ids)
            )
            result_b64 = encode_image_result(
                artifact.image_bytes, parsed.output_format, parsed.output_compression
            )
            return build_responses_image_response(
                response_id=f"resp_{uuid.uuid4().hex}",
                item_id=f"ig_{uuid.uuid4().hex}",
                created_at=int(time.time()), model=parsed.inbound_model,
                result_b64=result_b64, usage=usage,
            )

        try:
            response_payload = run_with_token_retries(
                request=request, operation_name="responses.create", run_once=_run_once
            )
            set_request_task_progress(request, task_status="COMPLETED", task_progress=100.0)
            if parsed.stream:
                return StreamingResponse(
                    iter_responses_image_sse(response_payload),
                    media_type="text/event-stream",
                )
            return response_payload
        except Exception as exc:
            return _openai_image_error_response(
                request, exc, endpoint="/v1/responses", model_label=resolved_model_id
            )
```

- [ ] **Step 4: Run the non-streaming test and verify GREEN**

```bash
pytest -q tests/test_openai_responses.py::test_non_streaming_response_returns_image_generation_call
```

Expected: 1 test passes.

- [ ] **Step 5: Add failing streaming, tool-shape, and input-image tests**

Extend the Harness so `load_input_images` captures its argument and returns one
small PNG for an `input_image`. Add:

```python
def test_streaming_response_emits_image_events_in_order(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post("/v1/responses", json={
        "model": "gpt-image-2", "input": "draw", "stream": True
    })
    assert response.status_code == 200
    event_names = [line[7:] for line in response.text.splitlines() if line.startswith("event: ")]
    assert event_names == [
        "response.created", "response.output_item.added",
        "response.output_item.done", "response.completed",
    ]
    assert "response.output_text" not in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_text_model_with_image_tool_uses_gpt_image_2_backend(tmp_path: Path):
    harness = Harness(tmp_path)
    response = harness.http.post("/v1/responses", json={
        "model": "gpt-5.4-mini",
        "input": "draw",
        "tools": [{"type": "image_generation", "quality": "high"}],
        "tool_choice": {"type": "image_generation"},
    })
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-5.4-mini"
    assert harness.credit_contexts == [("gpt-image-2", "4K")]


def test_input_image_is_uploaded_and_forwarded(tmp_path: Path):
    harness = Harness(tmp_path)
    harness.loaded_images = [(png_bytes(), "image/png")]
    response = harness.http.post("/v1/responses", json={
        "model": "gpt-image-2",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "edit"},
            {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
        ]}],
    })
    assert response.status_code == 200
    assert harness.adobe.uploads == [("token-value", png_bytes(), "image/png")]
    assert harness.adobe.generate_kwargs["source_image_ids"] == ["source-1"]
```

Implement `Harness.loaded_images` and have its injected `load_input_images`
return that list. Do not decode data URLs in this route test; decoding belongs
to the existing app loader and its existing tests.

- [ ] **Step 6: Run new route tests and verify RED where behavior is missing**

```bash
pytest -q tests/test_openai_responses.py
```

Expected: the streaming and text-tool tests pass once Step 3 is complete; the
input-image test initially fails until the harness capture and upload path are
fully wired. Confirm each failure is behavioral, not a fixture error.

- [ ] **Step 7: Complete streaming and input-image behavior**

Use `iter_responses_image_sse` exactly as shown in Step 3. Ensure
`parsed.image_loader_messages()` is passed to `load_input_images`, upload every
returned image, pass all source IDs to `generate_image_artifact`, and include
the uploaded image count in `build_image_usage`.

No response output may contain `message`, `output_text`, or the public preview
URL.

- [ ] **Step 8: Add and verify error mapping tests**

Add these exact validation and domain error tests:

```python
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"model": "gpt-image-2", "input": ""}, "input is required"),
        (
            {"model": "gpt-image-2", "input": "draw", "tool_choice": "none"},
            "tool_choice",
        ),
        (
            {"model": "gpt-image-2", "input": "draw", "background": "transparent"},
            "transparent",
        ),
        (
            {"model": "gpt-image-2", "input": "draw", "partial_images": 1},
            "partial_images",
        ),
        (
            {"model": "gpt-5.4-mini", "input": "draw"},
            "image_generation tool is required",
        ),
    ],
)
def test_responses_validation_errors_are_openai_shaped(tmp_path: Path, payload, message):
    response = Harness(tmp_path).http.post("/v1/responses", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert message in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("error", "status_code", "error_type"),
    [
        (QuotaError("quota"), 429, "rate_limit_error"),
        (AuthError("auth"), 401, "authentication_error"),
        (UpstreamError("upstream"), 503, "server_error"),
    ],
)
def test_responses_maps_generation_errors(
    tmp_path: Path, error, status_code, error_type
):
    harness = Harness(tmp_path)
    harness.adobe.error = error
    response = harness.http.post(
        "/v1/responses",
        json={"model": "gpt-image-2", "input": "draw"},
    )
    assert response.status_code == status_code
    assert response.json()["error"]["type"] == error_type
```

Run:

```bash
pytest -q tests/test_openai_responses.py
```

Expected: all route tests pass and every error body has an `error.type`.

- [ ] **Step 9: Run protocol and route suites together**

```bash
pytest -q tests/test_openai_responses_protocol.py tests/test_image_generation.py tests/test_openai_responses.py
```

Expected: all tests pass.

- [ ] **Step 10: Commit the native endpoint**

```bash
git add api/routes/generation.py tests/test_openai_responses.py
git commit -m "feat: add native responses image endpoint"
```

If commits remain unavailable, run `git diff --check` for both paths.

---

### Task 5: Request Logging, Documentation, and Full Verification

**Files:**
- Modify: `app.py`
- Create: `tests/test_openai_responses_logging.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `Responses` input string/array conventions from Task 1.
- Produces: request operation `responses.create`, prompt preview extraction, and public endpoint documentation.

- [ ] **Step 1: Write failing logging metadata tests**

Create `tests/test_openai_responses_logging.py`:

```python
import json

import app


def test_responses_path_maps_to_responses_create():
    assert app._resolve_request_operation("POST", "/v1/responses") == "responses.create"


def test_logging_extracts_responses_string_input_without_base64_output():
    fields = app._extract_logging_fields(json.dumps({
        "model": "gpt-image-2",
        "input": "draw a blue square",
        "result": "A" * 10000,
    }).encode())
    assert fields == {"model": "gpt-image-2", "prompt_preview": "draw a blue square"}


def test_logging_extracts_last_user_input_text_only():
    fields = app._extract_logging_fields(json.dumps({
        "model": "gpt-5.4-mini",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "edit this image"},
            {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 10000},
        ]}],
    }).encode())
    assert fields == {"model": "gpt-5.4-mini", "prompt_preview": "edit this image"}
```

- [ ] **Step 2: Run logging tests and verify RED**

```bash
pytest -q tests/test_openai_responses_logging.py
```

Expected: operation mapping is empty and input prompt previews are missing.

- [ ] **Step 3: Implement operation and safe prompt extraction**

In `app.py`, add:

```python
    if path == "/v1/responses":
        return "responses.create"
```

Add a local safe helper next to `_extract_logging_fields`:

```python
def _extract_responses_prompt(input_value: Any) -> str:
    if isinstance(input_value, str):
        return input_value.strip()
    if not isinstance(input_value, list):
        return ""
    for item in reversed(input_value):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_text", "text"}:
                text = str(part.get("text") or "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()
    return ""
```

Inside `_extract_logging_fields`, after `prompt` and message extraction remain
empty, assign:

```python
        if not prompt:
            prompt = _extract_responses_prompt(data.get("input"))
```

Never inspect `result`, `input_image.image_url`, or response bodies.

- [ ] **Step 4: Run logging tests and verify GREEN**

```bash
pytest -q tests/test_openai_responses_logging.py
```

Expected: 3 tests pass.

- [ ] **Step 5: Document the endpoint**

Add a concise README section showing both supported requests:

```markdown
### OpenAI Responses image generation

`POST /v1/responses` supports Adobe-backed image generation with either a
top-level `gpt-image-*` model or an `image_generation` tool. Successful output
uses `image_generation_call.result` base64 data; `stream: true` returns Responses
SSE events.

```json
{"model":"gpt-image-2","input":"draw a blue square","stream":false}
```

```json
{"model":"gpt-5.4-mini","input":"draw a blue square","tools":[{"type":"image_generation"}]}
```

Current limitations: no `/v1/responses/compact`, WebSocket mode, Files API,
mask input, transparent background, or partial preview images.
```

- [ ] **Step 6: Run all focused regression tests**

```bash
pytest -q \
  tests/test_openai_responses_protocol.py \
  tests/test_image_generation.py \
  tests/test_openai_responses.py \
  tests/test_openai_responses_logging.py \
  tests/test_images_edits.py \
  tests/test_generation_credit_context.py \
  tests/test_service.py
```

Expected: all focused tests pass.

- [ ] **Step 7: Run the complete test suite**

```bash
pytest -q
```

Expected: all tests pass. Record any pre-existing unrelated failure separately;
do not weaken assertions or revert user changes to obtain green output.

- [ ] **Step 8: Run source and diff checks**

```bash
python -m py_compile \
  api/openai_responses.py \
  core/image_generation.py \
  api/routes/generation.py \
  app.py
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 9: Repeat the two endpoint contract tests verbosely**

Run the exact non-streaming and streaming contract tests with full assertion
names visible, using the fake Adobe client rather than production credentials:

```bash
pytest -vv \
  tests/test_openai_responses.py::test_non_streaming_response_returns_image_generation_call \
  tests/test_openai_responses.py::test_streaming_response_emits_image_events_in_order
```

Expected: both tests pass; the first decodes the final base64 to the generated
PNG bytes, and the second verifies the exact SSE event order.

- [ ] **Step 10: Commit logging and documentation**

```bash
git add app.py tests/test_openai_responses_logging.py README.md
git commit -m "docs: document responses image compatibility"
```

If `.git` remains read-only, capture `git status --short` and `git diff --stat`
for the final handoff instead.

---

## Post-implementation Review and Deployment Gate

- [ ] Use the `requesting-code-review` skill and review the complete diff against the design specification.
- [ ] Confirm every new function is covered by a test that was observed failing before implementation.
- [ ] Confirm no base64 image data is written to request logs or error logs.
- [ ] Do not build/push/deploy until the user explicitly requests it after reviewing local test results.
- [ ] After deployment, directly test Adobe2api before changing sub2api settings.
- [ ] Only after direct Adobe tests pass, set the sub2api Adobe account to Force Responses + Auto Passthrough, keep WS off, Force Compact off, and disable Embeddings.
- [ ] Verify the same request through `https://tomapi.top/` and confirm sub2api records `image_count=1`.
