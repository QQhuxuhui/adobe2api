# 视频通用模型映射与生命周期修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Sora/Veo 通用模型名按请求参数匹配 Adobe 后缀模型，同时修复视频任务的参数校验、持久化、重启、关机、下载安全和积分日志收尾。

**Architecture:** 在 `core/models/video_resolver.py` 建立独立的视频参数解析器，输入公共或旧后缀模型名和请求参数，输出统一视频配置及具体积分模型 ID。协议路由、旧聊天路由和任务 worker 只消费解析结果，不各自重做匹配。任务 store/manager 保持线程安全，应用层负责重启日志和生成文件收尾。

**Tech Stack:** Python 3、FastAPI、`ThreadPoolExecutor`、JSONL store、pytest。

> 本次环境的 `.git` 目录只读，计划中的分片提交无法执行；代码与测试均已在工作区完成。

## Global Constraints

- 公共模型名必须支持 `sora-2`、`sora-2-pro`、`veo-3.1-generate-preview`、`veo-3.1-fast-generate-preview`。
- 旧 `firefly-*` 视频模型 ID 继续可调用，且不删除现有行为。
- Sora 默认 `size` 是 `720x1280`，Veo 默认 `duration=8`、`aspect_ratio=16:9`、`resolution=720p`。
- Veo `1080p` 只允许 8 秒；Sora Pro 高尺寸映射 Adobe 1080p，普通 Sora 不允许高尺寸。
- 未支持的非空媒体、安全或生成参数必须在提交前返回 400，不能静默忽略。
- 生成文件只能从配置的 `generated_dir` 内提供下载。
- 运行中 worker 在积分 tracker 关闭前必须完成；重启 active 任务必须有 FAILED 日志终态。
- 不修改 `new-api` 源码，不新增图生视频、编辑、webhook、Veo 4K 或 Sora 16/20 秒。

## 文件结构

- Create: `core/models/video_resolver.py` — 公共/后缀视频模型与请求参数的统一解析。
- Modify: `core/models/catalog.py` — 注册四个公共动态模型别名及默认值。
- Modify: `api/routes/generation.py`、`app.py` — 将旧聊天视频路由接到统一解析器，区分日志模型和积分模型。
- Modify: `api/routes/openai_videos.py`、`api/routes/gemini_native.py` — 严格参数校验、默认值和安全下载边界。
- Modify: `core/video_tasks.py` — 状态写入失败收尾、重启记录回调、关机等待和日志异常隔离。
- Modify: `core/credits_tracker.py`、`core/stores.py`、`app.py` — 保证生命周期收尾不丢积分/日志。
- Test: `tests/test_video_model_resolver.py`、`tests/test_openai_videos.py`、`tests/test_gemini_parser.py`、`tests/test_gemini_native.py`、`tests/test_video_tasks.py`、`tests/test_generation_credit_context.py`。

### Task 1: 动态视频模型解析

**Files:**
- Create: `core/models/video_resolver.py`
- Modify: `core/models/catalog.py`
- Test: `tests/test_video_model_resolver.py`

**Interfaces:**
- Consumes: `VIDEO_MODEL_CATALOG` and a public/legacy model ID plus request mapping.
- Produces: `ResolvedVideoModel` with `public_model_id`, `catalog_model_id`, `engine`, `upstream_model`, `duration`, `aspect_ratio`, `resolution`, `requested_size`, `credit_model_id`, `reference_mode`, and `generate_audio`.

- [x] **Step 1: Write failing resolver tests.**

```python
import pytest

from core.models.video_resolver import resolve_video_model


def test_sora_alias_defaults_to_new_api_portrait_size():
    resolved = resolve_video_model("sora-2", {})
    assert (resolved.duration, resolved.aspect_ratio, resolved.resolution) == (
        4,
        "9:16",
        "720p",
    )
    assert resolved.requested_size == "720x1280"
    assert resolved.credit_model_id == "firefly-sora2-4s-9x16"


def test_sora_pro_maps_high_size_to_1080p():
    resolved = resolve_video_model(
        "sora-2-pro", {"seconds": "12", "size": "1792x1024"}
    )
    assert (resolved.duration, resolved.aspect_ratio, resolved.resolution) == (
        12,
        "16:9",
        "1080p",
    )
    assert resolved.credit_model_id == "firefly-sora2-pro-12s-16x9"


def test_veo_fast_alias_maps_duration_ratio_and_resolution():
    resolved = resolve_video_model(
        "veo-3.1-fast-generate-preview",
        {"durationSeconds": 8, "aspectRatio": "9:16", "resolution": "1080p"},
    )
    assert resolved.engine == "veo31-fast"
    assert resolved.credit_model_id == "firefly-veo31-fast-8s-9x16-1080p"


def test_veo_rejects_1080p_when_duration_is_not_eight():
    with pytest.raises(VideoModelRequestError, match="1080p"):
        resolve_video_model(
            "veo-3.1-generate-preview",
            {"duration": 6, "resolution": "1080p"},
        )
```

