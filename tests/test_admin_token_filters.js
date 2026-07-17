"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
  getFilteredTokens,
  hasKnownCredits,
  matchesTokenFilter,
  resolveTokenFilter,
} = require("../static/admin_token_filters.js");

test("CommonJS loading does not mutate the Node global object", () => {
  assert.equal(globalThis.AdminTokenFilters, undefined);
});

test("browser loading exposes the complete AdminTokenFilters contract", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin_token_filters.js"),
    "utf8",
  );
  const context = {};

  vm.runInNewContext(source, context);

  assert.deepEqual(
    Object.keys(context.AdminTokenFilters).sort(),
    [
      "getFilteredTokens",
      "hasKnownCredits",
      "matchesTokenFilter",
      "resolveTokenFilter",
    ],
  );
  assert.equal(
    context.AdminTokenFilters.matchesTokenFilter(
      { status: "active", credits_error: "401" },
      "broken",
    ),
    true,
  );
});

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

test("zero-credit filtering composes every documented credit boundary", () => {
  const cases = [
    [null, false],
    [undefined, false],
    ["", false],
    ["   ", false],
    [Number.NaN, false],
    [0, true],
    [-1, true],
    ["0", true],
    ["12.5", false],
  ];

  cases.forEach(([creditsAvailable, expected]) => {
    assert.equal(
      matchesTokenFilter(
        { status: "active", credits_available: creditsAvailable },
        "zero_credit",
      ),
      expected,
    );
  });
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

test("admin markup exposes four semantic filter buttons and loads helpers first", () => {
  const htmlPath = path.join(__dirname, "..", "static", "admin.html");
  const html = fs.readFileSync(htmlPath, "utf8");
  const filterButtons = html.match(
    /<button[^>]+class="stat-card"[^>]+data-token-filter="[^"]*"/g,
  ) || [];

  assert.equal(filterButtons.length, 4);
  assert.match(html, /data-token-filter=""[^>]+aria-pressed="true"/);
  assert.match(html, /data-token-filter="active"[^>]+aria-pressed="false"/);
  assert.match(html, /data-token-filter="zero_credit"[^>]+aria-pressed="false"/);
  assert.match(html, /data-token-filter="broken"[^>]+aria-pressed="false"/);
  assert.ok(
    html.indexOf("/static/admin_token_filters.js")
      < html.indexOf("/static/admin.js"),
  );
});

test("admin controller wires filtering into pagination, selection, and empty states", () => {
  const adminJsPath = path.join(__dirname, "..", "static", "admin.js");
  const source = fs.readFileSync(adminJsPath, "utf8");

  assert.match(source, /let tokenFilter = null;/);
  assert.match(source, /getFilteredTokens\(latestTokens, tokenFilter\)/);
  assert.match(source, /tokenCurrentPage = 1;\s+tokenSelectedIds\.clear\(\);/);
  assert.match(source, /matchesTokenFilter\(token, "zero_credit"\)/);
  assert.match(source, /matchesTokenFilter\(token, "broken"\)/);
  assert.match(source, /该筛选下暂无账号/);
  assert.match(source, /card\.setAttribute\("aria-pressed", isActive \? "true" : "false"\)/);
});
