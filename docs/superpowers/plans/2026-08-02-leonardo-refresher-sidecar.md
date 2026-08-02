# Leonardo Refresher Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent Playwright sidecar that renews Leonardo Cognito ID tokens and safely upserts them into the existing `type=leonardo` token pool.

**Architecture:** adobe2api exposes one shared-key-protected machine endpoint backed by an atomic account-scoped upsert. A separate `leonardo_refresher` package owns configuration, refresh scheduling, health state, the Playwright persistent browser, and the push client; Docker Compose enables it through an optional `leonardo` profile with an isolated browser-profile volume.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, requests 2.31.0, pytest, Playwright Python 1.61.0, Chromium, Xvfb, x11vnc, noVNC, Docker Compose.

## Global Constraints

- One sidecar manages exactly one Leonardo account.
- Browser profile persists in the named volume `leo-profile:/profile` and is never mounted into adobe2api.
- noVNC binds to `127.0.0.1` and requires `NOVNC_PASSWORD`; remote access uses an SSH tunnel.
- Use only `LEONARDO_PROXY`; do not set container-wide `HTTP_PROXY` or `HTTPS_PROXY`.
- `REFRESH_INTERVAL_SECONDS=3000`, `SAFETY_MARGIN_SECONDS=600`, and `MIN_INTERVAL_SECONDS=60` by default.
- `LEONARDO_TOKEN_MIN_TTL_SECONDS=600` must match the sidecar safety margin.
- A token with less than the safety margin is never pushed, and an older `exp` never replaces a newer stored token.
- `LEONARDO_REFRESH_KEY` is required only when the sidecar is enabled; an unset key disables the endpoint without disabling adobe2api.
- Production code follows failing-test-first red-green-refactor cycles.

---

### Task 1: Atomic Leonardo account upsert

**Files:**
- Modify: `core/token_mgr.py`
- Test: `tests/test_token_mgr.py`

**Interfaces:**
- Consumes: normalized Leonardo ID token, Cognito `sub`, and optional account label.
- Produces: `TokenManager.upsert_leonardo_token(value: str, account_id: str, label: Optional[str] = None) -> Dict`, returning `{"status": "created|updated|noop", "token": record}` where `record` is a detached copy of the stored token dictionary.

- [ ] **Step 1: Write failing tests for create, update, deduplication, idempotency, and exp regression**

Add tests using the existing `_jwt()` helper. Build tokens with the same `sub` and controlled `exp` values, then assert:

```python
def test_upsert_leonardo_token_creates_typed_active_record(fresh_tm):
    token = _jwt({"sub": "leo-1", "exp": 2000})
    result = fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")
    assert result["status"] == "created"
    assert result["token"]["type"] == "leonardo"
    assert result["token"]["source"] == "leonardo_refresher"
    assert result["token"]["refresh_profile_name"] == "Primary"
    assert result["token"]["status"] == "active"


def test_upsert_leonardo_token_updates_and_resets_status(fresh_tm):
    old = _jwt({"sub": "leo-1", "exp": 2000})
    new = _jwt({"sub": "leo-1", "exp": 3000})
    item = fresh_tm.add(old, meta={"type": "leonardo", "status": "invalid", "fails": 3})
    result = fresh_tm.upsert_leonardo_token(new, "leo-1", "Primary")
    assert result["status"] == "updated"
    assert result["token"]["id"] == item["id"]
    assert result["token"]["value"] == new
    assert result["token"]["status"] == "active"
    assert result["token"]["fails"] == 0


def test_upsert_leonardo_token_keeps_only_newest_account_record(fresh_tm):
    fresh_tm.add(_jwt({"sub": "leo-1", "exp": 2000}), meta={"type": "leonardo"})
    newest = fresh_tm.add(_jwt({"sub": "leo-1", "exp": 3000}), meta={"type": "leonardo"})
    fresh_tm.add(_jwt({"sub": "adobe-1", "exp": 4000}), meta={"type": "adobe"})
    result = fresh_tm.upsert_leonardo_token(
        _jwt({"sub": "leo-1", "exp": 2500}), "leo-1", "Primary"
    )
    matching = [t for t in fresh_tm.tokens if t.get("account_id") == "leo-1"]
    assert result["status"] == "noop"
    assert result["token"]["id"] == newest["id"]
    assert len(matching) == 1


def test_upsert_leonardo_token_is_noop_for_identical_active_record(fresh_tm):
    token = _jwt({"sub": "leo-1", "exp": 3000})
    fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")
    result = fresh_tm.upsert_leonardo_token(token, "leo-1", "Primary")
    assert result["status"] == "noop"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/test_token_mgr.py -q`

