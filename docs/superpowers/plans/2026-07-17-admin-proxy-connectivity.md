# Admin Proxy Connectivity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated administrator test the unsaved proxy input against a fixed Adobe host with deterministic direct/proxy behavior and sanitized structured results.

**Architecture:** Add a typed FastAPI request model and a synchronous admin endpoint that creates an isolated Requests session with environment proxy discovery disabled. Keep the target fixed, validate only supported proxy schemes, translate network exceptions into stable codes, and render those codes next to the existing proxy input without persisting configuration.

**Tech Stack:** Python 3, FastAPI, Pydantic 2, Requests 2.31, pytest, vanilla JavaScript, HTML, CSS

## Global Constraints

- Endpoint: `POST /api/v1/config/test-proxy`.
- Request body: `{ "proxy": "<url or empty>" }`.
- Only `http` and `https` proxy schemes are supported; SOCKS is not installed or accepted.
- Probe target is always `https://firefly.adobe.io/`.
- Timeout is exactly 10 seconds and redirects are disabled.
- The Requests session must set `trust_env = False`.
- Empty proxy input means deterministic direct access, not environment-proxy access.
- Any HTTP response means connectivity succeeded.
- Network failures return HTTP 200 with stable error codes.
- Authentication and invalid input retain non-2xx responses.
- Never return raw exception strings, proxy credentials, full proxy URLs, DNS results, or socket details.
- Loopback and private proxy hosts remain allowed because local proxies are a supported administrator use case.

---

### Task 1: Add a Tested Proxy Probe Endpoint

**Files:**
- Create: `tests/test_admin_proxy.py`
- Modify: `api/schemas.py:21-29`
- Modify: `api/routes/admin.py:1-21`
- Modify: `api/routes/admin.py:418-429`

**Interfaces:**
- Produces: `ProxyTestRequest(proxy: str = "")`
- Produces: `POST /api/v1/config/test-proxy`
- Success response: `{ok, status_code, latency_ms, via}`
- Failure response: `{ok, error, latency_ms, via}`

- [ ] **Step 1: Write the endpoint tests with a fake Session**

Create `tests/test_admin_proxy.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.routes.admin as admin_routes_module
from api.routes.admin import build_admin_router
from api.schemas import ProxyTestRequest


class FakeConfigManager:
    def get(self, key: str, default=None):
        return default

    def get_all(self) -> dict:
        return {}


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class RecordingSession:
    def __init__(self, *, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.trust_env = True
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("test session requires a response or error")
        return self.response

    def close(self) -> None:
        self.closed = True


def make_admin_client(*, authenticated: bool = True) -> TestClient:
    def require_admin_auth(request) -> None:
        if not authenticated:
            raise HTTPException(status_code=401, detail="Unauthorized")

    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=FakeConfigManager(),
            refresh_manager=object(),
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=require_admin_auth,
            is_admin_authenticated=lambda request: authenticated,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def install_session(monkeypatch, session: RecordingSession) -> None:
    monkeypatch.setattr(admin_routes_module.requests, "Session", lambda: session)


def test_proxy_request_schema_defaults_to_direct():
    assert ProxyTestRequest().model_dump() == {"proxy": ""}


def test_direct_probe_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.test:8080")
    session = RecordingSession(response=FakeResponse(401))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": ""},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status_code"] == 401
    assert response.json()["via"] == "direct"
    assert response.json()["latency_ms"] >= 0
    assert session.trust_env is False
    assert session.calls == [
        (
            "https://firefly.adobe.io/",
            {"timeout": 10, "allow_redirects": False, "proxies": None},
        )
    ]
    assert session.closed is True


def test_explicit_proxy_is_used_for_both_schemes(monkeypatch):
    session = RecordingSession(response=FakeResponse(404))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": "http://127.0.0.1:7890"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["via"] == "proxy"
    assert session.calls[0][1]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (requests.exceptions.Timeout("slow upstream"), "timeout"),
        (requests.exceptions.ProxyError("proxy rejected"), "proxy_error"),
        (requests.exceptions.ConnectionError("connection refused"), "connection_error"),
        (requests.exceptions.RequestException("request failed"), "request_error"),
    ],
)
def test_network_errors_return_stable_sanitized_codes(
    monkeypatch,
    error: Exception,
    expected_code: str,
):
    session = RecordingSession(error=error)
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": "https://user:secret@proxy.example:8443"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == expected_code
    assert response.json()["via"] == "proxy"
    assert response.json()["latency_ms"] >= 0
    assert "secret" not in response.text
    assert str(error) not in response.text
    assert session.closed is True


@pytest.mark.parametrize(
    "proxy",
    [
        "proxy.example:8080",
        "http://",
        "http://bad host:8080",
        "http://proxy.example:0",
        "http://proxy.example:70000",
        "ftp://proxy.example:21",
        "socks5://proxy.example:1080",
    ],
)
def test_invalid_proxy_urls_return_400_without_network(
    monkeypatch,
    proxy: str,
):
    session = RecordingSession(response=FakeResponse(200))
    install_session(monkeypatch, session)

    response = make_admin_client().post(
        "/api/v1/config/test-proxy",
        json={"proxy": proxy},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid proxy URL"
    assert session.calls == []


def test_proxy_probe_requires_admin_auth(monkeypatch):
    session = RecordingSession(response=FakeResponse(200))
    install_session(monkeypatch, session)

    response = make_admin_client(authenticated=False).post(
        "/api/v1/config/test-proxy",
        json={"proxy": ""},
    )

    assert response.status_code == 401
    assert session.calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
pytest tests/test_admin_proxy.py -v
```

