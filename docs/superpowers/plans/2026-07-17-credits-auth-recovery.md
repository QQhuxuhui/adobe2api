# Credits Authentication Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route credits 401/403 responses through the existing authentication recovery mechanism without duplicate balance requests, while keeping Token availability separate from credits health.

**Architecture:** Introduce a typed `CreditsAuthError`, add an opt-out for the credits side effect inside cookie refresh, and let explicit admin credit refreshes perform one controlled recovery attempt. Keep error persistence at existing callers so every failure writes `credits_error` once, and always reload the single-refresh UI after the request settles.

**Tech Stack:** Python 3, FastAPI, Requests, pytest, vanilla JavaScript

## Global Constraints

- Token `status` controls membership in the generation pool; `credits_error` is a separate health signal.
- Manual Token credits 401/403 marks the Token `invalid`.
- An auto-refresh Token may perform one cookie refresh after credits 401/403.
- A newly refreshed Token that still receives credits 401/403 remains `active` and records `credits_error`.
- A recovery attempt performs exactly two balance calls: one with the old Token and one with the new Token.
- 429, 500, JSON, and network failures never change Token status.
- Existing generation authentication recovery behavior must remain unchanged by default.
- No failure path may recursively call authentication recovery.
- Callers, not `refresh_credits_for_token_id`, own the single `set_credits_error` write on failure.

---

### Task 1: Classify Credits Authentication Responses

**Files:**
- Create: `tests/test_refresh_credits_auth.py`
- Modify: `core/refresh_mgr.py:15-20`
- Modify: `core/refresh_mgr.py:694-721`

**Interfaces:**
- Produces: `CreditsAuthError(message: str, status_code: int)`
- Preserves: `_fetch_credits_balance(access_token, account_id) -> Dict`

- [ ] **Step 1: Write failing response-classification tests**

Create `tests/test_refresh_credits_auth.py`:

```python
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.refresh_mgr as refresh_mgr_module
import core.token_mgr as token_mgr_module
from api.routes.admin import build_admin_router
from core.refresh_mgr import CreditsAuthError, RefreshManager
from core.token_mgr import TokenManager


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def make_refresh_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._requests_proxies = lambda: None
    return manager


@pytest.mark.parametrize("status_code", [401, 403])
def test_fetch_credits_raises_typed_auth_error(monkeypatch, status_code: int):
    manager = make_refresh_manager()
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code),
    )

    with pytest.raises(CreditsAuthError) as caught:
        manager._fetch_credits_balance("token", "account")

    assert caught.value.status_code == status_code
    assert str(caught.value) == f"credits request failed: {status_code}"


@pytest.mark.parametrize("status_code", [429, 500])
def test_fetch_credits_keeps_non_auth_failures_as_runtime_error(
    monkeypatch, status_code: int
):
    manager = make_refresh_manager()
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code),
    )

    with pytest.raises(RuntimeError) as caught:
        manager._fetch_credits_balance("token", "account")

    assert type(caught.value) is RuntimeError
    assert str(caught.value) == f"credits request failed: {status_code}"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: collection FAILS because `CreditsAuthError` is not defined.

- [ ] **Step 3: Add the typed exception**

Add below the path constants in `core/refresh_mgr.py`:

```python
class CreditsAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = int(status_code)
```

- [ ] **Step 4: Raise it only for 401 and 403**

Replace the non-200 branch in `_fetch_credits_balance` with:

```python
        if resp.status_code in {401, 403}:
            raise CreditsAuthError(
                f"credits request failed: {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"credits request failed: {resp.status_code}")
```

- [ ] **Step 5: Run the classification tests**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 6: Commit the exception classification**

```bash
git add core/refresh_mgr.py tests/test_refresh_credits_auth.py
git commit -m "feat: classify credits authentication failures"
```

### Task 2: Add a Side-Effect-Free Cookie Refresh Path

**Files:**
- Modify: `tests/test_refresh_credits_auth.py`
- Modify: `core/refresh_mgr.py:777-853`
- Modify: `core/token_mgr.py:333-386`

**Interfaces:**
- Produces: `RefreshManager.refresh_once(profile_id, *, refresh_credits=True) -> Dict`
- Produces: `TokenManager.handle_auth_failure(value, *, refresh_credits=True) -> Dict`
- Preserves: default `refresh_credits=True` for all existing callers

- [ ] **Step 1: Add failing tests for the new keyword flow**

Append to `tests/test_refresh_credits_auth.py`:

```python
class FakeRefreshTokenStore:
    def __init__(self):
        self.credit_errors: list[tuple[str, str]] = []

    def upsert_auto_refresh_token(self, value: str, **kwargs) -> dict:
        assert value == "new-token"
        assert kwargs["profile_id"] == "profile-1"
        return {"id": "token-1"}

    def set_credits_error(self, token_id: str, message: str) -> None:
        self.credit_errors.append((token_id, message))


def make_refresh_once_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._prepare_refresh = lambda profile_id: {
        "id": profile_id,
        "name": "Profile",
        "url": "https://refresh.test/token",
        "headers": {},
        "form": {},
    }
    manager._requests_proxies = lambda: None
    manager._fetch_account_info = lambda token: {}
    manager._mark_success = lambda profile_id, http_status: None
    return manager


def test_refresh_once_can_skip_credits_side_effect(monkeypatch):
    manager = make_refresh_once_manager()
    credits_calls: list[str] = []
    manager.refresh_credits_for_token_id = lambda token_id: credits_calls.append(token_id)
    token_store = FakeRefreshTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"access_token": "new-token"}),
    )

    result = manager.refresh_once("profile-1", refresh_credits=False)

    assert credits_calls == []
    assert token_store.credit_errors == []
    assert result["credits_error"] == ""