Expected: new tests fail with `AttributeError: 'TokenManager' object has no attribute 'upsert_leonardo_token'`.

- [ ] **Step 3: Implement the minimal locked upsert**

Inside one `self._lock` section: strip `Bearer `, validate non-empty `account_id`, collect only `type == "leonardo"` records with matching stored/decoded account ID, retain the highest decoded `exp`, remove duplicates, reject an older incoming `exp`, and save exactly once when state changed. New and accepted records set:

```python
record = {
    "id": target["id"] if target is not None else uuid.uuid4().hex[:8],
    "value": value,
    "status": "active",
    "fails": 0,
    "added_at": target.get("added_at", now_ts) if target is not None else now_ts,
    "updated_at": now_ts,
    "error_until": 0,
    "type": "leonardo",
    "source": "leonardo_refresher",
    "account_id": account_id,
    "refresh_profile_name": label.strip() or account_id,
    "refresh_profile_email": "",
}
```

Return a wrapper with the operation status and a copy of the retained record; never expose the internal mutable record.

- [ ] **Step 4: Run focused and token-manager regression tests**

Run: `pytest tests/test_token_mgr.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add core/token_mgr.py tests/test_token_mgr.py
git commit -m "feat(leonardo): add atomic refresher token upsert"
```

---

### Task 2: Shared-key Leonardo refresh endpoint

**Files:**
- Create: `api/routes/leonardo_tokens.py`
- Modify: `api/schemas.py`
- Modify: `app.py`
- Create: `tests/test_leonardo_token_endpoint.py`

**Interfaces:**
- Consumes: `POST /api/v1/tokens/leonardo`, JSON `{"token": str, "label": str | null}`, header `X-Leonardo-Refresh-Key`.
- Produces: sanitized JSON `{"status", "token_id", "account_id", "expires_at"}` and status codes 200/400/401/503.

- [ ] **Step 1: Write endpoint tests before the router exists**

Create a real `FastAPI` + `TestClient` fixture with a fresh `TokenManager`. Generate JWT-shaped test values with exact claims:

```python
LEONARDO_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu"
LEONARDO_AUDIENCE = "29lhcpsoi9crda0du1s0ampft3"


def leonardo_token(*, sub="leo-1", exp):
    return _jwt({
        "iss": LEONARDO_ISSUER,
        "aud": LEONARDO_AUDIENCE,
        "token_use": "id",
        "sub": sub,
        "exp": exp,
    })
```

Cover these behaviors independently:

```python
def test_endpoint_is_disabled_without_configured_key(client, monkeypatch):
    monkeypatch.delenv("LEONARDO_REFRESH_KEY", raising=False)
    response = client.post("/api/v1/tokens/leonardo", json={"token": "x.y.z"})
    assert response.status_code == 503


def test_endpoint_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setenv("LEONARDO_REFRESH_KEY", "correct-key")
    response = client.post(
        "/api/v1/tokens/leonardo",
        headers={"X-Leonardo-Refresh-Key": "wrong-key"},
        json={"token": "x.y.z"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("claim,value", [
    ("iss", "https://example.invalid/pool"),
    ("aud", "wrong-client"),
    ("token_use", "access"),
    ("sub", ""),
])
def test_endpoint_rejects_wrong_claim(client, authorized_headers, claim, value):
    payload = valid_payload(exp=int(time.time()) + 3600)
    payload[claim] = value
    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": _jwt(payload)},
    )
    assert response.status_code == 400


def test_endpoint_rejects_token_below_minimum_ttl(client, authorized_headers):
    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": leonardo_token(exp=int(time.time()) + 599)},
    )
    assert response.status_code == 400


def test_endpoint_upserts_without_returning_raw_token(client, authorized_headers):
    token = leonardo_token(exp=int(time.time()) + 3600)
    response = client.post(
        "/api/v1/tokens/leonardo",
        headers=authorized_headers,
        json={"token": token, "label": "Primary"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["account_id"] == "leo-1"
    assert token not in response.text
```

- [ ] **Step 2: Run the endpoint tests and verify RED**

Run: `pytest tests/test_leonardo_token_endpoint.py -q`

Expected: collection fails because `api.routes.leonardo_tokens` does not exist.

- [ ] **Step 3: Implement schema, claim validation, authentication, and router**

Define `LeonardoTokenUpsertRequest` in `api/schemas.py`. In `api/routes/leonardo_tokens.py`, define:

```python
DEFAULT_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu"
DEFAULT_AUDIENCE = "29lhcpsoi9crda0du1s0ampft3"


def validate_leonardo_id_token(token: str, *, now: int) -> dict:
    payload = decode_jwt_payload(token)
    min_ttl = max(1, int(os.getenv("LEONARDO_TOKEN_MIN_TTL_SECONDS", "600")))
    issuer = os.getenv("LEONARDO_COGNITO_ISSUER", DEFAULT_ISSUER).strip()
    audience = os.getenv("LEONARDO_COGNITO_AUDIENCE", DEFAULT_AUDIENCE).strip()
    if (
        payload.get("iss") != issuer
        or payload.get("aud") != audience
        or payload.get("token_use") != "id"
        or not str(payload.get("sub") or "").strip()
        or not isinstance(payload.get("exp"), (int, float))
        or int(payload["exp"]) - now < min_ttl
    ):
        raise ValueError("invalid Leonardo ID token")
    return payload


def build_leonardo_token_router(*, token_manager) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/tokens/leonardo")
    def upsert_token(req: LeonardoTokenUpsertRequest, request: Request):
        required = os.getenv("LEONARDO_REFRESH_KEY", "").strip()
        if not required:
            raise HTTPException(status_code=503, detail="Leonardo refresher disabled")
        provided = request.headers.get("X-Leonardo-Refresh-Key", "")
        if not hmac.compare_digest(provided, required):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            claims = validate_leonardo_id_token(req.token, now=int(time.time()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Leonardo token")
        result = token_manager.upsert_leonardo_token(
            req.token, str(claims["sub"]), req.label
        )
        item = result["token"]
        return {
            "status": result["status"],
            "token_id": item["id"],
            "account_id": item["account_id"],
            "expires_at": token_exp(item["value"]),
        }

    return router
```

Read environment settings per request so tests and runtime configuration are deterministic. Compare non-empty secrets with `hmac.compare_digest`; return 503 before reading the request key when the endpoint is disabled, and 401 for a missing/wrong request key. Convert validation failures into a generic 400 without echoing token contents.

Wire `build_leonardo_token_router(token_manager=token_manager)` into `app.py` as a separate router from the admin-session routes.

- [ ] **Step 4: Run endpoint, service, and token tests**

Run: `pytest tests/test_leonardo_token_endpoint.py tests/test_token_mgr.py tests/test_service.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add api/routes/leonardo_tokens.py api/schemas.py app.py tests/test_leonardo_token_endpoint.py
git commit -m "feat(leonardo): add scoped refresher token endpoint"
```

---

### Task 3: Leonardo-only proxy routing