Expected: collection FAILS because `ProxyTestRequest` and the endpoint do not exist.

- [ ] **Step 3: Add the request schema**

Add after `TokenCreditsBatchRefreshRequest` in `api/schemas.py`:

```python
class ProxyTestRequest(BaseModel):
    proxy: str = ""
```

- [ ] **Step 4: Add imports and validation helpers**

At the top of `api/routes/admin.py`, add `urlparse` and Requests imports so the import block contains:

```python
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException, Request
```

Replace the existing `api.schemas` import block with:

```python
from api.schemas import (
    AdminLoginRequest,
    ConfigUpdateRequest,
    ExportSelectionRequest,
    ProxyTestRequest,
    RefreshCookieBatchImportRequest,
    RefreshCookieImportRequest,
    RefreshProfileEnabledRequest,
    TokenAddRequest,
    TokenBatchAddRequest,
    TokenCreditsBatchRefreshRequest,
)
```

Inside `build_admin_router`, immediately after `get_batch_concurrency`, add:

```python
    def validate_proxy_url(raw_proxy: str) -> str:
        proxy = str(raw_proxy or "").strip()
        if not proxy:
            return ""
        try:
            parsed = urlparse(proxy)
            port = parsed.port
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid proxy URL")

        hostname = str(parsed.hostname or "").strip()
        if parsed.scheme.lower() not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="invalid proxy URL")
        if not hostname or any(char.isspace() for char in hostname):
            raise HTTPException(status_code=400, detail="invalid proxy URL")
        if port is not None and not 1 <= port <= 65535:
            raise HTTPException(status_code=400, detail="invalid proxy URL")
        return proxy

    def proxy_latency_ms(started_at: float) -> int:
        return max(0, round((time.perf_counter() - started_at) * 1000))
```

- [ ] **Step 5: Add the authenticated fixed-target endpoint**

Add immediately before the existing `GET /api/v1/config` route:

```python
    @router.post("/api/v1/config/test-proxy")
    def test_proxy(req: ProxyTestRequest, request: Request):
        require_admin_auth(request)
        proxy = validate_proxy_url(req.proxy)
        via = "proxy" if proxy else "direct"
        proxies = {"http": proxy, "https": proxy} if proxy else None
        started_at = time.perf_counter()
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                "https://firefly.adobe.io/",
                timeout=10,
                allow_redirects=False,
                proxies=proxies,
            )
            return {
                "ok": True,
                "status_code": int(response.status_code),
                "latency_ms": proxy_latency_ms(started_at),
                "via": via,
            }
        except requests.exceptions.Timeout:
            error = "timeout"
        except requests.exceptions.ProxyError:
            error = "proxy_error"
        except requests.exceptions.ConnectionError:
            error = "connection_error"
        except requests.exceptions.RequestException:
            error = "request_error"
        finally:
            session.close()

        return {
            "ok": False,
            "error": error,
            "latency_ms": proxy_latency_ms(started_at),
            "via": via,
        }
```

- [ ] **Step 6: Run endpoint and full backend tests**

Run:

```bash
pytest tests/test_admin_proxy.py -v
pytest -q
```

Expected: proxy tests PASS without real network access and the full suite PASS.

- [ ] **Step 7: Commit the endpoint**

```bash
git add api/schemas.py api/routes/admin.py tests/test_admin_proxy.py
git commit -m "feat: add authenticated proxy connectivity probe"
```

### Task 2: Add the Inline Proxy Test Control

**Files:**
- Modify: `static/admin.html:180-183`
- Modify: `static/admin.html:413`
- Modify: `static/admin.css:192-247`
- Modify: `static/admin.css:1171-1188`
- Modify: `static/admin.js:820-848`
- Modify: `static/admin.js:901-929`

**Interfaces:**
- Consumes: `POST /api/v1/config/test-proxy`
- Produces: `#testProxyBtn` and `#proxyTestResult`
- Produces: stable Chinese copy for `timeout`, `proxy_error`, `connection_error`, and `request_error`

- [ ] **Step 1: Add the input, button, and live result region**

