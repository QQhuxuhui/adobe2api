# Admin Token UX Regression Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four confirmed authentication and asynchronous UI state regressions without changing the approved admin UX.

**Architecture:** Authenticate and manually validate the proxy request inside an async FastAPI dependency so the synchronous network probe remains in the thread pool. Add a small dependency-free browser/CommonJS state helper for filtered selection reconciliation and latest-request gating; keep rendering and network behavior in `admin.js`.

**Tech Stack:** FastAPI, Pydantic v2, vanilla JavaScript, Node built-in test runner, pytest/TestClient.

## Global Constraints

- Proxy validation errors must never include the submitted proxy URL or credentials.
- Selection may persist across pages only while the token remains in the current filtered result.
- Editing proxy input invalidates every older in-flight proxy result.
- Token load failure clears cached tokens, selection, and pagination.
- No new runtime or test dependency.

---

### Task 1: Authenticate Before Sanitized Proxy Validation

**Files:**
- Modify: `api/routes/admin.py`
- Test: `tests/test_admin_proxy.py`

**Interfaces:**
- Consumes: `require_admin_auth(request)` and `ProxyTestRequest.model_validate(payload)`.
- Produces: `parse_proxy_test_request(request: Request) -> ProxyTestRequest`, used through `Depends`.

- [ ] **Step 1: Write failing endpoint tests**

```python
def test_unauthenticated_malformed_proxy_request_returns_401_without_echo():
    response = make_admin_client(authenticated=False).post(
        "/api/v1/config/test-proxy",
        json={"proxy": ["https://user:secret@proxy.example"]},
    )
    assert response.status_code == 401
    assert "secret" not in response.text


def test_authenticated_malformed_proxy_request_is_sanitized():
    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": ["https://user:secret@proxy.example"]},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid proxy request"}
    assert "secret" not in response.text
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_admin_proxy.py -k 'malformed_proxy'`

Expected: unauthenticated request returns 422 and both responses expose the submitted input.

- [ ] **Step 3: Add authenticated sanitized parsing dependency**

```python
async def parse_proxy_test_request(request: Request) -> ProxyTestRequest:
    require_admin_auth(request)
    try:
        payload = await request.json()
        return ProxyTestRequest.model_validate(payload)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="invalid proxy request")

@router.post("/api/v1/config/test-proxy")
def test_proxy(req: ProxyTestRequest = Depends(parse_proxy_test_request)):
    ...
```

- [ ] **Step 4: Run GREEN**

Run: `pytest -q tests/test_admin_proxy.py`

Expected: all proxy tests pass and no response includes proxy credentials.

### Task 2: Reconcile Filtered Selection and Clear Failed Loads

**Files:**
- Create: `static/admin_ui_state.js`
- Create: `tests/test_admin_ui_state.js`
- Modify: `static/admin.js`
- Modify: `static/admin.html`

**Interfaces:**
- Consumes: arrays/iterables of selected IDs and filtered token records.
- Produces: `retainSelectedTokenIds(selectedIds, tokens) -> string[]`.

- [ ] **Step 1: Write failing helper and wiring tests**

```javascript
test("selection retains only IDs in the complete filtered result", () => {
  assert.deepEqual(
    retainSelectedTokenIds(new Set(["visible", "hidden"]), [{ id: "visible" }]),
    ["visible"],
  );
});
```

Also assert that `admin.js` reconciles against `filteredTokens`, and its `loadTokens` catch branch clears `latestTokens`, `tokenSelectedIds`, and `tokenCurrentPage` before rendering the failure state.

- [ ] **Step 2: Run RED**

Run: `node --test tests/test_admin_ui_state.js`

Expected: missing `static/admin_ui_state.js` or missing exports.

- [ ] **Step 3: Implement minimal state helper and wire it**

```javascript
function retainSelectedTokenIds(selectedIds, tokens) {
  const allowed = new Set((Array.isArray(tokens) ? tokens : [])
    .map((token) => String(token?.id || "").trim())
    .filter(Boolean));
  return Array.from(selectedIds || [], (id) => String(id || "").trim())
    .filter((id) => id && allowed.has(id));
}
```

In `renderTable`, calculate `filteredTokens` first and delete every selected ID not returned by this helper. In the `loadTokens` catch branch, assign `latestTokens = []`, clear selection, and reset the page before rendering the error.

- [ ] **Step 4: Run GREEN**

Run: `node --test tests/test_admin_ui_state.js tests/test_admin_token_filters.js`

Expected: all Node tests pass.

### Task 3: Discard Stale Proxy Test Results

**Files:**
- Modify: `static/admin_ui_state.js`
- Modify: `tests/test_admin_ui_state.js`
- Modify: `static/admin.js`

**Interfaces:**
- Produces: `createLatestRequestGate() -> {begin, invalidate, isCurrent}`.

- [ ] **Step 1: Write the failing request-gate test**

```javascript
test("invalidating a request gate rejects older completions", () => {
  const gate = createLatestRequestGate();
  const oldRequest = gate.begin();
  gate.invalidate();
  assert.equal(gate.isCurrent(oldRequest), false);
});
```

- [ ] **Step 2: Run RED**

Run: `node --test tests/test_admin_ui_state.js`

Expected: `createLatestRequestGate` is missing.

