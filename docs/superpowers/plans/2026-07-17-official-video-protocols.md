# new-api Video Task Protocols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, persistent Sora and Veo asynchronous video task endpoints that the current new-api fork can submit, poll, and download without changing new-api.

**Architecture:** Extract file-oriented video generation from the chat route, then run it behind a persistent `VideoTaskStore` and a bounded `VideoTaskManager`. OpenAI and Gemini routes translate protocol requests into one `VideoTaskSpec`; the worker owns Token retries, final request-log upserts, and credit measurement without retaining an HTTP request.

**Tech Stack:** Python 3.10, FastAPI 0.109, Starlette `FileResponse`, `ThreadPoolExecutor`, dataclasses, JSONL persistence, pytest/TestClient.

## Global Constraints

- Keep existing `/v1/chat/completions` video models and behavior working.
- Support only `sora-2`, `sora-2-pro`, `veo-3.1-generate-preview`, and `veo-3.1-fast-generate-preview`.
- Keep at most 2 running and 20 queued official-protocol tasks.
- Persist no full prompt, negative prompt, image, or video input in `video_tasks.jsonl` or request logs.
- Production configuration must use HTTPS or a trusted private-network base URL.
- Reject unsupported media and safety parameters with 400; never silently ignore them.
- Do not modify the sibling new-api repository.
- Preserve unrelated dirty-worktree changes.

---

### Task 1: Shared Video File Generation

**Files:**
- Create: `core/video_generation.py`
- Create: `tests/test_video_generation.py`
- Modify: `api/routes/generation.py`

**Interfaces:**
- Produces: `GeneratedVideoFile(path: Path, mime_type: str, metadata: dict)`.
- Produces: `generate_video_file(*, client, token, video_conf, prompt, aspect_ratio, duration, generated_dir, task_id, resolution, negative_prompt, generate_audio, source_image_ids, entity_refs, reference_mode, timeout, progress_cb, on_generated_file_written) -> GeneratedVideoFile`.
- Consumes: existing `AdobeClient.generate_video` and generated-storage accounting callback.

- [ ] **Step 1: Write failing file-generation tests**

```python
def test_generate_video_file_uses_real_mime_extension_and_accounts(tmp_path):
    client = FakeVideoClient(content_type="video/webm", payload=b"webm")
    accounted = []
    result = generate_video_file(
        client=client, token="token", video_conf={}, prompt="p",
        aspect_ratio="16:9", duration=4, generated_dir=tmp_path,
        task_id="job", resolution="720p", negative_prompt="",
        generate_audio=True, source_image_ids=[], entity_refs=None,
        reference_mode="frame", timeout=600, progress_cb=None,
        on_generated_file_written=lambda path, old, new: accounted.append((path, old, new)),
    )
    assert result.path == tmp_path / "job.webm"
    assert result.mime_type == "video/webm"
    assert result.path.read_bytes() == b"webm"
    assert accounted == [(result.path, 0, 4)]

def test_generate_video_file_removes_partial_temp_on_failure(tmp_path):
    client = FailingVideoClient()
    with pytest.raises(RuntimeError, match="failed"):
        call_generate_video_file(client, tmp_path)
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Verify red state**

Run: `python -m pytest -q tests/test_video_generation.py`

Expected: collection fails because `core.video_generation` does not exist.

- [ ] **Step 3: Implement the shared helper**

```python
@dataclass(frozen=True)
class GeneratedVideoFile:
    path: Path
    mime_type: str
    metadata: dict

def video_extension_and_mime(metadata: dict) -> tuple[str, str]:
    content_type = str(metadata.get("contentType") or "video/mp4").lower()
    if "webm" in content_type:
        return "webm", "video/webm"
    if "ogg" in content_type or "ogv" in content_type:
        return "ogv", "video/ogg"
    return "mp4", "video/mp4"