def test_refresh_once_keeps_existing_default_credits_side_effect(monkeypatch):
    manager = make_refresh_once_manager()
    credits_calls: list[str] = []
    manager.refresh_credits_for_token_id = lambda token_id: credits_calls.append(token_id)
    token_store = FakeRefreshTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    monkeypatch.setattr(
        refresh_mgr_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"access_token": "new-token"}),
    )

    manager.refresh_once("profile-1")

    assert credits_calls == ["token-1"]


class FakeCookieRefreshManager:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def refresh_once(self, profile_id: str, *, refresh_credits: bool = True) -> dict:
        self.calls.append((profile_id, refresh_credits))
        return {"status": "ok", "credits_error": ""}


def make_auto_token_manager() -> TokenManager:
    manager = object.__new__(TokenManager)
    manager._lock = threading.Lock()
    manager.tokens = [
        {
            "id": "token-1",
            "value": "old-token",
            "status": "active",
            "fails": 0,
            "error_until": 0,
            "auto_refresh": True,
            "refresh_profile_id": "profile-1",
        }
    ]
    manager.save = lambda: None
    return manager


def test_handle_auth_failure_forwards_refresh_credits_flag(monkeypatch):
    manager = make_auto_token_manager()
    fake_refresh_manager = FakeCookieRefreshManager()
    monkeypatch.setattr(refresh_mgr_module, "refresh_manager", fake_refresh_manager)

    result = manager.handle_auth_failure("old-token", refresh_credits=False)

    assert result["status"] == "refreshed"
    assert fake_refresh_manager.calls == [("profile-1", False)]


def test_manual_token_auth_failure_still_marks_invalid():
    manager = object.__new__(TokenManager)
    manager._lock = threading.Lock()
    manager.tokens = [
        {
            "id": "manual-1",
            "value": "manual-token",
            "status": "active",
            "fails": 0,
            "error_until": 0,
            "auto_refresh": False,
        }
    ]
    manager.save = lambda: None

    result = manager.handle_auth_failure("manual-token", refresh_credits=False)

    assert result["status"] == "invalid"
    assert manager.tokens[0]["status"] == "invalid"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: FAIL with unexpected keyword argument `refresh_credits`.

- [ ] **Step 3: Make `refresh_once` optionally skip the balance request**

Change the signature in `core/refresh_mgr.py` to:

```python
    def refresh_once(self, profile_id: str, *, refresh_credits: bool = True) -> Dict:
```

Change its credits block to:

```python
        credits_error = ""
        token_id = str(token_record.get("id") or "").strip()
        if refresh_credits and token_id:
            try:
                self.refresh_credits_for_token_id(token_id)
            except Exception as exc:
                credits_error = str(exc)
                token_manager.set_credits_error(token_id, credits_error)
```

