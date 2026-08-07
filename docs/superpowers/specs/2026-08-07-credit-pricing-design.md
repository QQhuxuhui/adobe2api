# Request Credit Pricing Design

**Date:** 2026-08-07

**Status:** Approved direction, pending written-spec review

## Goal

Allow an administrator to configure the RMB price per credit and show each request's credit consumption and corresponding RMB cost in the request log. A request must retain the unit price that was active when it started, so later price changes do not rewrite historical costs.

## Decisions

- The configured unit is Chinese yuan per credit (`CNY/credit`).
- The setting is named `credit_price_cny`.
- The default is `null` (not configured), not zero. A missing price must never be presented as a free request.
- The request-level snapshot is named `credit_unit_price_cny`.
- The calculated total is named `cost_cny` and is rounded to six decimal places for persistence/display.
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

Register `credit_price_cny` in `ConfigManager` with a `null` default. Add it to `ConfigUpdateRequest`, validate it as either null or a finite non-negative number, and return it through the existing config endpoints. The admin configuration UI gets a dedicated billing category with a numeric input that accepts small decimal prices such as `0.001`.

The backend remains authoritative for validation. The frontend mirrors the same range/precision checks for immediate feedback, but must not silently convert an empty input to zero.

### Request snapshot and cost calculation

At request start, capture the current configured price into request state. Every log row generated for that request, including retry-attempt rows, uses that same snapshot. If the setting changes while a request is in flight, the request continues using its original price.

When a log row has a finite `credits_used` and a finite non-negative snapshot price, calculate:

```text
cost_cny = round(credits_used * credit_unit_price_cny, 6)
```

If either value is unavailable, leave `cost_cny` null. Credit source (`measured`, `estimated`, `upstream`) remains independent and continues to describe how the credit number was obtained.

For Leonardo, the route's immediate credit result is enriched with the snapshot before the final log is written. For Adobe, `CreditsTracker._merge_credits` must preserve the snapshot already present in the queued payload and calculate `cost_cny` when it backfills the measured or estimated credit value. The existing authoritative-credit protection must continue to prevent an asynchronous backfill from overwriting an exact Leonardo value.

### Persistent log schema

Extend `RequestLogRecord` with optional fields:

- `credit_unit_price_cny: Optional[float] = None`
- `cost_cny: Optional[float] = None`

The fields are added to both initial attempt payloads and final upsert payloads. `RequestLogStore` needs no format migration because JSONL rows are dictionaries and old rows can omit the fields.

### Admin log presentation

Keep the existing `积分` column for credit usage and add a `成本` column for the total RMB cost. The cost formatter receives the stored cost, unit price, and credit source:

- exact credit source: `¥0.1460`
- estimated credit source: `~¥0.1400`
- missing credits or missing price: `-`

The cost cell's tooltip includes the request-time unit price, for example `单价 ¥0.001/积分`. The existing credit formatter remains responsible for marking estimated credit values with `~`. The empty-state row and table column span are updated for the additional column.

No current price is fetched when logs are listed. Rendering uses only the snapshot fields stored on each row, which guarantees historical stability.

## Validation and Error Handling

- `null` means cost calculation is disabled/unknown and is accepted.
- A configured price must be finite and `>= 0`; invalid values return HTTP 422/400 through the existing config validation path.
- Empty UI input maps to `null`, not `0`.
- Non-finite, negative, or non-numeric credit values never produce a cost.
- Cost calculation failures must not affect image/video generation or request success; they result in a null cost and a normal log row.
- Existing `credits_used` and `credits_source` behavior is unchanged.

## Testing Strategy

Backend tests will cover:

1. Config defaults to null and accepts/rejects the defined price values.
2. Request middleware snapshots the configured price once and uses it for all attempt/final log payloads.
3. Leonardo measured, upstream, and estimated credit paths calculate the expected `cost_cny`.
4. Adobe asynchronous measured/estimated backfill calculates cost from the queued snapshot.
5. Updating the configured price does not change a previously persisted log row.
6. Legacy log rows without price fields remain readable with null cost.

Frontend tests will cover:

1. Config load/save includes the new price field and preserves null.
2. Cost formatting distinguishes exact, estimated, and unknown states.
3. The log table has the new cost column and correct empty-state colspan.

Focused test commands will include the relevant Python tests and Node tests, followed by the full existing test suite.

## Non-Goals

- No billing totals dashboard, invoice export, or account-level cost aggregation.
- No retroactive migration of old request-log rows.
- No change to how Leonardo balances are refreshed or how credit sources are measured.
- No per-model or per-account price table; this version uses one global RMB price per credit.