```

`generate_video_file` must create `{task_id}.video.tmp`, call `client.generate_video(out_path=tmp_path)`, write returned bytes when present, rename the temp file to the MIME-derived final name, account the final size, and unlink both temp and incomplete final files on every exception.

- [ ] **Step 4: Replace the chat route's inline temp/download block**

Call `generate_video_file` inside the existing video `_run_once`, then use `result.path.name` with `public_generated_url` and preserve the existing response HTML, progress callback, Token retry, and preview behavior.

- [ ] **Step 5: Verify shared and chat tests**

Run: `python -m pytest -q tests/test_video_generation.py tests/test_token_retry_deadline.py tests/test_generation_credit_context.py`

Expected: all tests pass.

### Task 2: Persistent Task Store and Bounded State Machine

**Files:**
- Create: `core/video_tasks.py`
- Create: `tests/test_video_tasks.py`

**Interfaces:**
- Produces: `VideoTaskRecord`, `VideoTaskSpec`, `VideoTaskOutcome`, `VideoTaskStore`.
- Produces: `VideoTaskManager.submit(spec: VideoTaskSpec) -> VideoTaskRecord`.
- Produces: `VideoTaskCapacityError` and `VideoTaskStorageError`.
- Consumes: a runner callback `(spec, progress_callback) -> VideoTaskOutcome`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_store_reloads_latest_state_and_fails_interrupted_tasks(tmp_path):
    path = tmp_path / "video_tasks.jsonl"
    store = VideoTaskStore(path, max_items=3)
    queued = store.create(make_record("q", status="queued"))
    running = store.create(make_record("r", status="in_progress"))
    store.update(queued.id, progress=10)
    reloaded = VideoTaskStore(path, max_items=3)
    assert reloaded.get("q").status == "failed"
    assert reloaded.get("q").error_code == "service_restarted"
    assert reloaded.get("r").status == "failed"

def test_store_ignores_bad_json_and_compacts_by_task_id(tmp_path):
    path = tmp_path / "video_tasks.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    store = VideoTaskStore(path, max_items=2)
    for task_id in ("a", "b", "c"):
        store.create(make_record(task_id, status="completed"))
    store.compact()
    assert {row.id for row in store.list()} == {"b", "c"}
```

- [ ] **Step 2: Write failing capacity and lifecycle tests**

```python
def test_manager_rejects_when_running_and_pending_capacity_is_full(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, runner=lambda spec, progress: gate.wait(), max_workers=1, max_pending=1)
    manager.submit(make_spec("one"))
    manager.submit(make_spec("two"))
    with pytest.raises(VideoTaskCapacityError):
        manager.submit(make_spec("three"))
    gate.set()
    wait_for_status(manager.store, "one", "completed")

def test_manager_releases_slot_after_failure(tmp_path):
    manager = make_manager(tmp_path, runner=raising_runner, max_workers=1, max_pending=0)
    manager.submit(make_spec("one"))
    wait_for_status(manager.store, "one", "failed")
    assert manager.submit(make_spec("two")).id == "two"
```

- [ ] **Step 3: Verify red state**

Run: `python -m pytest -q tests/test_video_tasks.py`

Expected: collection fails because task types are undefined.

- [ ] **Step 4: Implement records and JSONL upserts**

```python
@dataclass(frozen=True)
class VideoTaskSpec:
    id: str
    protocol: str
    model: str
    prompt: str
    prompt_preview: str
    engine: str
    upstream_model: str
    duration: int
    aspect_ratio: str
    resolution: str
    requested_size: str
    negative_prompt: str
    credit_model_id: str
    result_url_prefix: str
    log_id: str

@dataclass
class VideoTaskRecord:
    id: str
    protocol: str
    model: str
    prompt_preview: str
    engine: str
    duration: int
    aspect_ratio: str
    resolution: str
    requested_size: str
    log_id: str
    status: str = "queued"
    progress: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    result_path: str | None = None
    result_mime: str | None = None
    result_url: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None

@dataclass(frozen=True)
class VideoTaskOutcome:
    result_path: Path
    result_mime: str
    result_url: str
```