Leave the existing return object unchanged so `credits_error` is `""` when the side effect is skipped.

- [ ] **Step 4: Forward the keyword through `handle_auth_failure`**

Change the signature in `core/token_mgr.py` to:

```python
    def handle_auth_failure(
        self, value: str, *, refresh_credits: bool = True
    ) -> Dict:
```

Replace the cookie refresh call with:

```python
            refresh_result = refresh_manager.refresh_once(
                linked_profile_id,
                refresh_credits=refresh_credits,
            )
```

- [ ] **Step 5: Run the focused and existing retry tests**

Run:

```bash
pytest tests/test_refresh_credits_auth.py tests/test_token_retry_deadline.py -v
```

Expected: all focused tests PASS and existing generation retry tests remain PASS.

- [ ] **Step 6: Commit the opt-out path**

```bash
git add core/refresh_mgr.py core/token_mgr.py tests/test_refresh_credits_auth.py
git commit -m "refactor: allow auth refresh without duplicate credits call"
```

### Task 3: Recover Explicit Credits Refreshes Once

**Files:**
- Modify: `tests/test_refresh_credits_auth.py`
- Modify: `core/refresh_mgr.py:732-743`

**Interfaces:**
- Produces: `refresh_credits_for_token_id(token_id, handle_auth=False) -> Dict`
- Consumes: `token_manager.handle_auth_failure(value, refresh_credits=False) -> Dict`
- Preserves: no authentication recovery when `handle_auth=False`

- [ ] **Step 1: Add fake Token storage for recovery tests**

Append to `tests/test_refresh_credits_auth.py`:

```python
class FakeCreditsTokenStore:
    def __init__(self, auth_status: str = "refreshed"):
        self.auth_status = auth_status
        self.record = {
            "id": "token-1",
            "value": "old-token",
            "status": "active",
        }
        self.auth_calls: list[tuple[str, bool]] = []
        self.saved_credits: list[tuple[str, dict]] = []

    def get_by_id(self, token_id: str) -> dict | None:
        if token_id != self.record["id"]:
            return None
        return dict(self.record)

    def handle_auth_failure(
        self, value: str, *, refresh_credits: bool = True
    ) -> dict:
        self.auth_calls.append((value, refresh_credits))
        if self.auth_status == "refreshed":
            self.record["value"] = "new-token"
        elif self.auth_status == "invalid":
            self.record["status"] = "invalid"
        return {
            "status": self.auth_status,
            "message": f"auth result: {self.auth_status}",
            "http_status": 401,
        }

    def set_credits(self, token_id: str, credits: dict) -> None:
        self.saved_credits.append((token_id, credits))


def make_credits_recovery_manager() -> RefreshManager:
    manager = object.__new__(RefreshManager)
    manager._extract_account_id = lambda token: f"account-for-{token}"
    return manager


def test_default_credits_refresh_does_not_invoke_auth_recovery(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    manager._fetch_credits_balance = lambda token, account: (_ for _ in ()).throw(
        CreditsAuthError("credits request failed: 401", status_code=401)
    )

    with pytest.raises(CreditsAuthError):
        manager.refresh_credits_for_token_id("token-1")

    assert token_store.auth_calls == []
    assert token_store.record["status"] == "active"


def test_explicit_refresh_uses_old_and_new_token_once_each(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    calls: list[tuple[str, str]] = []

    def fetch(token: str, account_id: str) -> dict:
        calls.append((token, account_id))
        if token == "old-token":
            raise CreditsAuthError("credits request failed: 401", status_code=401)
        return {"total": 100, "used": 25, "available": 75, "updated_at": 1}

    manager._fetch_credits_balance = fetch

    result = manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert calls == [
        ("old-token", "account-for-old-token"),
        ("new-token", "account-for-new-token"),
    ]
    assert token_store.auth_calls == [("old-token", False)]
    assert token_store.saved_credits == [("token-1", result["credits"])]


def test_new_token_auth_error_does_not_recurse_or_invalidate(monkeypatch):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore()
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    calls: list[str] = []

    def fetch(token: str, account_id: str) -> dict:
        calls.append(token)
        raise CreditsAuthError("credits request failed: 401", status_code=401)

    manager._fetch_credits_balance = fetch

    with pytest.raises(CreditsAuthError):
        manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert calls == ["old-token", "new-token"]
    assert token_store.auth_calls == [("old-token", False)]
    assert token_store.record["status"] == "active"
    assert token_store.saved_credits == []


@pytest.mark.parametrize("auth_status", ["invalid", "retry"])
def test_auth_recovery_terminal_results_raise_known_error(
    monkeypatch, auth_status: str
):
    manager = make_credits_recovery_manager()
    token_store = FakeCreditsTokenStore(auth_status=auth_status)
    monkeypatch.setattr(refresh_mgr_module, "token_manager", token_store)
    manager._fetch_credits_balance = lambda token, account: (_ for _ in ()).throw(
        CreditsAuthError("credits request failed: 403", status_code=403)
    )

    with pytest.raises(CreditsAuthError) as caught:
        manager.refresh_credits_for_token_id("token-1", handle_auth=True)

    assert str(caught.value) == f"auth result: {auth_status}"
    assert caught.value.status_code == 403
    assert token_store.auth_calls == [("old-token", False)]
```