- [x] **Step 2: Run the resolver tests and verify they fail because the resolver and aliases do not exist.**

Run: `python -m pytest -q tests/test_video_model_resolver.py`

Expected: collection or assertion failures for the missing `core.models.video_resolver` interface.

- [x] **Step 3: Add the four dynamic aliases to `VIDEO_MODEL_CATALOG`.**

Register these exact entries without removing legacy entries:

```python
VIDEO_MODEL_CATALOG.update(
    {
        "sora-2": {
            "dynamic": True,
            "engine": "sora2",
            "upstream_model": "openai:firefly:colligo:sora2",
            "default_duration": 4,
            "default_size": "720x1280",
            "description": "Sora 2 video (parameters select Adobe output)",
        },
        "sora-2-pro": {
            "dynamic": True,
            "engine": "sora2-pro",
            "upstream_model": "openai:firefly:colligo:sora2-pro",
            "default_duration": 4,
            "default_size": "720x1280",
            "description": "Sora 2 Pro video (parameters select Adobe output)",
        },
        "veo-3.1-generate-preview": {
            "dynamic": True,
            "engine": "veo31-standard",
            "upstream_model": "google:firefly:colligo:veo31",
            "default_duration": 8,
            "default_aspect_ratio": "16:9",
            "default_resolution": "720p",
            "description": "Veo 3.1 video (parameters select Adobe output)",
        },
        "veo-3.1-fast-generate-preview": {
            "dynamic": True,
            "engine": "veo31-fast",
            "upstream_model": "google:firefly:colligo:veo31-fast",
            "default_duration": 8,
            "default_aspect_ratio": "16:9",
            "default_resolution": "720p",
            "description": "Veo 3.1 Fast video (parameters select Adobe output)",
        },
    }
)
```

- [x] **Step 4: Implement `ResolvedVideoModel` and `resolve_video_model`.**

Use strict coercion helpers that reject booleans, non-finite numbers, fractional durations, unsupported sizes, ratios, and resolutions. For legacy IDs, read the existing catalog configuration and preserve its fixed values. For aliases, use the exact defaults and suffix rules from the design spec. Set `credit_model_id` to the concrete `firefly-*` ID while keeping `public_model_id` unchanged.

- [x] **Step 5: Run the resolver tests and the catalog import tests.**

Run: `python -m pytest -q tests/test_video_model_resolver.py tests/test_gemini_parser.py -k 'model or video'`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the resolver slice.**

```bash
git add core/models/catalog.py core/models/video_resolver.py tests/test_video_model_resolver.py
git commit -m "feat: 添加视频通用模型动态映射"
```

### Task 2: Wire aliases into routes and preserve model/credit identity

**Files:**
- Modify: `api/routes/generation.py`
- Modify: `app.py`
- Modify: `tests/test_generation_credit_context.py`
- Modify: `tests/test_openai_videos.py`
- Modify: `tests/test_gemini_native.py`

**Interfaces:**
- Consumes: `resolve_video_model` from Task 1.
- Produces: `/v1/models` entries and chat/async tasks with public log model plus concrete credit model.

- [x] **Step 1: Add failing route tests for alias exposure and chat mapping.**

```python
def test_v1_models_exposes_public_video_aliases(harness):
    response = harness.client.get("/v1/models", headers=harness.headers)
    ids = {item["id"] for item in response.json()["data"]}
    assert {"sora-2", "sora-2-pro", "veo-3.1-generate-preview"}.issubset(ids)


def test_chat_video_alias_uses_request_parameters_for_adobe_mapping(tmp_path):
    client, credit_contexts = make_client(tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "veo-3.1-generate-preview",
            "messages": [{"role": "user", "content": "p"}],
            "duration": 8,
            "aspect_ratio": "9:16",
            "resolution": "1080p",
        },
    )
    assert response.status_code == 200
    assert credit_contexts == [("firefly-veo31-8s-9x16-1080p", "1080p")]
```

