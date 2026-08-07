# Credit Pricing Implementation Plan

> Status: Implemented and verified on 2026-08-07.

> **For agentic workers:** Execute this plan task-by-task with TDD. Each task starts with a failing test and ends with focused verification.

**Goal:** Add separate Leonardo and Adobe CNY-per-credit settings, snapshot the selected provider price per request, and show stable credit and RMB cost values in request logs.

**Architecture:** Keep configuration in the existing `ConfigManager`/admin config API. Capture both prices at request middleware start, select the provider-specific snapshot when a token is selected, and persist `credit_type`, `credit_unit_price_cny`, and `cost_cny` alongside existing credit fields. Use one shared Decimal-based helper for validation and multiplication; use the stored snapshot for Adobe's asynchronous backfill and the existing log JSONL store.

**Tech Stack:** Python 3, FastAPI/Pydantic v2, JSONL request logs, vanilla JavaScript, Node `node:test`, pytest.

## Global Constraints

- Prices are RMB per credit and are either null or finite, non-negative values with at most six decimal places.
- Empty configuration input means null, never zero.
- Cost calculation uses `Decimal(str(value))` and rounds to six decimal places with `ROUND_HALF_UP`.
- Historical rows use only persisted snapshot fields; current settings are never applied while listing logs.
- Unknown credit usage or missing provider price produces null cost and `-` in the UI.
- Leonardo `measured/upstream/estimated` source semantics remain unchanged.
- No request-generation or balance-refresh behavior changes are allowed.

### Task 1: Decimal pricing utility and configuration contract

**Files:**
- Create: `core/credit_costs.py`
- Modify: `core/config_mgr.py`
- Modify: `api/schemas.py`
- Modify: `api/routes/admin.py`
- Modify: `config/config.example.json`
- Test: `tests/test_credit_costs.py`

**Interfaces:**
- `normalize_credit_price(value) -> float | None` validates null/finite/non-negative/at-most-six-decimal values and returns a normalized JSON-safe number.
- `calculate_credit_cost(credits_used, unit_price) -> float | None` returns a six-decimal RMB cost or null.
- Configuration keys are `leonardo_credit_price_cny` and `adobe_credit_price_cny`.

- [ ] **Step 1: Write failing utility and schema tests**

Add tests with these behaviors:

```python
def test_normalize_credit_price_accepts_null_zero_and_six_decimals():
    assert normalize_credit_price(None) is None
    assert normalize_credit_price(0) == 0.0
    assert normalize_credit_price("0.001") == 0.001


@pytest.mark.parametrize("value", [-0.1, "nan", "inf", 0.0000001])
def test_normalize_credit_price_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_credit_price(value)


def test_calculate_credit_cost_uses_decimal_round_half_up():
    assert calculate_credit_cost("146", "0.001") == 0.146
    assert calculate_credit_cost("1", "0.0000005") == 0.000001


def test_calculate_credit_cost_returns_none_when_input_is_unknown():
    assert calculate_credit_cost(None, "0.001") is None
    assert calculate_credit_cost(10, None) is None
```

Add schema tests asserting both config fields are present and invalid values fail validation.

- [ ] **Step 2: Run tests to verify the expected failure**

Run: `python -m pytest tests/test_credit_costs.py -q`

Expected: FAIL because `core.credit_costs` and the new schema fields do not exist.

- [ ] **Step 3: Implement the shared helper and config fields**

Implement `normalize_credit_price` with `Decimal(str(value))`, reject non-finite/negative values and values whose exponent exceeds six fractional places, and return `float(decimal_value)`. Implement `calculate_credit_cost` with Decimal multiplication and `quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)`.

Add both config keys with null defaults. Add `Optional[float]` fields to `ConfigUpdateRequest` and field validators that call `normalize_credit_price`. In `update_config`, copy validated values into `update_data` so explicit null clears a configured price. Add both keys to `config/config.example.json`.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_credit_costs.py -q`

Expected: PASS.

### Task 2: Request snapshot, persisted fields, and async backfill

**Files:**
- Modify: `core/stores.py`
- Modify: `app.py`
- Modify: `core/credits_tracker.py`
- Test: `tests/test_generation_credit_context.py`
- Test: `tests/test_credits_tracker.py`
- Test: `tests/test_request_log_store.py`

**Interfaces:**
- Request state holds `log_credit_prices_cny` (both provider snapshots), `log_credit_type`, and `log_credit_unit_price_cny` (selected snapshot).
- Each request-log payload includes optional `credit_type`, `credit_unit_price_cny`, and `cost_cny`.
- `CreditsTracker._merge_credits` calculates cost before its authoritative-credit early return and never replaces an existing authoritative credit value.
- Add pure helpers `snapshot_credit_prices(config_manager) -> dict[str, float | None]` and `select_credit_price(snapshot, credit_type) -> float | None` in `core/credit_costs.py` so the snapshot decision is directly testable.

- [ ] **Step 1: Write failing snapshot and backfill tests**

Add tests for:

```python
def test_request_price_snapshot_is_stable_when_config_changes(monkeypatch):
    from core.credit_costs import select_credit_price, snapshot_credit_prices

    config = {"leonardo_credit_price_cny": 0.001, "adobe_credit_price_cny": 0.002}
    snapshot = snapshot_credit_prices(config.get)
    config["leonardo_credit_price_cny"] = 0.009
    assert select_credit_price(snapshot, "leonardo") == 0.001