- [ ] **Step 2: Run the recovery tests to verify they fail**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: FAIL because `refresh_credits_for_token_id` does not accept `handle_auth` and never calls recovery.

- [ ] **Step 3: Replace `refresh_credits_for_token_id`**

Replace the method in `core/refresh_mgr.py` with:

```python
    def refresh_credits_for_token_id(
        self, token_id: str, handle_auth: bool = False
    ) -> Dict:
        token_info = token_manager.get_by_id(token_id)
        if not token_info:
            raise KeyError("token not found")

        token_value = str(token_info.get("value") or "").strip()
        account_id = self._extract_account_id(token_value)
        try:
            credits = self._fetch_credits_balance(token_value, account_id)
        except CreditsAuthError as exc:
            if not handle_auth:
                raise

            auth_result = token_manager.handle_auth_failure(
                token_value,
                refresh_credits=False,
            )
            auth_status = str(auth_result.get("status") or "").strip().lower()
            if auth_status != "refreshed":
                message = str(auth_result.get("message") or str(exc)).strip()
                raise CreditsAuthError(
                    message,
                    status_code=exc.status_code,
                ) from exc

            refreshed_info = token_manager.get_by_id(token_id)
            if not refreshed_info:
                raise KeyError("token not found")
            refreshed_value = str(refreshed_info.get("value") or "").strip()
            refreshed_account_id = self._extract_account_id(refreshed_value)
            credits = self._fetch_credits_balance(
                refreshed_value,
                refreshed_account_id,
            )

        token_manager.set_credits(token_id, credits)
        return {
            "token_id": token_id,
            "credits": credits,
        }
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: all classification, side-effect, and recovery tests PASS.

- [ ] **Step 5: Commit the recovery logic**

```bash
git add core/refresh_mgr.py tests/test_refresh_credits_auth.py
git commit -m "feat: recover credits auth failures once"
```

### Task 4: Enable Recovery at Admin Call Sites and Refresh the UI

**Files:**
- Modify: `tests/test_refresh_credits_auth.py`
- Modify: `api/routes/admin.py:354-416`
- Modify: `static/admin.html:413`
- Modify: `static/admin.js:580-602`

**Interfaces:**
- Consumes: `refresh_credits_for_token_id(token_id, handle_auth=True)`
- Preserves: single endpoint returns 500 for a handled refresh failure and writes `credits_error` once
- Preserves: batch endpoint returns `ok` or `partial`

- [ ] **Step 1: Add failing admin route tests**

Append to `tests/test_refresh_credits_auth.py`:

```python
class FakeAdminConfig:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


class FakeAdminTokenStore:
    def __init__(self):
        self.errors: list[tuple[str, str]] = []

    def get_by_id(self, token_id: str) -> dict | None:
        return {"id": token_id, "value": "token"} if token_id else None

    def list_active_ids(self) -> list[str]:
        return ["token-1"]

    def set_credits_error(self, token_id: str, message: str) -> None:
        self.errors.append((token_id, message))


class FakeAdminRefreshManager:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []

    def refresh_credits_for_token_id(
        self, token_id: str, handle_auth: bool = False
    ) -> dict:
        self.calls.append((token_id, handle_auth))
        if self.fail:
            raise CreditsAuthError("credits request failed: 401", status_code=401)
        return {"token_id": token_id, "credits": {"available": 10}}


