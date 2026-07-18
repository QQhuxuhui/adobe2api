# Request Log Credits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure or estimate the Adobe credit cost of each successful generation request, persist the learned costs, and show the result in the admin request log.

**Architecture:** A single-worker `CreditsTracker` owns per-account attribution state, refreshes Adobe balances outside the request path, learns clean deltas, and appends a full updated log payload. `RequestLogStore` resolves append-only updates by log ID and rejects stale post-clear backfills, while middleware and background jobs bind attempts to accounts and submit only final successful generations. A dependency-free browser helper formats the three credit display states.

**Tech Stack:** Python 3.10, FastAPI middleware/background tasks, `threading`/`queue`, JSONL storage, vanilla JavaScript, pytest, Node built-in test runner.

## Global Constraints

- Balance requests must never block the async or synchronous generation response path.
- Only successful generation attempts with a token ID are measured; failed attempts and non-generation operations keep unknown credits.
- Tokens for the same Adobe account share in-flight state, completion counts, and balance snapshots because the upstream balance is account-scoped.
- Clean measurement requires a known previous balance, exactly one completion since that snapshot, no overlapping request, no in-flight request, and no attribution-state change during refresh.
- Every successful balance refresh establishes the next snapshot; unclean or failed measurements fall back to the learned price for the same cost key.
- Learned prices persist in `data/credit_costs_learned.json`; a missing or corrupt file starts with an empty table.
- Existing logs are not backfilled.
- A clear operation invalidates every queued backfill written under the previous log-store generation.
- No new runtime or test dependency.

---

### Task 1: Append-Only Log Update Semantics

**Files:**
- Modify: `core/stores.py`
- Create: `tests/test_request_log_store.py`

**Interfaces:**
- Produces: `RequestLogStore.list(limit, page) -> (latest_unique_rows, unique_total)`.
- Produces: `RequestLogStore.stats(...)` over the same latest unique rows.
- Produces: unique-ID truncation at `max_items` and generation-conditional backfill.

- [ ] **Step 1: Write failing tests**

Create records `a`, `b`, then append a second full payload for `a`. Assert list returns two rows, the latest `a` payload wins, pagination and stats count unique IDs, truncation retains `max_items` unique rows, and a clear rejects an older generation's backfill.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_request_log_store.py`

Expected: duplicate rows and inflated totals from the current raw-line implementation.

- [ ] **Step 3: Implement latest-by-ID reading**

Add a locked JSONL reader that ignores malformed lines, keeps the last dictionary for every non-empty ID, keeps anonymous legacy rows distinct, and sorts by numeric `ts` descending. Reuse it in `list()` and `stats()`.

- [ ] **Step 4: Run GREEN**

Run: `pytest -q tests/test_request_log_store.py`

Expected: all tests pass.

### Task 2: Credits Tracker Core

**Files:**
- Create: `core/credits_tracker.py`
- Create: `tests/test_credits_tracker.py`

**Interfaces:**
- Produces: `derive_cost_key(model_id, output_resolution, model_catalog, video_model_catalog) -> str`.
- Produces: `CreditsTracker.begin(token_id, request_id, account_id=None) -> None`.
- Produces: `CreditsTracker.finish(token_id, request_id, account_id=None, completed=False) -> None`.
- Produces: `CreditsTracker.complete(token_id, account_id, request_id, log_id, log_generation, payload, model_id, output_resolution) -> None`.
- Produces: `CreditsTracker.process_next() -> bool` for deterministic tests and `close()` for worker cleanup.

- [ ] **Step 1: Write failing key and persistence tests**