def test_log_payload_contains_provider_price_and_cost():
    from core.credit_costs import calculate_credit_cost

    payload = {
        "credit_type": "leonardo",
        "credit_unit_price_cny": 0.001,
        "credits_used": 146.0,
        "credits_source": "measured",
    }
    payload["cost_cny"] = calculate_credit_cost(
        payload["credits_used"], payload["credit_unit_price_cny"]
    )
    assert payload == {
        "credit_type": "leonardo",
        "credit_unit_price_cny": 0.001,
        "credits_used": 146.0,
        "credits_source": "measured",
        "cost_cny": 0.146,
    }


def test_tracker_backfill_calculates_cost_from_queued_snapshot():
    payload = {
        "credits_used": None,
        "credits_source": None,
        "credit_type": "adobe",
        "credit_unit_price_cny": 0.002,
    }
    merged = CreditsTracker._merge_credits(payload, 12, "measured")
    assert merged["cost_cny"] == 0.024


def test_tracker_authoritative_value_still_gets_cost_without_overwrite():
    payload = {
        "credits_used": 250,
        "credits_source": "upstream",
        "credit_type": "leonardo",
        "credit_unit_price_cny": 0.001,
    }
    merged = CreditsTracker._merge_credits(payload, 999, "estimated")
    assert merged["credits_used"] == 250
    assert merged["cost_cny"] == 0.25
```

Add a RequestLogRecord serialization assertion for the three optional fields and a legacy row assertion that omitted fields remain readable.

- [ ] **Step 2: Run tests to verify the expected failure**

Run: `python -m pytest tests/test_generation_credit_context.py tests/test_credits_tracker.py tests/test_request_log_store.py -q`

Expected: FAIL because request state and persisted cost fields do not exist.

- [ ] **Step 3: Implement the minimal logging and backfill changes**

Add the three optional dataclass fields. In the request middleware, capture both validated config prices once into request state. In `_set_request_token_context`, select `leonardo` or `adobe` from token metadata and copy the corresponding snapshot into request state. Add a small app helper that builds the three log fields from request state and current credits/source; use it in both `_append_attempt_log` and middleware finalization.

Update `CreditsTracker._merge_credits` to merge/backfill credits as before, then calculate `cost_cny` from the payload snapshot regardless of whether the authoritative-credit guard will return. Preserve an existing cost when no valid replacement can be calculated. Do not enqueue Leonardo requests into this tracker.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_generation_credit_context.py tests/test_credits_tracker.py tests/test_request_log_store.py -q`

Expected: PASS.

### Task 3: Admin configuration UI and API coverage

**Files:**
- Modify: `static/admin.html`
- Modify: `static/admin.js`
- Modify: `tests/test_admin_ui_state.js`
- Create or modify: `tests/test_admin_config.py`

- [ ] **Step 1: Write failing config UI/API tests**

Assert the HTML contains a billing category with `confLeonardoCreditPriceCny` and `confAdobeCreditPriceCny`, the JS loads/saves both fields and maps blank input to null, and the API accepts both prices while rejecting negative, non-finite, or seven-decimal values.

- [ ] **Step 2: Run tests to verify the expected failure**

Run: `node --test tests/test_admin_ui_state.js && python -m pytest tests/test_admin_config.py -q`

Expected: FAIL because the controls and API fields do not exist.

- [ ] **Step 3: Implement the configuration controls**

Add a `计费` category with two number inputs, `step="0.000001"`, explanatory text, and a note that blank means unknown cost. Load both values from `/api/v1/config`. During save, preserve null for blank input, reject non-finite/negative/more-than-six-decimal values client-side, and include both fields in the existing PUT payload.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `node --test tests/test_admin_ui_state.js && python -m pytest tests/test_admin_config.py -q`

Expected: PASS.

### Task 4: Request-log cost display

**Files:**
- Modify: `static/admin_log_credits.js`
- Modify: `static/admin.js`
- Modify: `static/admin.html`
- Modify: `static/admin.css`
- Modify: `tests/test_admin_log_credits.js`

**Interfaces:**
- Add `formatLogCost(costCny, creditType, unitPriceCny, creditsSource)` returning `{text, title, estimated}`.

- [ ] **Step 1: Write failing formatter and table tests**

Add assertions for exact `¥0.1460`, estimated `~¥0.1400`, tooltip `Leonardo 单价 ¥0.001/积分`, unknown `-`, six-decimal values, and the new tenth table column/empty-state colspan.

- [ ] **Step 2: Run tests to verify the expected failure**

Run: `node --test tests/test_admin_log_credits.js`

Expected: FAIL because the cost formatter and column do not exist.

- [ ] **Step 3: Implement the formatter and table column**

Use a number formatter with four minimum and six maximum fractional digits, prefix estimated values with `~`, and include provider/unit price in the tooltip. Keep the existing credit formatter unchanged. Add the `成本` header, render the new cell from persisted log fields, update the empty-state colspan to 10, and update column-specific CSS selectors.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `node --test tests/test_admin_log_credits.js`

Expected: PASS.

### Task 5: Full verification and handoff

**Files:**
- No new production files; update tests only if a regression is found.

- [ ] **Step 1: Run all focused pricing tests**

Run: `python -m pytest tests/test_credit_costs.py tests/test_generation_credit_context.py tests/test_credits_tracker.py tests/test_request_log_store.py tests/test_admin_config.py -q && node --test tests/test_admin_log_credits.js tests/test_admin_ui_state.js`

Expected: PASS with no warnings related to the feature.

- [ ] **Step 2: Run the complete existing test suite**

Run: `python -m pytest -q`

Expected: PASS; if the repository has an independent Node test command, run the existing JavaScript test command as well.

- [ ] **Step 3: Review the final diff and working tree**

Run: `git diff --check` and `git status --short`. Verify that only the pricing implementation, tests, config example, and the two design/plan documents changed; no tokens, cookies, or runtime data files may be staged.