def make_admin_client(
    token_store: FakeAdminTokenStore,
    refresh_manager: FakeAdminRefreshManager,
) -> TestClient:
    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=token_store,
            config_manager=FakeAdminConfig(),
            refresh_manager=refresh_manager,
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=lambda request: None,
            is_admin_authenticated=lambda request: True,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def test_single_admin_refresh_enables_auth_recovery():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager()
    client = make_admin_client(token_store, refresh_manager)

    response = client.post("/api/v1/tokens/token-1/credits/refresh")

    assert response.status_code == 200
    assert refresh_manager.calls == [("token-1", True)]
    assert token_store.errors == []


def test_single_admin_refresh_records_failure_once():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager(fail=True)
    client = make_admin_client(token_store, refresh_manager)

    response = client.post("/api/v1/tokens/token-1/credits/refresh")

    assert response.status_code == 500
    assert refresh_manager.calls == [("token-1", True)]
    assert token_store.errors == [("token-1", "credits request failed: 401")]


def test_batch_admin_refresh_enables_auth_recovery():
    token_store = FakeAdminTokenStore()
    refresh_manager = FakeAdminRefreshManager()
    client = make_admin_client(token_store, refresh_manager)

    response = client.post(
        "/api/v1/tokens/credits/refresh-batch",
        json={"ids": ["token-1"]},
    )

    assert response.status_code == 200
    assert response.json()["refreshed_count"] == 1
    assert refresh_manager.calls == [("token-1", True)]
```

- [ ] **Step 2: Run route tests to verify they fail**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
```

Expected: admin assertions FAIL because both call sites still use the default `handle_auth=False`.

- [ ] **Step 3: Enable recovery at both admin call sites**

In `refresh_token_credits`, replace the manager call with:

```python
            result = refresh_manager.refresh_credits_for_token_id(
                tid,
                handle_auth=True,
            )
```

In the batch endpoint's `refresh_one`, replace the manager call with:

```python
                result = refresh_manager.refresh_credits_for_token_id(
                    tid,
                    handle_auth=True,
                )
                return index, "ok", result
```

- [ ] **Step 4: Make single-refresh UI state reload on success and failure**

Replace `window.refreshTokenCredits` in `static/admin.js` with:

```javascript
  window.refreshTokenCredits = async (id) => {
    showToast("Token 积分刷新中...", false, { duration: 0 });
    try {
      const res = await fetch(`/api/v1/tokens/${id}/credits/refresh`, { method: "POST" });
      if (!res.ok) {
        let detail = "刷新积分失败";
        try {
          const body = await res.json();
          detail = body.detail || JSON.stringify(body);
        } catch (parseError) {
          detail = await res.text();
        }
        alert(detail || "刷新积分失败");
        showToast(`刷新积分失败：${detail || "unknown error"}`, true);
        return;
      }
      showToast("Token 积分刷新成功", false);
    } catch (err) {
      alert("刷新积分失败");
      showToast("Token 积分刷新失败", true);
    } finally {
      await loadTokens();
    }
  };
```

Update the `admin.js` cache key at the end of `static/admin.html` to:

```html
<script src="/static/admin.js?v=20260717-2"></script>
```

Only change the `admin.js` line; retain any preceding script tags unchanged.

- [ ] **Step 5: Run focused and full backend tests**

Run:

```bash
pytest tests/test_refresh_credits_auth.py -v
pytest -q
```

Expected: focused tests PASS and the full pytest suite PASS.

- [ ] **Step 6: Perform the admin smoke test**

With the application running, verify:

1. A manual Token returning credits 401/403 moves to `invalid` after one click.
2. The failed single request still reloads the list and immediately shows the updated status.
3. An auto-refresh Token refreshes its cookie once and performs one balance call with the new Token.
4. If the new Token also receives 401/403, it stays active but shows `credits_error` and enters the broken filter.
5. Batch refresh reports partial failures and reloads the list.
6. A 429/500/network failure records `credits_error` without changing Token status.

- [ ] **Step 7: Commit admin and UI integration**

```bash
git add api/routes/admin.py static/admin.html static/admin.js tests/test_refresh_credits_auth.py
git commit -m "feat: recover credits auth failures from admin refresh"
```