- [x] **Step 2: Run these tests and verify current suffix-only behavior fails.**

Run: `python -m pytest -q tests/test_generation_credit_context.py tests/test_openai_videos.py -k 'alias or models or video'`

Expected: aliases are absent or rejected, and the captured config does not match request parameters.

- [x] **Step 3: Expose aliases from `/v1/models` and route model validation.**

Use the catalog entries from Task 1 for listing. Update the invalid-video prefix check to recognize the four public aliases while retaining the existing `firefly-*` prefix checks.

- [x] **Step 4: Replace chat route field lookup with the resolver.**

When `model` is a public alias or legacy video ID, call `resolve_video_model(model, data)`. Use its values for `duration`, `ratio`, `video_resolution`, `video_engine`, `upstream_model`, `reference_mode`, and `credit_model_id`. Preserve user-facing request logging with the original model name and set a separate `request.state.log_credit_model_id` to the concrete credit ID before token execution.

- [x] **Step 5: Keep official Sora/Veo routes aligned with the resolver defaults.**

Change the Sora async default size to `720x1280`. Keep official `VideoTaskSpec.model` as `sora-2` or the Gemini model name, and continue setting `credit_model_id` to the concrete suffix returned by the route parser/resolver.

- [x] **Step 6: Run route and credit tests.**

Run: `python -m pytest -q tests/test_openai_videos.py tests/test_gemini_native.py tests/test_generation_credit_context.py`

Expected: all route mapping, task submission, and credit model assertions pass.

- [ ] **Step 7: Commit the route integration slice.**

```bash
git add api/routes/generation.py api/routes/openai_videos.py app.py tests/test_openai_videos.py tests/test_gemini_native.py tests/test_generation_credit_context.py
git commit -m "feat: 接入视频通用模型参数映射"
```

### Task 3: Strict protocol validation and safe downloads

**Files:**
- Modify: `api/routes/openai_videos.py`
- Modify: `api/routes/gemini_native.py`
- Modify: `app.py`
- Test: `tests/test_openai_videos.py`, `tests/test_gemini_parser.py`, `tests/test_gemini_native.py`

**Interfaces:**
- Consumes: parsed request values and `generated_dir` passed from `app.py`.
- Produces: deterministic 400 responses for unsupported/non-finite parameters and 404 for out-of-root video files.

- [x] **Step 1: Add failing tests for unsupported fields and non-finite values.**

```python
@pytest.mark.parametrize("field", ["metadata", "inputReference", "safety_identifier"])
def test_sora_rejects_nonempty_unsupported_fields(harness, field):
    value = {"input": {"media": ["image"]}} if field == "metadata" else "value"
    response = harness.client.post(
        "/v1/videos",
        headers=harness.headers,
        json={"model": "sora-2", "prompt": "p", field: value},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"


def test_veo_rejects_nonempty_unsupported_generation_parameter():
    response = post(
        harness,
        "veo-3.1-generate-preview",
        "predictLongRunning",
        {"instances": [{"prompt": "p"}], "parameters": {"seed": 1}},
    )
    assert_google_error(response, 400, "INVALID_ARGUMENT")


def test_nonfinite_video_duration_is_a_bad_request(harness):
    response = harness.client.post(
        "/v1/videos",
        headers=harness.headers,
        json={"model": "sora-2", "prompt": "p", "seconds": float("inf")},
    )
    assert response.status_code == 400
```

- [x] **Step 2: Run the tests and verify current parsers either accept the field or raise 500.**

Run: `python -m pytest -q tests/test_openai_videos.py tests/test_gemini_parser.py tests/test_gemini_native.py -k 'unsupported or finite or duration'`

Expected: failures showing silent acceptance or an incorrect 500 response.

- [x] **Step 3: Implement strict allowed-field validation.**