**Files:**
- Modify: `core/leonardo_client.py`
- Modify: `tests/test_leonardo_client.py`

**Interfaces:**
- Consumes: optional `LEONARDO_PROXY` environment variable.
- Produces: explicit requests proxy mapping only for Leonardo GraphQL requests.

- [ ] **Step 1: Write failing proxy isolation tests**

Patch `requests.post`, set both global proxy variables to an unwanted value, and verify only `LEONARDO_PROXY` is used:

```python
def test_http_gql_uses_only_leonardo_proxy(monkeypatch):
    captured = {}
    monkeypatch.setenv("HTTP_PROXY", "http://wrong-global:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong-global:8080")
    monkeypatch.setenv("LEONARDO_PROXY", "http://leo-proxy:10809")

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return FakeOkResponse({"data": {"user_details": []}})

    monkeypatch.setattr(lc.requests, "post", fake_post)
    LeonardoClient().get_credits("token")
    assert captured["proxies"] == {
        "http": "http://leo-proxy:10809",
        "https": "http://leo-proxy:10809",
    }


def test_http_gql_disables_environment_proxy_when_leonardo_proxy_is_empty(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://wrong-global:8080")
    monkeypatch.delenv("LEONARDO_PROXY", raising=False)
    # Assert the HTTP path uses a Session with trust_env=False and no explicit proxy.
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/test_leonardo_client.py -q`

Expected: the proxy assertion sees the old `HTTP_PROXY/HTTPS_PROXY` values.

- [ ] **Step 3: Route Leonardo requests through a dedicated Session**

Use a short-lived `requests.Session()` with `trust_env=False`. Read only `LEONARDO_PROXY`, construct both `http` and `https` entries when present, preserve existing retry behavior, and close the session in `finally`.

- [ ] **Step 4: Run Leonardo client regressions**

Run: `pytest tests/test_leonardo_client.py tests/test_leonardo_generation.py tests/test_leonardo_route.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "fix(leonardo): isolate GraphQL proxy configuration"
```

---

### Task 4: Testable refresher core and health state

**Files:**
- Create: `leonardo_refresher/__init__.py`
- Create: `leonardo_refresher/config.py`
- Create: `leonardo_refresher/service.py`
- Create: `leonardo_refresher/health.py`
- Create: `tests/test_leonardo_refresher.py`

**Interfaces:**
- Produces: `RefresherConfig.from_env()`, `calculate_next_delay()`, `RuntimeState.snapshot()`, `RefresherService.run_once()`, and `start_health_server()`.
- Consumes: injected session source with `fetch_token() -> str` and token sink with `push(token, label) -> dict`.

- [ ] **Step 1: Write failing configuration and scheduling tests**

```python
def test_config_requires_secret_proxy_and_password(monkeypatch):
    monkeypatch.delenv("LEONARDO_REFRESH_KEY", raising=False)
    with pytest.raises(ValueError, match="LEONARDO_REFRESH_KEY"):
        RefresherConfig.from_env()


@pytest.mark.parametrize(("ttl", "expected"), [
    (7200, 3000),
    (3600, 3000),
    (601, 60),
    (300, 60),
])
def test_calculate_next_delay_respects_expiry_margin(ttl, expected):
    assert calculate_next_delay(
        exp=10000 + ttl,
        now=10000,
        min_interval=60,
        refresh_interval=3000,
        safety_margin=600,
    ) == expected
```

Also test invalid numeric relationships and defaults.

- [ ] **Step 2: Run scheduling tests and verify RED**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: collection fails because `leonardo_refresher` does not exist.

- [ ] **Step 3: Implement configuration, token decoding, and state transitions**

`RefresherConfig` validates non-empty base URL/key/proxy/noVNC password, positive intervals, `safety_margin > min_interval`, and `refresh_interval > min_interval`. `calculate_next_delay()` implements:

```python
max(min_interval, min(refresh_interval, exp - now - safety_margin))
```