Implement locked create/update/get/list, append-only upserts, latest-by-ID loading, terminal-record compaction, and startup conversion of queued/in_progress records to failed.

- [ ] **Step 5: Implement bounded executor admission**

Use `BoundedSemaphore(max_workers + max_pending)`. Acquire before persistence; release if persistence or executor submission fails; release in worker `finally`. On `close()`, mark not-started records failed before `shutdown(wait=False, cancel_futures=True)`.

- [ ] **Step 6: Verify task tests**

Run: `python -m pytest -q tests/test_video_tasks.py`

Expected: all tests pass without leaked executor threads.

### Task 3: Token-Aware Video Worker, Logs, and Credits

**Files:**
- Modify: `core/video_tasks.py`
- Modify: `tests/test_video_tasks.py`
- Modify: `tests/test_video_generation.py`
- Modify: `core/adobe_client.py`

**Interfaces:**
- Produces: `build_video_task_runner(*, token_manager, client, credits_tracker, request_log_store, generated_dir, generate_video, on_generated_file_written, quota_error_cls, auth_error_cls, upstream_temp_error_cls, adobe_error_cls, logger) -> Callable[[VideoTaskSpec, Callable[[float], None]], VideoTaskOutcome]`.
- Consumes: `token_manager`, `credits_tracker`, `request_log_store`, shared `generate_video_file`, existing Adobe exception classes.

- [ ] **Step 1: Write failing success/failure accounting tests**

```python
def test_worker_writes_completed_log_and_submits_credit_measurement(tmp_path):
    env = make_worker_env(tmp_path, generate_result=generated_mp4(tmp_path))
    record = env.manager.submit(make_spec("video_ok"))
    wait_for_status(env.store, record.id, "completed")
    payload = env.logs.latest[record.log_id]
    assert payload["task_status"] == "COMPLETED"
    assert payload["preview_kind"] == "video"
    assert payload["model"] == "sora-2"
    assert env.credits.complete_calls[0]["model_id"] == "firefly-sora2-4s-16x9"

def test_worker_failure_finishes_credit_without_completion(tmp_path):
    env = make_worker_env(tmp_path, generate_error=RuntimeError("boom"))
    record = env.manager.submit(make_spec("video_fail"))
    wait_for_status(env.store, record.id, "failed")
    assert env.credits.complete_calls == []
    assert env.credits.finish_calls[-1]["completed"] is False
```

- [ ] **Step 2: Verify red state**

Run: `python -m pytest -q tests/test_video_tasks.py -k 'credit or log'`

Expected: fails because the worker runner does not exist.

- [ ] **Step 3: Implement retry-aware runner**

For each distinct Token identity, call `credits_tracker.begin`, generate, and report success. Handle quota exhaustion, auth recovery, and retryable temporary errors with the same attempt limit and delay functions used by the current app. On each unsuccessful attempt call `credits_tracker.finish(token_id, spec.id, account_id=account_id, completed=False)`. On final success, upsert one `RequestLogRecord` payload and pass its store generation to `credits_tracker.complete`.

- [ ] **Step 4: Pass Veo negative prompt to Adobe payload**

Inside the Veo branch of `_build_video_payload`, add:

```python
if negative_prompt:
    payload["modelSpecificPayload"]["parameters"]["negativePrompt"] = negative_prompt
```

Add the builder assertion to `tests/test_video_generation.py`, verifying the field is absent when empty and present when nonempty.

- [ ] **Step 5: Verify worker and Adobe payload tests**

Run: `python -m pytest -q tests/test_video_tasks.py tests/test_video_generation.py`

Expected: all tests pass.

### Task 4: OpenAI Sora Task Protocol

**Files:**
- Create: `api/routes/openai_videos.py`
- Create: `tests/test_openai_videos.py`

**Interfaces:**
- Produces: `build_openai_videos_router(*, task_manager, task_store, require_service_api_key, public_generated_url, request_log_store) -> APIRouter`.
- Consumes: `VideoTaskSpec`, `VideoTaskCapacityError`, and `VideoTaskStorageError`.