OpenAI accepts only `model`, `prompt`, `seconds`, and `size` as meaningful fields; known media/safety aliases and nested `metadata.input.media` are rejected when non-empty. Gemini accepts only `instances[0].prompt` and the documented `parameters` keys; non-empty unknown parameters are rejected. Empty optional values remain compatible where the existing protocol permits them. Catch `OverflowError` and reject non-finite numeric values before integer conversion.

- [x] **Step 4: Add generated-root containment to the OpenAI video router.**

Extend `build_openai_videos_router` with `generated_dir: Path`. Resolve both the configured root and record path, require `path.is_relative_to(root)` (or the equivalent `relative_to` check), require a regular file, and permit only the video MIME/extension combinations produced by `generate_video_file`. Return the existing `video_file_not_found` error for violations.

- [x] **Step 5: Wire `generated_dir` through `app.py` and test path traversal.**

Create a completed task whose persisted `result_path` points outside the generated root; assert `/v1/videos/{id}/content` returns 404 and never reads the file.

- [x] **Step 6: Run the validation and download tests.**

Run: `python -m pytest -q tests/test_openai_videos.py tests/test_gemini_parser.py tests/test_gemini_native.py`

Expected: all selected tests pass.

- [ ] **Step 7: Commit the protocol hardening slice.**

```bash
git add api/routes/openai_videos.py api/routes/gemini_native.py app.py tests/test_openai_videos.py tests/test_gemini_parser.py tests/test_gemini_native.py
git commit -m "fix: 严格校验视频参数并限制下载路径"
```

### Task 4: Make task persistence and lifecycle terminal

**Files:**
- Modify: `core/video_tasks.py`
- Modify: `app.py`
- Modify: `core/credits_tracker.py` only if a small shutdown-safe helper is required
- Test: `tests/test_video_tasks.py`

**Interfaces:**
- Consumes: existing `VideoTaskStore`, `VideoTaskManager`, request log store, and credits tracker.
- Produces: restart callback records, retry-safe worker state transitions, and blocking manager shutdown that finishes running workers before credits close.

- [x] **Step 1: Add failing tests for initial/final store-write failures.**

```python
class FailingOnceVideoTaskStore(VideoTaskStore):
    def __init__(self, path, *, fail_updates):
        super().__init__(path)
        self._remaining_failures = fail_updates

    def update(self, task_id, **changes):
        if self._remaining_failures and changes.get("status") == "in_progress":
            self._remaining_failures -= 1
            raise VideoTaskStorageError("transient state write failure")
        return super().update(task_id, **changes)


def successful_runner(spec, progress):
    return VideoTaskOutcome(Path(f"{spec.id}.mp4"), "video/mp4", "url")


def blocking_manager(tmp_path, started, release):
    store = VideoTaskStore(tmp_path / "tasks.jsonl")

    def runner(spec, progress):
        started.set()
        release.wait(timeout=2)
        return VideoTaskOutcome(Path(f"{spec.id}.mp4"), "video/mp4", "url")

    manager = VideoTaskManager(store, runner, max_workers=1, max_pending=0)
    return manager


def test_manager_converts_transient_in_progress_write_failure_to_failed(tmp_path):
    store = FailingOnceVideoTaskStore(tmp_path / "tasks.jsonl", fail_updates=1)
    manager = VideoTaskManager(store, successful_runner, max_workers=1, max_pending=0)
    try:
        manager.submit(make_spec("write-fails"))
        failed = wait_for_status(store, "write-fails", "failed")
        assert failed.error_code == "task_state_persist_failed"
    finally:
        manager.close()


def test_close_waits_for_running_worker_before_return(tmp_path):
    started = threading.Event()
    release = threading.Event()
    manager = blocking_manager(tmp_path, started, release)
    manager.submit(make_spec("running"))
    assert started.wait(timeout=1)
    release.set()
    manager.close()
    assert manager.store.get("running").status == "completed"
```

- [x] **Step 2: Run the lifecycle tests and verify the current worker leaves queued state or returns before completion.**

Run: `python -m pytest -q tests/test_video_tasks.py -k 'write or close or shutdown'`

Expected: failures for the uncaught initial update and non-blocking shutdown behavior.

- [x] **Step 3: Add a best-effort terminal-state helper.**

Wrap the initial `in_progress` update, runner invocation, and completed update in one guarded flow. On any exception, call a helper that retries the failure update once, stores `task_state_persist_failed` when the failure came from persistence, and logs the final persistence exception without allowing the future to die silently. Progress callback updates must ignore storage errors so an advisory progress write cannot abort Adobe generation.

