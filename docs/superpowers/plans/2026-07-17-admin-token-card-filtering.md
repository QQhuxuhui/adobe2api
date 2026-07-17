# Admin Token Card Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the four Token overview cards into accessible filters whose counts and table results use one shared predicate, with safe pagination and selection behavior.

**Architecture:** Put data-only filtering rules in a small browser-compatible CommonJS module so Node's built-in test runner can verify edge cases without adding an npm toolchain. Keep DOM rendering and selection state in `static/admin.js`, but make it consume the shared predicates for both summary counts and filtered rows.

**Tech Stack:** Vanilla JavaScript, HTML, CSS, Node.js 22 built-in `node:test`

## Global Constraints

- `tokenFilter` is one of `null`, `"active"`, `"zero_credit"`, or `"broken"`.
- `null`, `undefined`, `""`, and `NaN` are unknown credit values and must not count as zero credit.
- A broken account is invalid, error, expired, or has a non-empty `credits_error`.
- Filter categories may overlap; their counts do not need to sum to the total.
- Summary cards and the credits chart always use the complete `latestTokens` list.
- Changing filters resets `tokenCurrentPage` to 1 and clears `tokenSelectedIds`.
- Cards must be native buttons with correct `aria-pressed` state.
- Do not add an npm dependency or package manifest.

---

### Task 1: Add Tested Token Filter Predicates

**Files:**
- Create: `static/admin_token_filters.js`
- Create: `tests/test_admin_token_filters.js`

**Interfaces:**
- Consumes: token objects returned by `GET /api/v1/tokens`
- Produces: `window.AdminTokenFilters` in the browser and `module.exports` in Node
- Produces: `hasKnownCredits(value) -> boolean`
- Produces: `matchesTokenFilter(token, filter) -> boolean`
- Produces: `getFilteredTokens(tokens, filter) -> Array`
- Produces: `resolveTokenFilter(currentFilter, requestedFilter) -> null|string`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_admin_token_filters.js`:

```javascript
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  getFilteredTokens,
  hasKnownCredits,
  matchesTokenFilter,
  resolveTokenFilter,
} = require("../static/admin_token_filters.js");

test("hasKnownCredits rejects missing values and accepts finite numbers", () => {
  assert.equal(hasKnownCredits(null), false);
  assert.equal(hasKnownCredits(undefined), false);
  assert.equal(hasKnownCredits(""), false);
  assert.equal(hasKnownCredits("   "), false);
  assert.equal(hasKnownCredits(Number.NaN), false);
  assert.equal(hasKnownCredits(Number.POSITIVE_INFINITY), false);
  assert.equal(hasKnownCredits(0), true);
  assert.equal(hasKnownCredits(-1), true);
  assert.equal(hasKnownCredits("0"), true);
  assert.equal(hasKnownCredits("12.5"), true);
});

test("matchesTokenFilter uses the approved category semantics", () => {
  const activeWithCreditsError = {
    status: "active",
    credits_available: 10,
    credits_error: "credits request failed: 401",
    is_expired: false,
  };
  const zeroCredit = {
    status: "disabled",
    credits_available: 0,
    credits_error: "",
    is_expired: false,
  };
  const unknownCredit = {
    status: "active",
    credits_available: null,
    credits_error: "",
    is_expired: false,
  };

  assert.equal(matchesTokenFilter(activeWithCreditsError, "active"), true);
  assert.equal(matchesTokenFilter(activeWithCreditsError, "broken"), true);
  assert.equal(matchesTokenFilter(zeroCredit, "zero_credit"), true);
  assert.equal(matchesTokenFilter(unknownCredit, "zero_credit"), false);
  assert.equal(matchesTokenFilter({ status: "invalid" }, "broken"), true);
  assert.equal(matchesTokenFilter({ status: "error" }, "broken"), true);
  assert.equal(matchesTokenFilter({ status: "active", is_expired: true }, "broken"), true);
  assert.equal(matchesTokenFilter({ status: "active" }, null), true);
});