- [ ] **Step 1: Write failing JSON and multipart validation tests**

```python
@pytest.mark.parametrize("content_type", ["json", "multipart"])
def test_create_sora_video_maps_request_to_task(content_type, harness):
    response = harness.post_video(content_type, model="sora-2-pro", prompt="p", seconds="12", size="1792x1024")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    spec = harness.manager.specs[-1]
    assert (spec.duration, spec.aspect_ratio, spec.resolution) == (12, "16:9", "1080p")

def test_create_rejects_media_and_body_over_one_mib(harness):
    assert harness.client.post("/v1/videos", json={"model":"sora-2","prompt":"p","input_reference":"file"}).status_code == 400
    assert harness.client.post("/v1/videos", content=b"x" * (1024 * 1024 + 1), headers={"content-type":"application/json"}).status_code == 400
```

- [ ] **Step 2: Write failing status/content tests**

Cover completed mp4/webm, queued 409, failed 424, missing/pruned 404, cross-protocol task 404, Bearer/X-API-Key success, and invalid key 401.

- [ ] **Step 3: Verify red state**

Run: `python -m pytest -q tests/test_openai_videos.py`

Expected: collection fails because the router does not exist.

- [ ] **Step 4: Implement limited request parsing and protocol errors**

Read at most 1 MiB before parsing. For multipart, cache the bounded body on the request and use `request.form()`. Normalize seconds to the response string, map size to ratio/resolution, reject every nonempty unsupported media field, and return stable OpenAI error objects.

- [ ] **Step 5: Implement status and content endpoints**

Serialize `VideoTaskRecord` to OpenAI video object. Use real `record.result_mime` and `record.result_path` for `FileResponse`. Validate `record.protocol == "openai"` before exposing it.

- [ ] **Step 6: Verify Sora route tests**

Run: `python -m pytest -q tests/test_openai_videos.py`

Expected: all tests pass.

### Task 5: Gemini Veo Model Actions and Operations

**Files:**
- Modify: `api/routes/gemini_native.py`
- Modify: `tests/test_gemini_parser.py`
- Modify: `tests/test_gemini_native.py`

**Interfaces:**
- Produces: `ParsedVeoRequest` and `parse_veo_request(raw_body: bytes, model_spec: GeminiModelSpec) -> ParsedVeoRequest`.
- Extends: `GeminiModelSpec.supported_actions: frozenset[str]` and optional video engine/upstream fields.
- Consumes: shared video task manager/store.

- [ ] **Step 1: Write failing action-registry tests**

```python
def test_video_models_only_support_predict_long_running():
    spec, action = resolve_model_action("veo-3.1-generate-preview:predictLongRunning")
    assert spec.family == "video"
    assert action == "predictLongRunning"
    with pytest.raises(GeminiNativeError):
        resolve_model_action("veo-3.1-generate-preview:generateContent")
```

- [ ] **Step 2: Write failing Veo parser matrix**

Test exact-one instance, prompt, camel/snake fields, 4/6/8 duration, 720p, 1080p only at 8 seconds, 4K rejection, media rejection, personGeneration rejection, and negativePrompt retention.

- [ ] **Step 3: Write failing submit and operation tests**

Assert submit returns `models/{model}/operations/{id}`, running/completed/failed objects match new-api's expected shape, completed URI ends with the real extension and contains only `key=proxy`, and model/protocol mismatches return Google 404.

- [ ] **Step 4: Verify red state**

Run: `python -m pytest -q tests/test_gemini_parser.py tests/test_gemini_native.py -k 'veo or video_model or predictLongRunning or operation'`

Expected: tests fail because video actions and parser are absent.

- [ ] **Step 5: Implement per-model actions and parser**

Make `resolve_model_action` consult `spec.supported_actions`; make `model_resource` return that list. Keep image/text actions unchanged. Route `predictLongRunning` to `parse_veo_request` and task submission before the existing image parser/generator.

- [ ] **Step 6: Implement operation polling**