- [x] **Step 4: Make `VideoTaskStore` report interrupted records.**

Change `_fail_interrupted_locked` to return the updated records and add a one-shot `take_interrupted()` accessor. The constructor still persists `service_restarted` records before returning; the app consumes the records to finalize request logs and remove task-specific `.video.tmp`, `.mp4`, `.webm`, and `.ogv` artifacts.

- [x] **Step 5: Make shutdown cancel pending work and wait for running work.**

Keep `_closed` admission behavior, mark futures canceled before execution as `service_shutdown`, invoke the existing final-log callback for them, and call `executor.shutdown(wait=True, cancel_futures=True)`. The app shutdown handler must call `video_task_manager.close()` before `credits_tracker.close()`; running workers therefore finish their credit measurement first.

- [x] **Step 6: Finalize restarted/canceled request logs without credit leaks.**

Add an app callback that upserts a FAILED `RequestLogRecord` using the task’s `log_id`, protocol path, public model, preview-free payload, `service_restarted` or `service_shutdown` error, and prompt preview. Do not call `CreditsTracker.complete` for these records; a fresh process has no active attribution to finish.

- [x] **Step 7: Isolate post-generation logging and credit errors.**

Keep the generated `VideoTaskOutcome` successful when `request_log_store.upsert` or the asynchronous credits queue fails after Adobe has returned a file. Log the accounting failure and let the manager persist `completed`; never re-run generation or mark the file as a generation failure solely because observability failed.

- [ ] **Step 8: Run lifecycle tests and commit.**

Run: `python -m pytest -q tests/test_video_tasks.py tests/test_generation_credit_context.py`

Expected: all task state, retry, logging, and credit assertions pass.

```bash
git add core/video_tasks.py app.py core/credits_tracker.py tests/test_video_tasks.py tests/test_generation_credit_context.py
git commit -m "fix: 收敛视频任务持久化与生命周期"
```

### Task 5: Documentation and integration regression coverage

**Files:**
- Modify: `README.md`, `README_EN.md`
- Modify: `tests/test_gemini_native.py`, `tests/test_openai_videos.py`, `tests/test_generation_credit_context.py`

- [x] **Step 1: Add failing documentation/contract assertions where route behavior is currently undocumented.**

Add tests asserting `/v1/models` contains all four public aliases, old suffix IDs remain accepted, and the generated credit model ID is concrete while the request log model remains public.

- [x] **Step 2: Update the model and deployment tables.**

Document the four public aliases, Sora `seconds`/`size`, Veo `durationSeconds`/`aspectRatio`/`resolution`, default values, suffix compatibility, and the requirement to configure `public_base_url` to a URL reachable by new-api for Veo downloads.

- [x] **Step 3: Run the cross-route integration tests.**

Run: `python -m pytest -q tests/test_openai_videos.py tests/test_gemini_native.py tests/test_generation_credit_context.py`

Expected: all public/legacy model and logging assertions pass.

- [ ] **Step 4: Commit documentation and regression coverage.**

```bash
git add README.md README_EN.md tests/test_openai_videos.py tests/test_gemini_native.py tests/test_generation_credit_context.py
git commit -m "docs: 补充视频通用模型与部署说明"
```

### Task 6: Full verification and review handoff

**Files:**
- Test only: all repository tests.

- [x] **Step 1: Run focused video suites.**

Run: `python -m pytest -q tests/test_video_model_resolver.py tests/test_video_generation.py tests/test_video_tasks.py tests/test_openai_videos.py tests/test_gemini_parser.py tests/test_gemini_native.py`

Expected: all focused tests pass.

- [x] **Step 2: Run the complete test suite.**

Run: `python -m pytest -q`

Expected: all tests pass with no unhandled warnings.

- [x] **Step 3: Inspect the final diff.**

Run: `git diff origin/dev...HEAD --check` and `git status --short`.

Expected: no whitespace errors; unrelated existing worktree changes remain untouched.

- [x] **Step 4: Request a code review focused on model mapping and lifecycle races.**

Review the final diff for public-vs-credit model identity, default-value compatibility with `new-api`, persistence failure handling, shutdown ordering, and path containment before handing the changes back.