`RuntimeState` stores a lock-protected snapshot with `state`, `session_state`, `last_success_at`, `current_token_exp`, `consecutive_failures`, and `last_error_kind`.

- [ ] **Step 4: Write failing service-state tests**

Use small fakes that implement the real adapter interfaces. Verify observable state and push behavior, not fake call mechanics:

```python
def test_run_once_pushes_fresh_token_and_becomes_healthy(service_factory):
    service, state, sink = service_factory(token=valid_token(exp=13600), now=10000)
    delay = service.run_once()
    assert sink.received_tokens == [valid_token(exp=13600)]
    assert state.snapshot()["state"] == "healthy"
    assert state.snapshot()["session_state"] == "authenticated"
    assert delay == 3000


def test_run_once_does_not_push_low_ttl_token(service_factory):
    service, state, sink = service_factory(token=valid_token(exp=10599), now=10000)
    assert service.run_once() == 60
    assert sink.received_tokens == []
    assert state.snapshot()["state"] == "refresh_retrying"


def test_run_once_marks_explicit_null_session_as_login_required(service_factory):
    service, state, sink = service_factory(source_error=LoginRequiredError())
    assert service.run_once() == 60
    assert sink.received_tokens == []
    assert state.snapshot()["state"] == "login_required"
    assert state.snapshot()["session_state"] == "login_required"


def test_run_once_keeps_session_unknown_on_proxy_failure(service_factory):
    error = RefreshFetchError("proxy")
    service, state, sink = service_factory(source_error=error)
    assert service.run_once() == 60
    assert sink.received_tokens == []
    assert state.snapshot()["state"] == "refresh_retrying"
    assert state.snapshot()["session_state"] == "unknown"
    assert state.snapshot()["last_error_kind"] == "proxy"


def test_run_once_marks_push_failure_without_declaring_logout(service_factory):
    service, state, sink = service_factory(
        token=valid_token(exp=13600), now=10000, sink_error=TokenPushError("http_503")
    )
    assert service.run_once() == 60
    assert state.snapshot()["state"] == "push_failed"
    assert state.snapshot()["session_state"] == "authenticated"
```

- [ ] **Step 5: Run state tests and verify RED**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: tests fail because `RefresherService.run_once()` is not implemented.

- [ ] **Step 6: Implement the minimal refresh state machine and health server**

Use explicit `LoginRequiredError` and `RefreshFetchError(kind)` exceptions from the source adapter. `run_once()` catches them separately, never pushes low-TTL/invalid tokens, classifies sink failures as `push_failed`, and clears failures only after a successful push. `start_health_server(state, host="0.0.0.0", port=8080)` serves only `/healthz`, returns 200 JSON snapshots, and returns a closable server/thread handle for production shutdown.

- [ ] **Step 7: Run refresher core tests**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit the task**

```bash
git add leonardo_refresher/__init__.py leonardo_refresher/config.py leonardo_refresher/service.py leonardo_refresher/health.py tests/test_leonardo_refresher.py
git commit -m "feat(leonardo): add refresher service state machine"
```

---

### Task 5: Playwright source, push client, and process runtime

**Files:**
- Create: `leonardo_refresher/adapters.py`
- Create: `leonardo_refresher/__main__.py`
- Modify: `tests/test_leonardo_refresher.py`

**Interfaces:**
- Produces: `Adobe2ApiTokenSink`, `PlaywrightSessionSource`, and the `python -m leonardo_refresher` process entrypoint.
- Consumes: persistent profile `/profile`, `LEONARDO_PROXY`, and the core service interfaces from Task 4.

- [ ] **Step 1: Write failing push-client tests**

Use a recording `requests.Session` factory and verify real request construction:

```python
def test_token_sink_ignores_environment_proxy_and_sends_scoped_key():
    session = RecordingSession(json_data={"status": "created"})
    sink = Adobe2ApiTokenSink(
        base_url="http://adobe2api:6001",
        refresh_key="secret",
        session_factory=lambda: session,
    )
    sink.push("jwt", "Primary")
    assert session.trust_env is False
    assert session.calls == [{
        "url": "http://adobe2api:6001/api/v1/tokens/leonardo",
        "headers": {"X-Leonardo-Refresh-Key": "secret"},
        "json": {"token": "jwt", "label": "Primary"},
        "timeout": 15,
    }]
```

Test that non-2xx and transport errors raise a sanitized `TokenPushError` without embedding the token/key.

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: import fails because `leonardo_refresher.adapters` does not exist.

- [ ] **Step 3: Implement the push client**

Create one private Session with `trust_env=False`, send the custom key header, use a 15-second timeout, call `raise_for_status()`, return response JSON, and expose `close()`.

- [ ] **Step 4: Write failing browser-source tests around an injected Playwright factory**

Verify launch options contain `headless=False`, `/profile`, and `proxy={"server": config.proxy}`. Verify `fetch_token()` evaluates a same-origin `fetch('/api/auth/get-session', {credentials: 'include', cache: 'no-store'})`, returns `session.accessToken`, maps a null session/HTML login page to `LoginRequiredError`, and maps 403 to `RefreshFetchError("geo_embargo")`.

- [ ] **Step 5: Run browser-source tests and verify RED**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: tests fail because `PlaywrightSessionSource` is missing.

- [ ] **Step 6: Implement the persistent browser adapter and runtime**

Launch a persistent headful Chromium context with the configured profile and explicit proxy. Keep a controller page on `https://app.leonardo.ai/` for session fetches and a separate visible page for manual login; fetch response status/content in page JavaScript and parse JSON in Python. The process entrypoint starts health, source, sink, and `RefresherService`, handles SIGTERM/SIGINT with an event, runs immediately before sleeping, and closes browser/session/health resources in `finally`.

- [ ] **Step 7: Run all refresher tests**

Run: `pytest tests/test_leonardo_refresher.py -q`

Expected: all tests pass without importing or launching a real browser.

- [ ] **Step 8: Commit the task**

```bash
git add leonardo_refresher/adapters.py leonardo_refresher/__main__.py tests/test_leonardo_refresher.py
git commit -m "feat(leonardo): add persistent browser refresher runtime"
```

---

### Task 6: noVNC image, Compose profile, and deployment guide

**Files:**
- Create: `leonardo_refresher/Dockerfile`
- Create: `leonardo_refresher/requirements.txt`
- Create: `leonardo_refresher/entrypoint.sh`
- Modify: `docker-compose.yml`
- Modify: `docs/leonardo_integration.md`
- Create: `tests/test_leonardo_refresher_deployment.py`

**Interfaces:**
- Produces: optional `docker compose --profile leonardo up -d` deployment, localhost noVNC port 6080, health port 8080, and named volume `leo-profile`.

- [ ] **Step 1: Write a failing Compose contract test**

Run `docker compose --profile leonardo config --format json` through `subprocess`; skip only when Docker Compose is unavailable. Assert the rendered model contains the refresher profile, `127.0.0.1:6080:6080`, `leo-profile:/profile`, the shared-key environment on both services, `shm_size`, and no `HTTP_PROXY`/`HTTPS_PROXY` on either service.

- [ ] **Step 2: Run the deployment test and verify RED**

Run: `pytest tests/test_leonardo_refresher_deployment.py -q`

Expected: fail because `leonardo-refresher` is absent from Compose.

- [ ] **Step 3: Add the pinned image and secure process entrypoint**

Use `mcr.microsoft.com/playwright/python:v1.61.0-noble`, pin `playwright==1.61.0` and `requests==2.31.0`, and install `xvfb`, `x11vnc`, `novnc`, and `websockify`. `entrypoint.sh` must reject an empty `NOVNC_PASSWORD`, create a mode-600 x11vnc password file without printing the secret, start Xvfb/x11vnc/noVNC, then run `python -m leonardo_refresher`; traps terminate every child process.