Add `GET /v1beta/models/{model}/operations/{op_id}`. Validate API key, protocol, and model; map task state; append `key=proxy` with structured URL query handling rather than string concatenation.

- [ ] **Step 7: Verify Gemini tests**

Run: `python -m pytest -q tests/test_gemini_parser.py tests/test_gemini_native.py`

Expected: all tests pass.

### Task 6: Application Wiring, Middleware, and Shutdown

**Files:**
- Modify: `app.py`
- Modify: `tests/test_gemini_native.py`
- Modify: `tests/test_admin_proxy.py`

**Interfaces:**
- Instantiates: one global `VideoTaskStore` and `VideoTaskManager`.
- Registers: OpenAI video router and Gemini video dependencies.
- Extends: `_resolve_request_operation` and `_gemini_model_from_path`.

- [ ] **Step 1: Write failing operation/logging tests**

```python
@pytest.mark.parametrize(("method", "path", "expected"), [
    ("POST", "/v1/videos", "videos.create"),
    ("GET", "/v1/videos/video_x", "videos.get"),
    ("GET", "/v1/videos/video_x/content", "videos.content"),
    ("POST", "/v1beta/models/veo-3.1-generate-preview:predictLongRunning", "gemini.predictLongRunning"),
    ("GET", "/v1beta/models/veo-3.1-generate-preview/operations/op", "gemini.operations.get"),
])
def test_resolve_video_operations(method, path, expected):
    assert app_module._resolve_request_operation(method, path) == expected
```

Also assert externally managed submit logs are not overwritten by middleware finalization.

- [ ] **Step 2: Verify red state**

Run: `python -m pytest -q tests/test_gemini_native.py -k 'video_operation or externally_managed'`

Expected: operation mappings are empty or incorrect.

- [ ] **Step 3: Wire global services and routers**

Instantiate the store under `DATA_DIR`, create the shared runner and manager after existing client/log/credit services, include the Sora router, and pass manager/store/public URL dependencies to Gemini. Register an application shutdown handler that calls `video_task_manager.close()` before `credits_tracker.close()`.

- [ ] **Step 4: Extend middleware safely**

Recognize the five new operations. Parse the Gemini model before `/operations/`. When `request.state.log_managed_externally` is true, remove live state but skip the middleware's final RequestLogStore write and credit finalizer.

- [ ] **Step 5: Verify application tests**

Run: `python -m pytest -q tests/test_gemini_native.py tests/test_admin_proxy.py tests/test_request_log_store.py`

Expected: all tests pass.

### Task 7: Documentation and Full Regression

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-17-official-video-protocols-design.md` only if implementation reveals an exact contract correction.

**Interfaces:**
- Documents the externally callable protocol and new-api pricing/configuration.

- [ ] **Step 1: Add endpoint examples and limits**

Document both channel types, HTTPS/private base URL requirement, Sora per-second multiplier behavior, Gemini fixed per-call limitation, model/parameter matrices, polling, content download, queue limit, and restart behavior.

- [ ] **Step 2: Run focused suite**

Run:

```bash
python -m pytest -q \
  tests/test_video_generation.py \
  tests/test_video_tasks.py \
  tests/test_openai_videos.py \
  tests/test_gemini_parser.py \
  tests/test_gemini_native.py \
  tests/test_generation_credit_context.py \
  tests/test_token_retry_deadline.py
```

Expected: all focused tests pass.

- [ ] **Step 3: Run complete suite**

Run: `python -m pytest -q`

Expected: all tests pass; unrelated pre-existing test failures, if any, are reported without reverting user changes.

- [ ] **Step 4: Validate repository and task persistence hygiene**

Run: `git diff --check` and confirm no test-generated `data/video_tasks.jsonl`, generated video, or temporary file is tracked.

- [ ] **Step 5: Review deployment prerequisites**

Confirm `python-multipart` remains installed, data/generated volumes are writable, new-api Gemini version is `v1beta`, public base URL is reachable from new-api, and the configured URL is HTTPS or private-network HTTP.