test("getFilteredTokens is safe for invalid input and preserves overlap", () => {
  const tokens = [
    { id: "a", status: "active", credits_available: 5, credits_error: "401" },
    { id: "b", status: "disabled", credits_available: 0, credits_error: "" },
    { id: "c", status: "invalid", credits_available: null, credits_error: "" },
  ];

  assert.deepEqual(getFilteredTokens(tokens, null).map((token) => token.id), ["a", "b", "c"]);
  assert.deepEqual(getFilteredTokens(tokens, "active").map((token) => token.id), ["a"]);
  assert.deepEqual(getFilteredTokens(tokens, "zero_credit").map((token) => token.id), ["b"]);
  assert.deepEqual(getFilteredTokens(tokens, "broken").map((token) => token.id), ["a", "c"]);
  assert.deepEqual(getFilteredTokens(null, "active"), []);
});

test("resolveTokenFilter toggles active filters and keeps all stable", () => {
  assert.equal(resolveTokenFilter(null, "active"), "active");
  assert.equal(resolveTokenFilter("active", "active"), null);
  assert.equal(resolveTokenFilter("active", "broken"), "broken");
  assert.equal(resolveTokenFilter("broken", null), null);
  assert.equal(resolveTokenFilter(null, null), null);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
node --test tests/test_admin_token_filters.js
```

Expected: FAIL with `Cannot find module '../static/admin_token_filters.js'`.

- [ ] **Step 3: Implement the pure helper module**

Create `static/admin_token_filters.js`:

```javascript
(function initializeAdminTokenFilters(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.AdminTokenFilters = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createAdminTokenFilters() {
  "use strict";

  const VALID_FILTERS = new Set([null, "active", "zero_credit", "broken"]);

  function normalizeFilter(filter) {
    return VALID_FILTERS.has(filter) ? filter : null;
  }

  function hasKnownCredits(value) {
    if (value === null || value === undefined) return false;
    if (typeof value === "string" && value.trim() === "") return false;
    return Number.isFinite(Number(value));
  }

  function matchesTokenFilter(token, filter) {
    const item = token && typeof token === "object" ? token : {};
    const normalized = normalizeFilter(filter);
    if (normalized === null) return true;

    const status = String(item.status || "").toLowerCase();
    if (normalized === "active") {
      return status === "active";
    }
    if (normalized === "zero_credit") {
      return hasKnownCredits(item.credits_available)
        && Number(item.credits_available) <= 0;
    }
    return status === "invalid"
      || status === "error"
      || Boolean(item.is_expired)
      || String(item.credits_error || "").trim() !== "";
  }

  function getFilteredTokens(tokens, filter) {
    const list = Array.isArray(tokens) ? tokens : [];
    const normalized = normalizeFilter(filter);
    return list.filter((token) => matchesTokenFilter(token, normalized));
  }

  function resolveTokenFilter(currentFilter, requestedFilter) {
    const current = normalizeFilter(currentFilter);
    const requested = normalizeFilter(requestedFilter);
    if (requested !== null && requested === current) return null;
    return requested;
  }

  return {
    getFilteredTokens,
    hasKnownCredits,
    matchesTokenFilter,
    resolveTokenFilter,
  };
});
```

- [ ] **Step 4: Run the unit tests**

Run:

```bash
node --test tests/test_admin_token_filters.js
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit the helper and tests**

```bash
git add static/admin_token_filters.js tests/test_admin_token_filters.js
git commit -m "test: define admin token filter semantics"
```

### Task 2: Make the Overview Cards Semantic Filter Controls

**Files:**
- Modify: `tests/test_admin_token_filters.js`
- Modify: `static/admin.html:50-72`
- Modify: `static/admin.html:413`
- Modify: `static/admin.css:390-438`

**Interfaces:**
- Consumes: `window.AdminTokenFilters` from Task 1
- Produces: four buttons carrying `data-token-filter` values `""`, `"active"`, `"zero_credit"`, and `"broken"`
- Produces: helper script loaded before `admin.js`

- [ ] **Step 1: Add a failing markup contract test**

Append to `tests/test_admin_token_filters.js`:

```javascript
const fs = require("node:fs");
const path = require("node:path");

test("admin markup exposes four semantic filter buttons and loads helpers first", () => {
  const htmlPath = path.join(__dirname, "..", "static", "admin.html");
  const html = fs.readFileSync(htmlPath, "utf8");
  const filterButtons = html.match(/<button[^>]+class="stat-card"[^>]+data-token-filter="[^"]*"/g) || [];

  assert.equal(filterButtons.length, 4);
  assert.match(html, /data-token-filter=""[^>]+aria-pressed="true"/);
  assert.match(html, /data-token-filter="active"[^>]+aria-pressed="false"/);
  assert.match(html, /data-token-filter="zero_credit"[^>]+aria-pressed="false"/);
  assert.match(html, /data-token-filter="broken"[^>]+aria-pressed="false"/);
  assert.ok(html.indexOf("/static/admin_token_filters.js") < html.indexOf("/static/admin.js"));
});
```

- [ ] **Step 2: Run the markup contract test to verify it fails**

Run:

```bash
node --test tests/test_admin_token_filters.js
```

Expected: FAIL because no semantic filter buttons or helper script tag exist yet.

- [ ] **Step 3: Replace the four overview card elements**

Replace the existing `token-stat-grid` contents in `static/admin.html` with:

```html
<div class="token-stat-grid">
  <button class="stat-card" type="button" data-token-filter="" aria-pressed="true">
    <span class="stat-label">账号总数</span>
    <span id="tokenTotalCount" class="stat-value">0</span>
    <span id="tokenAutoRefreshFoot" class="stat-foot">自动刷新 -</span>
  </button>
  <button class="stat-card" type="button" data-token-filter="active" aria-pressed="false">
    <span class="stat-label">生效中</span>
    <span id="tokenActiveCount" class="stat-value">0</span>
    <span id="tokenActiveFoot" class="stat-foot">占比 -</span>
  </button>
  <button class="stat-card" type="button" data-token-filter="zero_credit" aria-pressed="false">
    <span class="stat-label">无积分账号</span>
    <span id="tokenZeroCreditCount" class="stat-value">0</span>
    <span id="tokenZeroCreditFoot" class="stat-foot">余额为 0</span>
  </button>
  <button class="stat-card" type="button" data-token-filter="broken" aria-pressed="false">
    <span class="stat-label">异常账号</span>
    <span id="tokenBrokenCount" class="stat-value">0</span>
    <span id="tokenBrokenFoot" class="stat-foot">失效 / 过期 / 积分异常</span>
  </button>
</div>
```

Add the helper script before `admin.js` at the end of `static/admin.html`, incrementing both cache keys:

```html
<script src="/static/admin_token_filters.js?v=20260717-1"></script>
<script src="/static/admin.js?v=20260717-1"></script>
```

- [ ] **Step 4: Add stable button, active, hover, and focus styles**

Add after the existing `.token-stat-grid .stat-card` rule in `static/admin.css`:

```css
.token-stat-grid button.stat-card {
  width: 100%;
  appearance: none;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transform: none;
  transition: background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}

.token-stat-grid button.stat-card:hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}

.token-stat-grid button.stat-card:active {
  transform: none;
}

.token-stat-grid button.stat-card:focus-visible {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

.token-stat-grid button.stat-card.is-active {
  background: var(--brand-soft);
  border-color: var(--brand);
}

.token-stat-grid .stat-label,
.token-stat-grid .stat-value,
.token-stat-grid .stat-foot {
  display: block;
}
```

- [ ] **Step 5: Run the markup and helper tests**

Run:

```bash
node --test tests/test_admin_token_filters.js
```

Expected: 5 tests PASS.

- [ ] **Step 6: Commit the semantic controls**

```bash
git add static/admin.html static/admin.css tests/test_admin_token_filters.js
git commit -m "feat: make token summary cards accessible filters"
```

### Task 3: Wire Filtering Into Summary, Pagination, and Selection

**Files:**
- Modify: `static/admin.js:72-98`
- Modify: `static/admin.js:125-193`
- Modify: `static/admin.js:232-257`
- Modify: `static/admin.js:324-402`
- Modify: `static/admin.js:1719-1733`

**Interfaces:**
- Consumes: all four exports from `window.AdminTokenFilters`
- Produces: module state `tokenFilter`
- Produces: `setTokenFilter(requestedFilter)` and `syncTokenFilterCards()`
- Preserves: `renderTable(tokens, summary)` as the central render entry point

- [ ] **Step 1: Add the helper bindings and filter state**

Immediately after the existing `tokenPageInfo` declaration, add:

```javascript
  const tokenFilterCards = document.querySelectorAll("[data-token-filter]");
  const {
    getFilteredTokens,
    hasKnownCredits,
    matchesTokenFilter,
    resolveTokenFilter,
  } = window.AdminTokenFilters;
```

Immediately after `let tokenTotalPages = 1;`, add:

```javascript
  let tokenFilter = null;
```

- [ ] **Step 2: Route pagination through the filtered list**

Replace `getCurrentPageTokens` with:

```javascript
  function getCurrentPageTokens(tokens = getFilteredTokens(latestTokens, tokenFilter)) {
    const list = Array.isArray(tokens) ? tokens : [];
    const start = (tokenCurrentPage - 1) * TOKENS_PAGE_SIZE;
    return list.slice(start, start + TOKENS_PAGE_SIZE);
  }
```

- [ ] **Step 3: Replace summary counting with the shared predicates**

Replace `renderTokenSummary` with:

```javascript
  function renderTokenSummary(tokens, summary = null) {
    const list = Array.isArray(tokens) ? tokens : [];
    const total = list.length;
    const active = list.filter((token) => matchesTokenFilter(token, "active")).length;

    let creditsTotalSum = 0;
    let creditsAvailableSum = 0;
    let zeroCredit = 0;
    let unknownCredit = 0;
    let broken = 0;
    let autoRefresh = 0;

    list.forEach((token) => {
      const available = token?.credits_available;
      const capacity = token?.credits_total;
      if (hasKnownCredits(capacity)) creditsTotalSum += Number(capacity);
      if (hasKnownCredits(available)) {
        creditsAvailableSum += Number(available);
      } else {
        unknownCredit += 1;
      }
      if (matchesTokenFilter(token, "zero_credit")) zeroCredit += 1;
      if (matchesTokenFilter(token, "broken")) broken += 1;
      if (token?.auto_refresh) autoRefresh += 1;
    });

    const summaryAvailable = Number(summary?.credits_available_total);
    if (Number.isFinite(summaryAvailable)) creditsAvailableSum = summaryAvailable;

    if (tokenTotalCount) tokenTotalCount.textContent = String(total);
    if (tokenActiveCount) tokenActiveCount.textContent = String(active);
    if (tokenZeroCreditCount) {
      tokenZeroCreditCount.textContent = String(zeroCredit);
      tokenZeroCreditCount.classList.toggle("is-critical", zeroCredit > 0);
    }
    if (tokenBrokenCount) {
      tokenBrokenCount.textContent = String(broken);
      tokenBrokenCount.classList.toggle("is-critical", broken > 0);
    }

    if (tokenAutoRefreshFoot) {
      tokenAutoRefreshFoot.textContent = `自动刷新 ${autoRefresh} 个`;
    }
    if (tokenActiveFoot) {
      const pct = total > 0 ? Math.round((active / total) * 100) : 0;
      tokenActiveFoot.textContent = total > 0 ? `占账号总数 ${pct}%` : "暂无账号";
    }
    if (tokenZeroCreditFoot) {
      tokenZeroCreditFoot.textContent = unknownCredit > 0
        ? `余额为 0，另有 ${unknownCredit} 个未获取`
        : "余额为 0";
    }
    if (tokenBrokenFoot) {
      tokenBrokenFoot.textContent = "失效 / 过期 / 积分异常";
    }

    renderCreditsChart(creditsTotalSum, creditsAvailableSum, unknownCredit);
  }
```

- [ ] **Step 4: Add card state synchronization and filter transitions**

Add immediately after `renderTokenPagination`:

```javascript
  function syncTokenFilterCards() {
    tokenFilterCards.forEach((card) => {
      const cardFilter = String(card.dataset.tokenFilter || "") || null;
      const isActive = cardFilter === tokenFilter;
      card.classList.toggle("is-active", isActive);
      card.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function setTokenFilter(requestedFilter) {
    const nextFilter = resolveTokenFilter(tokenFilter, requestedFilter);
    if (nextFilter === tokenFilter) {
      syncTokenFilterCards();
      return;
    }

    tokenFilter = nextFilter;
    tokenCurrentPage = 1;
    tokenSelectedIds.clear();
    syncTokenFilterCards();
    renderTable(latestTokens, null);
  }

  tokenFilterCards.forEach((card) => {
    card.addEventListener("click", () => {
      const requestedFilter = String(card.dataset.tokenFilter || "") || null;
      setTokenFilter(requestedFilter);
    });
  });
```

- [ ] **Step 5: Render filtered rows and distinct empty states**

Replace `renderTable` with the following function, preserving the existing row template exactly where shown here:

```javascript
  function renderTable(tokens, summary = null) {
    latestTokens = Array.isArray(tokens) ? tokens : [];
    renderTokenSummary(latestTokens, summary);
    syncTokenFilterCards();

    const availableIds = new Set(latestTokens.map((token) => String(token.id || "")).filter(Boolean));
    Array.from(tokenSelectedIds).forEach((id) => {
      if (!availableIds.has(id)) tokenSelectedIds.delete(id);
    });

    const filteredTokens = getFilteredTokens(latestTokens, tokenFilter);
    renderTokenPagination(filteredTokens.length);
    const pageTokens = getCurrentPageTokens(filteredTokens);

    if (!latestTokens.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">当前没有可用的 Token，请在上方添加。</td></tr>`;
      syncTokenSelectAllState();
      return;
    }
    if (!filteredTokens.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">该筛选下暂无账号</td></tr>`;
      syncTokenSelectAllState();
      return;
    }

    tbody.innerHTML = "";
    pageTokens.forEach((token) => {
      const tr = document.createElement("tr");
      const tokenId = String(token.id || "").trim();
      const selectedAttr = tokenSelectedIds.has(tokenId) ? "checked" : "";
      const statusClass = `status-${token.status.toLowerCase()}`;
      const isStatusActive = token.status === "active";
      const isFrozen = token.status === "exhausted" || token.status === "invalid";
      const displayStatus = STATUS_MAP[token.status.toLowerCase()] || token.status;
      const tokenProfileName = String(token.refresh_profile_name || "").trim();
      const tokenProfileEmail = String(token.refresh_profile_email || "").trim();
      const refreshProfileNameSafe = escapeHtml(tokenProfileName);
      const refreshProfileEmailSafe = escapeHtml(tokenProfileEmail);
      const accountName = refreshProfileNameSafe
        ? `<span class="account-name">${refreshProfileNameSafe}</span>`
        : '<span class="account-name">手动 Token</span>';
      const accountEmail = refreshProfileEmailSafe
        ? `<span class="account-email">${refreshProfileEmailSafe}</span>`
        : '<span class="account-meta">无绑定邮箱</span>';
      const autoEnabled = token.auto_refresh && token.auto_refresh_enabled !== false;
      const autoRefreshCell = token.auto_refresh
        ? `<div style="display: flex; align-items: center;"><button class="switch-btn ${autoEnabled ? "on" : "off"}" onclick="toggleAutoRefresh('${token.id}', ${autoEnabled ? "false" : "true"})" title="${autoEnabled ? "点击关闭自动刷新" : "点击开启自动刷新"}"><span class="switch-knob"></span></button><span class="switch-text">${autoEnabled ? "开启" : "关闭"}</span></div>`
        : '<div style="display: flex; align-items: center;"><button class="switch-btn off" disabled title="手动 token 不支持自动刷新"><span class="switch-knob"></span></button><span class="switch-text">手动</span></div>';

      const addedAt = new Date(token.added_at * 1000);
      const dateStr = addedAt.toLocaleString();
      const importedAtText = String(token.refresh_profile_imported_at_text || "").trim();
      const importedLine = importedAtText
        ? `<br><span class="account-meta">导入 ${escapeHtml(importedAtText)}</span>`
        : "";

      const refreshTokenBtn = token.auto_refresh
        ? `<button class="action-mini" onclick="refreshToken('${token.id}')">刷新Token</button>`
        : '<button class="action-mini" disabled title="仅自动刷新 token 支持刷新">刷新Token</button>';
      const statusBtn = isFrozen
        ? '<button class="action-mini" disabled title="额度耗尽或已失效 token 不可启用">不可启用</button>'
        : `<button class="action-mini" onclick="toggleToken('${token.id}', '${isStatusActive ? "disabled" : "active"}')">${isStatusActive ? "禁用Token" : "启用Token"}</button>`;
      const actionsGrid = `
        <div class="action-btns">
          <button class="action-mini" onclick="refreshTokenCredits('${token.id}')">刷新积分</button>
          ${refreshTokenBtn}
          ${statusBtn}
          <button class="action-mini danger" onclick="deleteToken('${token.id}')">删除Token</button>
        </div>
      `;

      tr.innerHTML = `
        <td><input type="checkbox" class="token-select" data-id="${tokenId}" ${selectedAttr} /></td>
        <td title="添加时间: ${dateStr}">${accountName}<br>${accountEmail}${importedLine}</td>
        <td class="token-val">${token.value}</td>
        <td><span class="status-badge ${statusClass}">${displayStatus}</span></td>
        <td>${autoRefreshCell}</td>
        <td>${formatCredits(token)}</td>
        <td class="${token.fails > 0 ? "expiry-gone" : ""}">${token.fails}</td>
        <td style="line-height:1.4;">${formatExpiry(token)}</td>
        <td>${actionsGrid}</td>
      `;
      tbody.appendChild(tr);
    });
    syncTokenSelectAllState();
  }
```

- [ ] **Step 6: Run the automated tests**

Run:

```bash
node --test tests/test_admin_token_filters.js
pytest -q
```

Expected: all Node tests PASS and the existing pytest suite PASS.

- [ ] **Step 7: Perform the browser smoke test**

Start the application on an unused local port:

```bash
uvicorn app:app --host 127.0.0.1 --port 6001
```

Verify in the Token tab:

1. The total card starts active and all accounts display.
2. Clicking each other card filters rows and resets the page indicator to page 1.
3. Clicking an active non-total card returns to all accounts.
4. A selected checkbox is cleared when the filter changes.
5. An active account with a non-empty `credits_error` appears under both active and broken.
6. An account with `credits_available: null` does not appear under zero credit.
7. Tab focus plus Enter and Space activates each card without layout movement.
8. Desktop and mobile widths show no overlap or clipped card text.

- [ ] **Step 8: Commit the controller integration**

```bash
git add static/admin.js
git commit -m "feat: filter token table from summary cards"
```