- [ ] **Step 4: Add the optional Compose profile**

Keep existing default startup unchanged by setting `profiles: ["leonardo"]` on the sidecar. Add the shared key/proxy/min-TTL variables to adobe2api, the isolated named volume, `shm_size: 1gb`, localhost-only noVNC mapping, and a healthcheck against `http://127.0.0.1:8080/healthz`.

- [ ] **Step 5: Document exact deployment and recovery commands**

In `docs/leonardo_integration.md`, document secret generation, required `.env` variables, `docker compose --profile leonardo up -d --build`, SSH tunneling for port 6080, first manual login, `/healthz` states, profile backup sensitivity, and login recovery. Do not include real credentials or proxy endpoints.

- [ ] **Step 6: Run deployment checks**

Run:

```bash
pytest tests/test_leonardo_refresher_deployment.py -q
bash -n leonardo_refresher/entrypoint.sh
LEONARDO_REFRESH_KEY=test-key LEONARDO_PROXY=http://proxy:10809 NOVNC_PASSWORD=test-pass docker compose --profile leonardo config --quiet
```

Expected: all commands exit 0.

- [ ] **Step 7: Build the sidecar image**

Run: `docker compose --profile leonardo build leonardo-refresher`

Expected: image builds with matching Playwright 1.61.0 browser/runtime versions.

- [ ] **Step 8: Commit the task**

```bash
git add leonardo_refresher/Dockerfile leonardo_refresher/requirements.txt leonardo_refresher/entrypoint.sh docker-compose.yml docs/leonardo_integration.md tests/test_leonardo_refresher_deployment.py
git commit -m "feat(leonardo): deploy refresher sidecar with noVNC"
```

---

### Task 7: Full verification and implementation review

**Files:**
- Modify only files required by verified failures or reviewer findings.

**Interfaces:**
- Consumes: all previous tasks and the v3 design spec.
- Produces: a clean test run, valid Compose model, buildable image, and reviewed implementation.

- [ ] **Step 1: Run focused suites**

```bash
pytest tests/test_token_mgr.py tests/test_leonardo_token_endpoint.py tests/test_leonardo_client.py tests/test_leonardo_refresher.py tests/test_leonardo_refresher_deployment.py -q
```

Expected: all tests pass with no warnings introduced by this feature.

- [ ] **Step 2: Run the full Python suite**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Validate repository and container artifacts**

```bash
git diff --check
bash -n leonardo_refresher/entrypoint.sh
LEONARDO_REFRESH_KEY=test-key LEONARDO_PROXY=http://proxy:10809 NOVNC_PASSWORD=test-pass docker compose --profile leonardo config --quiet
docker compose --profile leonardo build leonardo-refresher
```

Expected: every command exits 0.

- [ ] **Step 4: Request independent code review**

Give the reviewer the committed git range and both:

- `docs/superpowers/specs/2026-08-02-leonardo-refresher-sidecar-design.md`
- `docs/superpowers/plans/2026-08-02-leonardo-refresher-sidecar.md`

Require file/line findings for correctness, security, failure states, tests, and deployment behavior.

- [ ] **Step 5: Resolve all Critical and Important findings with TDD**

For each behavioral defect, add a failing regression test, observe the expected failure, implement the smallest correction, and rerun focused plus full suites.

- [ ] **Step 6: Commit review fixes if any**

```bash
git add core/token_mgr.py core/leonardo_client.py api/schemas.py api/routes/leonardo_tokens.py app.py leonardo_refresher docker-compose.yml docs/leonardo_integration.md tests/test_token_mgr.py tests/test_leonardo_client.py tests/test_leonardo_token_endpoint.py tests/test_leonardo_refresher.py tests/test_leonardo_refresher_deployment.py
git commit -m "fix(leonardo): address refresher review findings"
```