Replace the proxy form group in `static/admin.html` with:

```html
<div class="form-group">
  <label for="confProxy">代理服务器地址 (Proxy URL)</label>
  <div class="proxy-test-row">
    <input type="text" id="confProxy" class="input-text" placeholder="例如：http://127.0.0.1:7890" />
    <button id="testProxyBtn" class="secondary" type="button">测试连通性</button>
  </div>
  <p id="proxyTestResult" class="msg proxy-test-result" role="status" aria-live="polite"></p>
</div>
```

Update the `admin.js` cache key at the end of `static/admin.html` to:

```html
<script src="/static/admin.js?v=20260717-3"></script>
```

Only change the `admin.js` line; retain any preceding script tags unchanged.

- [ ] **Step 2: Add stable responsive styles**

Add after the `.form-group` rules in `static/admin.css`:

```css
.proxy-test-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.proxy-test-row .input-text {
  min-width: 0;
}

.proxy-test-row button {
  white-space: nowrap;
}

.proxy-test-result {
  min-height: 20px;
  margin: 0;
  line-height: 20px;
}

.proxy-test-result.is-success {
  color: var(--good);
}

.proxy-test-result.is-error {
  color: var(--critical);
}
```

Add inside the existing `@media (max-width: 560px)` block:

```css
  .proxy-test-row {
    grid-template-columns: 1fr;
  }

  .proxy-test-row button {
    width: 100%;
  }
```

- [ ] **Step 3: Bind the new elements and stable error messages**

After `const confProxy = document.getElementById("confProxy");` in `static/admin.js`, add:

```javascript
  const testProxyBtn = document.getElementById("testProxyBtn");
  const proxyTestResult = document.getElementById("proxyTestResult");
```

After the config element declarations, add:

```javascript
  const PROXY_TEST_ERROR_MESSAGES = {
    timeout: "连接超时",
    proxy_error: "代理连接失败",
    connection_error: "目标连接失败",
    request_error: "网络请求失败",
  };

  function clearProxyTestResult() {
    if (!proxyTestResult) return;
    proxyTestResult.textContent = "";
    proxyTestResult.classList.remove("is-success", "is-error");
  }

  function showProxyTestResult(text, isError) {
    if (!proxyTestResult) return;
    proxyTestResult.textContent = text;
    proxyTestResult.classList.toggle("is-success", !isError);
    proxyTestResult.classList.toggle("is-error", isError);
  }
```

- [ ] **Step 4: Add the proxy test interaction**

Add before `loadConfig` in `static/admin.js`:

```javascript
  if (confProxy) {
    confProxy.addEventListener("input", clearProxyTestResult);
  }

  if (testProxyBtn) {
    testProxyBtn.addEventListener("click", async () => {
      const originalText = testProxyBtn.textContent;
      testProxyBtn.disabled = true;
      testProxyBtn.textContent = "测试中...";
      clearProxyTestResult();
      try {
        const res = await fetch("/api/v1/config/test-proxy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proxy: String(confProxy?.value || "").trim() }),
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = typeof data.detail === "string" ? data.detail : "代理地址无效";
          throw new Error(detail);
        }

        const latency = Math.max(0, Math.round(Number(data.latency_ms || 0)));
        if (data.ok) {
          const routeText = data.via === "proxy" ? "经代理" : "直连";
          showProxyTestResult(
            `连通（HTTP ${Number(data.status_code)}，${latency}ms，${routeText}）`,
            false,
          );
          return;
        }

        const message = PROXY_TEST_ERROR_MESSAGES[data.error] || "连接失败";
        showProxyTestResult(`${message}（${latency}ms）`, true);
      } catch (err) {
        showProxyTestResult(err.message || "代理测试失败", true);
      } finally {
        testProxyBtn.disabled = false;
        testProxyBtn.textContent = originalText;
      }
    });
  }
```

- [ ] **Step 5: Run automated regression tests**

Run:

```bash
pytest tests/test_admin_proxy.py -v
pytest -q
```

Expected: proxy tests PASS and the full pytest suite PASS.

- [ ] **Step 6: Perform the responsive browser smoke test**

With the application running, verify:

1. An unsaved `http://127.0.0.1:7890` value is sent by the test button.
2. Empty input reports a direct test.
3. HTTP 401/403/404 responses display as connected.
4. Invalid schemes display a concise validation failure.
5. Timeout, proxy, connection, and request errors display stable Chinese copy without raw socket text.
6. Editing the input clears the previous result immediately.
7. The button cannot be clicked twice while a request is pending and always returns to its original label.
8. The input, button, and result line do not overlap at desktop or mobile widths.
9. Saving config remains independent from testing and preserves existing behavior.

- [ ] **Step 7: Commit the frontend control**

```bash
git add static/admin.html static/admin.css static/admin.js
git commit -m "feat: add inline proxy connectivity test"
```