- [ ] **Step 3: Implement and wire the gate**

```javascript
function createLatestRequestGate() {
  let version = 0;
  return {
    begin() { version += 1; return version; },
    invalidate() { version += 1; },
    isCurrent(requestVersion) { return requestVersion === version; },
  };
}
```

Invalidate the gate on proxy input, capture `requestVersion = gate.begin()` on click, and check `isCurrent(requestVersion)` before every success/error render. The button remains disabled while that single request is pending, so its `finally` block always restores the button even if the result became stale.

- [ ] **Step 4: Run GREEN**

Run: `node --test tests/test_admin_ui_state.js tests/test_admin_token_filters.js`

Expected: all Node tests pass.

### Task 4: Gate Concurrent Token Loads and Reject HTTP Failures

**Files:**
- Modify: `static/admin_ui_state.js`
- Modify: `tests/test_admin_ui_state.js`
- Modify: `static/admin.js`

**Interfaces:**
- Produces: `runLatestRequest(gate, operation, handlers) -> Promise<{status, value?, error?}>`.
- Produces: `fetchTokenList(fetchImpl) -> Promise<{tokens, summary}>`.

- [ ] **Step 1: Write failing behavioral tests**

```javascript
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test("only the latest request can commit", async () => {
  const gate = createLatestRequestGate();
  const older = deferred();
  const newer = deferred();
  const commits = [];
  const oldRun = runLatestRequest(gate, () => older.promise, { onSuccess: (v) => commits.push(v) });
  const newRun = runLatestRequest(gate, () => newer.promise, { onSuccess: (v) => commits.push(v) });
  newer.resolve("new");
  older.resolve("old");
  assert.equal((await newRun).status, "success");
  assert.equal((await oldRun).status, "stale");
  assert.deepEqual(commits, ["new"]);
});

test("token list rejects non-ok JSON responses", async () => {
  await assert.rejects(() => fetchTokenList(async () => ({
    ok: false,
    status: 500,
    json: async () => ({ detail: "failed" }),
  })));
});
```

- [ ] **Step 2: Run RED**

Run: `node --test tests/test_admin_ui_state.js`

Expected: `runLatestRequest` and `fetchTokenList` are missing.

- [ ] **Step 3: Implement and wire latest-only loading**

```javascript
async function runLatestRequest(gate, operation, handlers = {}) {
  const requestVersion = gate.begin();
  try {
    const value = await operation();
    if (!gate.isCurrent(requestVersion)) return { status: "stale" };
    handlers.onSuccess?.(value);
    return { status: "success", value };
  } catch (error) {
    if (!gate.isCurrent(requestVersion)) return { status: "stale" };
    handlers.onFailure?.(error);
    return { status: "failure", error };
  }
}
```

`fetchTokenList` must throw before parsing a response whose `ok` is false. `loadTokens` uses a dedicated gate and returns the request result. The manual refresh button shows success only for `success`, failure only for `failure`, and does not claim completion for `stale`.

- [ ] **Step 4: Run GREEN**

Run: `node --test tests/test_admin_ui_state.js`

Expected: reversed completion and HTTP failure tests pass.

### Task 5: Invalidate Proxy Results on Programmatic Value Changes

**Files:**
- Modify: `static/admin_ui_state.js`
- Modify: `tests/test_admin_ui_state.js`
- Modify: `static/admin.js`

**Interfaces:**
- Produces: `updateInputValue(input, nextValue, onChange) -> boolean`.

- [ ] **Step 1: Write the failing helper test**

```javascript
test("programmatic input changes invalidate dependent state", () => {
  const input = { value: "old" };
  let changes = 0;
  assert.equal(updateInputValue(input, "new", () => { changes += 1; }), true);
  assert.equal(updateInputValue(input, "new", () => { changes += 1; }), false);
  assert.equal(changes, 1);
});
```

- [ ] **Step 2: Run RED**

Run: `node --test tests/test_admin_ui_state.js`

Expected: `updateInputValue` is missing.

- [ ] **Step 3: Implement and wire the helper**

```javascript
function updateInputValue(input, nextValue, onChange) {
  if (!input) return false;
  const value = String(nextValue ?? "");
  if (input.value === value) return false;
  input.value = value;
  if (typeof onChange === "function") onChange();
  return true;
}
```

Use it for `confProxy` inside `loadConfig`; its callback invalidates `proxyTestGate` and clears the displayed result.

- [ ] **Step 4: Run GREEN**

Run: `node --test tests/test_admin_ui_state.js`

Expected: helper and controller wiring tests pass.

### Task 6: Full Verification and Review

**Files:**
- Verify: all files above

- [ ] **Step 1: Run all automated checks**

```bash
pytest -q
node --test tests/test_admin_token_filters.js tests/test_admin_ui_state.js
node --check static/admin.js
git diff --check
```

Expected: every command exits 0 with no warnings.

- [ ] **Step 2: Re-run browser reproductions**

Verify that a load failure cannot resurrect old rows, hidden filtered IDs are absent from export payloads, and editing a proxy during a pending request leaves the result blank.

- [ ] **Step 3: Request independent code review**

Review only the scoped fix files against `docs/superpowers/specs/2026-07-16-admin-token-ux-design.md` and resolve every Critical or Important issue before completion.