Assert cost keys for Nano Banana Pro/2, GPT Image, Gemini native image models, Sora2/Sora2 Pro, Veo31, Kling O3/Kling3, and an unknown model. Assert learned JSON loads, saves after a measured update, and corrupt JSON is ignored.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_credits_tracker.py -k 'cost_key or learned'`

Expected: `core.credits_tracker` does not exist.

- [ ] **Step 3: Implement key derivation and learned table storage**

Map catalog model versions `nano-banana-2` and `nano-banana-3` to distinct public families, unify GPT Image aliases, strip Gemini preview suffixes, and derive video engine/duration/resolution from `VIDEO_MODEL_CATALOG`. Persist finite non-negative values atomically.

- [ ] **Step 4: Write failing measurement tests**

Cover a clean positive delta (`measured` and learned), first-snapshot fallback, balance failure fallback, non-positive delta fallback, unknown-key fallback, queue-full fallback, same-account overlap across different tokens, sequential token rotation on one account, and clear-before-backfill races.

- [ ] **Step 5: Run RED**

Run: `pytest -q tests/test_credits_tracker.py`

Expected: tracker lifecycle and backfill assertions fail.

- [ ] **Step 6: Implement attribution and worker processing**

Use a bounded `queue.Queue`, one daemon thread, per-account active request sets, overlap markers, completion counters, balance snapshots, and a state version. Read the old token `credits_used` only to initialize the account snapshot, call `refresh_credits_for_token_id(token_id, handle_auth=True)`, validate the returned `credits.used`, and only learn a positive delta when all clean conditions remain true. Always backfill a full copied payload with either `measured`, `estimated`, or both credit fields set to `None`.

- [ ] **Step 7: Run GREEN**

Run: `pytest -q tests/test_credits_tracker.py`

Expected: all tracker tests pass without network access.

### Task 3: Request Lifecycle Integration

**Files:**
- Modify: `core/stores.py`
- Modify: `app.py`
- Modify: `api/routes/generation.py`
- Modify: `api/routes/gemini_native.py`
- Modify: `tests/test_credits_tracker.py`
- Modify: `tests/test_gemini_native.py`

**Interfaces:**
- `RequestLogRecord` adds `credits_used: Optional[float]` and `credits_source: Optional[str]`.
- `set_request_credit_context(request, model_id, output_resolution)` records the resolved cost dimensions.
- Middleware submits the last successful attempt payload after it is appended to the log store and only when the final HTTP status is 2xx.
- `/api/v1/generate` uses a response `BackgroundTask` to update the same log ID and submit its eventual successful generation.

- [ ] **Step 1: Write failing integration tests**

Assert binding a token starts tracking, retrying with a new token finishes the old binding, middleware failure finishes without submission, middleware success submits the final successful attempt ID, and Gemini/generation routes capture their resolved output resolution.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_credits_tracker.py tests/test_gemini_native.py`

Expected: missing fields, callback, and tracker lifecycle calls.

- [ ] **Step 3: Wire the tracker**

Instantiate `CreditsTracker` with the existing managers/catalogs/stores. Bind it in `_set_request_token_context`, unbind the previous token before a retry switch, add complete/failed finalization in middleware, and submit only the final 2xx `COMPLETED` attempt. Capture resolved model and output resolution in OpenAI image, chat image/video, API job, and Gemini native paths.

- [ ] **Step 4: Run GREEN**

Run: `pytest -q tests/test_credits_tracker.py tests/test_gemini_native.py tests/test_token_retry_deadline.py`

Expected: all targeted integration and retry tests pass.

### Task 4: Admin Credits Column

**Files:**
- Create: `static/admin_log_credits.js`
- Create: `tests/test_admin_log_credits.js`
- Modify: `static/admin.html`
- Modify: `static/admin.js`
- Modify: `static/admin.css`

**Interfaces:**
- Produces: `formatLogCredits(creditsUsed, creditsSource) -> {text, title, estimated}` for CommonJS and browser use.

- [ ] **Step 1: Write failing browser-helper and structure tests**

Assert measured values render as plain numbers, estimated values as `~number` with `估算值(按历史实测)`, unknown/invalid values as `-`, the table has a `积分` header after `模型`, and all empty/error colspans are nine.

- [ ] **Step 2: Run RED**

Run: `node --test tests/test_admin_log_credits.js`

Expected: helper and table column are missing.

- [ ] **Step 3: Implement and wire the column**

Load the helper before `admin.js`, render a narrow credit cell after the model cell, add a restrained estimated style, update column widths, make prompt text single-line ellipsis, and compress the preview column.

- [ ] **Step 4: Run GREEN**

Run: `node --test tests/test_admin_log_credits.js tests/test_admin_token_filters.js tests/test_admin_ui_state.js tests/test_admin_cookie_import.js`

Expected: all Node tests pass.

### Task 5: Regression Verification and Review

**Files:**
- Review all modified files.

- [ ] **Step 1: Run the full automated suite**

Run: `pytest -q`

Run: `node --test tests/*.js`

Expected: both suites pass.

- [ ] **Step 2: Run syntax and packaging checks**

Run: `python -m compileall -q app.py api core tests`

Run: `docker build -t adobe2api:credits-local .`

Expected: clean compilation and a successful image build.

- [ ] **Step 3: Review against the design**

Verify failed requests never refresh credits, balance errors never affect responses, append-only updates do not duplicate logs/statistics, old records render `-`, and no secrets or full request bodies enter tracker state or learned-cost storage.
