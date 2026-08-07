# Request Credit Pricing Design

**Date:** 2026-08-07

**Status:** Approved

## Goal

Allow an administrator to configure separate RMB prices for Leonardo and Adobe credits and show each request's credit consumption and corresponding RMB cost in the request log. A request must retain the provider-specific unit price that was active when it started, so later price changes do not rewrite historical costs.

## Decisions

- The configured unit is Chinese yuan per credit (`CNY/credit`).
- The settings are named `leonardo_credit_price_cny` and `adobe_credit_price_cny`.
- Both settings default to `null` (not configured), not zero. A missing price must never be presented as a free request.
- The log identifies the selected credit system with `credit_type` (`leonardo` or `adobe`).
- The request-level snapshot is named `credit_unit_price_cny`.
- The calculated total is named `cost_cny` and is rounded to six decimal places for persistence. The UI displays at least four and at most six decimal places.
- Existing logs without the new fields remain unchanged and show unknown cost (`-`). They are not retroactively repriced.
- Estimated credit usage remains visibly approximate; its derived RMB cost is approximate as well.
- No separate billing ledger or database is introduced in this change.

## Existing Context

- `core/config_mgr.py` is the single registry and persistence layer for `config/config.json`.
- `api/routes/admin.py` already exposes authenticated `GET /api/v1/config` and `PUT /api/v1/config` endpoints with field validation.
- `core/stores.py:RequestLogRecord` and `RequestLogStore` persist request-log rows as JSONL and support asynchronous upserts.
- `app.py` emits initial/final request-log rows and owns request middleware state.
- Leonardo routes set `request.state.log_credits_used` and `request.state.log_credits_source` immediately after generation.
- Adobe credit measurement runs asynchronously through `core/credits_tracker.py`, so the original request payload must carry the price snapshot for later backfill.
- `static/admin_log_credits.js` formats the existing measured/estimated/upstream credit states and `static/admin.js` renders the request-log table and configuration form.

## Architecture

### Configuration

Register `leonardo_credit_price_cny` and `adobe_credit_price_cny` in `ConfigManager`, both with a `null` default. Add both to `ConfigUpdateRequest`, validate each as either null or a finite non-negative number, and return them through the existing config endpoints. The admin configuration UI gets a dedicated billing category with one numeric input per credit system; each accepts a price such as `0.001`.

The backend remains authoritative for validation. The frontend mirrors the same range/precision checks for immediate feedback, but must not silently convert an empty input to zero. Prices are limited to six decimal places; values with more precision are rejected rather than silently rounded.

### Request snapshot and cost calculation

At request start, capture both configured prices into request state. Once token routing selects a credit system, select the matching snapshot and store its `credit_type` and `credit_unit_price_cny` on the request. Every log row generated for that request, including retry-attempt rows, uses that same selection. If either setting changes while a request is in flight, the request continues using its original snapshot.

When a log row has a finite `credits_used` and a finite non-negative snapshot price, calculate with decimal arithmetic (`Decimal(str(value))`) rather than binary float multiplication:

```text
cost_cny = round(credits_used * credit_unit_price_cny, 6)
```

The persisted JSON number is normalized to six decimal places after calculation. If either value is unavailable, leave `cost_cny` null. Credit source (`measured`, `estimated`, `upstream`) remains independent and continues to describe how the credit number was obtained.

For Leonardo, the route's immediate credit result is enriched with the selected snapshot before the final log is written. For Adobe, `CreditsTracker._merge_credits` must preserve `credit_type` and the price snapshot already present in the queued payload and calculate `cost_cny` when it backfills the measured or estimated credit value. Cost enrichment must happen before the authoritative-credit early return, so an existing exact credit value cannot leave a missing cost behind. The existing authoritative-credit protection must continue to prevent an asynchronous backfill from overwriting an exact Leonardo value.

### Persistent log schema

Extend `RequestLogRecord` with optional fields:

- `credit_type: Optional[str] = None`
- `credit_unit_price_cny: Optional[float] = None`
- `cost_cny: Optional[float] = None`

The fields are added to both initial attempt payloads and final upsert payloads. `RequestLogStore` needs no format migration because JSONL rows are dictionaries and old rows can omit the fields.

### Admin log presentation

Keep the existing `积分` column for credit usage and add a `成本` column for the total RMB cost. The cost formatter receives the stored cost, credit type, unit price, and credit source:

- exact credit source: `¥0.1460`
- estimated credit source: `~¥0.1400`
- missing credits or missing price: `-`

The cost cell's tooltip includes the request-time credit system and unit price, for example `Leonardo 单价 ¥0.001/积分`. The existing credit formatter remains responsible for marking estimated credit values with `~`; the cost formatter uses the same approximation marker. The empty-state row and table column span are updated for the additional column.

No current price is fetched when logs are listed. Rendering uses only the snapshot fields stored on each row, which guarantees historical stability.

## Validation and Error Handling

- `null` means cost calculation is disabled/unknown and is accepted.
- A configured price must be finite, `>= 0`, and have no more than six decimal places; invalid values return HTTP 422 through the existing config validation path.
- Empty UI input maps to `null`, not `0`.
- Non-finite, negative, or non-numeric credit values never produce a cost.
- Cost calculation failures must not affect image/video generation or request success; they result in a null cost and a normal log row.
- Existing `credits_used` and `credits_source` behavior is unchanged.
- A request with an unknown credit amount (including failed requests where no charge measurement is available) keeps both credit and cost as unknown; the feature does not infer a charge from HTTP status alone.

## Testing Strategy

Backend tests will cover:

1. Config defaults both prices to null and accepts/rejects the defined price values.
2. Request middleware snapshots the configured price once and uses it for all attempt/final log payloads.
3. Leonardo and Adobe routing select the matching price snapshot and persist `credit_type`.
4. Leonardo measured, upstream, and estimated credit paths calculate the expected `cost_cny`.
5. Adobe asynchronous measured/estimated backfill calculates cost from the queued snapshot, including the authoritative-credit early-return case.
6. Updating either configured price does not change a previously persisted log row.
7. Legacy log rows without price fields remain readable with null cost.

Frontend tests will cover:

1. Config load/save includes both new price fields and preserves null.
2. Cost formatting distinguishes exact, estimated, and unknown states.
3. The log table has the new cost column and correct empty-state colspan.

Focused test commands will include the relevant Python tests and Node tests, followed by the full existing test suite.

## Non-Goals

- No billing totals dashboard, invoice export, or account-level cost aggregation.
- No retroactive migration of old request-log rows.
- No change to how Leonardo balances are refreshed or how credit sources are measured.
- No per-model or per-account price table; this version uses one global RMB price per credit system (one Leonardo price and one Adobe price).
