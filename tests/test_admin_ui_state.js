"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  createLatestRequestGate,
  fetchTokenList,
  retainSelectedTokenIds,
  runLatestRequest,
  updateInputValue,
} = require("../static/admin_ui_state.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test("selection retains only IDs in the complete filtered result", () => {
  const retained = retainSelectedTokenIds(
    new Set(["visible-page-1", "visible-page-2", "hidden"]),
    [
      { id: "visible-page-1" },
      { id: "visible-page-2" },
    ],
  );

  assert.deepEqual(retained, ["visible-page-1", "visible-page-2"]);
});

test("selection reconciliation safely ignores invalid and blank IDs", () => {
  assert.deepEqual(retainSelectedTokenIds(null, [{ id: "visible" }]), []);
  assert.deepEqual(
    retainSelectedTokenIds(["", null, "visible", "missing"], [
      null,
      { id: "" },
      { id: "visible" },
    ]),
    ["visible"],
  );
});

test("request gate accepts only the latest non-invalidated request", () => {
  const gate = createLatestRequestGate();
  const firstRequest = gate.begin();
  assert.equal(gate.isCurrent(firstRequest), true);

  const secondRequest = gate.begin();
  assert.equal(gate.isCurrent(firstRequest), false);
  assert.equal(gate.isCurrent(secondRequest), true);

  gate.invalidate();
  assert.equal(gate.isCurrent(secondRequest), false);
});

test("only the latest request can commit a successful result", async () => {
  const gate = createLatestRequestGate();
  const older = deferred();
  const newer = deferred();
  const commits = [];
  const olderRun = runLatestRequest(gate, () => older.promise, {
    onSuccess: (value) => commits.push(value),
  });
  const newerRun = runLatestRequest(gate, () => newer.promise, {
    onSuccess: (value) => commits.push(value),
  });

  newer.resolve("new");
  assert.equal((await newerRun).status, "success");
  older.resolve("old");
  assert.equal((await olderRun).status, "stale");
  assert.deepEqual(commits, ["new"]);
});

test("a stale failed request cannot clear newer state", async () => {
  const gate = createLatestRequestGate();
  const older = deferred();
  const newer = deferred();
  const failures = [];
  const olderRun = runLatestRequest(gate, () => older.promise, {
    onFailure: (error) => failures.push(error.message),
  });
  const newerRun = runLatestRequest(gate, () => newer.promise);

  newer.resolve("new");
  assert.equal((await newerRun).status, "success");
  older.reject(new Error("old failure"));
  assert.equal((await olderRun).status, "stale");
  assert.deepEqual(failures, []);
});

test("the current failed request commits one failure result", async () => {
  const gate = createLatestRequestGate();
  const failures = [];
  const result = await runLatestRequest(
    gate,
    async () => { throw new Error("current failure"); },
    { onFailure: (error) => failures.push(error.message) },
  );

  assert.equal(result.status, "failure");
  assert.equal(result.error.message, "current failure");
  assert.deepEqual(failures, ["current failure"]);
});

test("token list rejects non-ok responses even when they contain JSON", async () => {
  await assert.rejects(
    () => fetchTokenList(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ detail: "failed" }),
    })),
    /token list request failed: 500/,
  );
});

test("token list normalizes successful token and legacy item payloads", async () => {
  const urls = [];
  const result = await fetchTokenList(async (url) => {
    urls.push(url);
    return {
      ok: true,
      status: 200,
      json: async () => ({ items: [{ id: "legacy" }], summary: { total: 1 } }),
    };
  });

  assert.deepEqual(urls, ["/api/v1/tokens"]);
  assert.deepEqual(result, {
    tokens: [{ id: "legacy" }],
    summary: { total: 1 },
  });
});

test("programmatic input changes invalidate dependent state once", () => {
  const input = { value: "old" };
  let changes = 0;

  assert.equal(updateInputValue(input, "new", () => { changes += 1; }), true);
  assert.equal(input.value, "new");
  assert.equal(updateInputValue(input, "new", () => { changes += 1; }), false);
  assert.equal(updateInputValue(null, "ignored", () => { changes += 1; }), false);
  assert.equal(changes, 1);
});

test("admin reconciles selection against the complete filtered result", () => {
  const repoRoot = path.join(__dirname, "..");
  const html = fs.readFileSync(path.join(repoRoot, "static", "admin.html"), "utf8");
  const script = fs.readFileSync(path.join(repoRoot, "static", "admin.js"), "utf8");
  const renderStart = script.indexOf("function renderTable");
  const renderEnd = script.indexOf("addBtn.addEventListener", renderStart);
  const renderSource = script.slice(renderStart, renderEnd);

  assert.ok(html.indexOf("/static/admin_ui_state.js?v=20260717-2") > -1);
  assert.ok(
    html.indexOf("/static/admin_ui_state.js") < html.indexOf("/static/admin.js"),
  );
  const uiStateStart = script.indexOf("const {", script.indexOf("window.AdminTokenFilters"));
  const uiStateEnd = script.indexOf("} = window.AdminUiState;", uiStateStart);
  const uiStateSource = script.slice(uiStateStart, uiStateEnd);
  assert.match(uiStateSource, /retainSelectedTokenIds,/);
  assert.match(
    renderSource,
    /retainSelectedTokenIds\(tokenSelectedIds, filteredTokens\)/,
  );
});

test("admin token loads commit only the latest success or failure", () => {
  const script = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.js"),
    "utf8",
  );
  const loadStart = script.indexOf("async function loadTokens");
  const loadEnd = script.indexOf("function getCurrentPageTokens", loadStart);
  const loadSource = script.slice(loadStart, loadEnd);

  assert.match(script, /fetchTokenList,/);
  assert.match(script, /runLatestRequest,/);
  assert.match(script, /const tokenLoadGate = createLatestRequestGate\(\);/);
  assert.match(loadSource, /return runLatestRequest\(/);
  assert.match(loadSource, /tokenLoadGate,/);
  assert.match(loadSource, /\(\) => fetchTokenList\(fetch\)/);
  assert.match(loadSource, /onSuccess: \(\{ tokens, summary \}\) => renderTable\(tokens, summary\)/);
  assert.match(
    loadSource,
    /onFailure: \(err\) => \{[\s\S]*latestTokens = \[\];[\s\S]*tokenSelectedIds\.clear\(\);[\s\S]*tokenCurrentPage = 1;/,
  );

  const refreshStart = script.indexOf('refreshBtn.addEventListener("click"');
  const refreshEnd = script.indexOf("if (tokenSelectAll)", refreshStart);
  const refreshSource = script.slice(refreshStart, refreshEnd);
  assert.match(refreshSource, /const loadResult = await loadTokens\(\);/);
  assert.match(refreshSource, /loadResult\.status === "success"/);
  assert.match(refreshSource, /loadResult\.status === "failure"/);
  assert.doesNotMatch(refreshSource, /else if \(appToast\)/);
});

test("admin invalidates stale proxy results while always restoring the button", () => {
  const script = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.js"),
    "utf8",
  );
  const proxyStart = script.indexOf("if (confProxy)");
  const proxyEnd = script.indexOf("async function loadConfig", proxyStart);
  const proxySource = script.slice(proxyStart, proxyEnd);
  const configStart = script.indexOf("async function loadConfig");
  const configEnd = script.indexOf("saveConfigBtn.addEventListener", configStart);
  const configSource = script.slice(configStart, configEnd);

  assert.match(script, /createLatestRequestGate,/);
  assert.match(script, /updateInputValue,/);
  assert.match(script, /const proxyTestGate = createLatestRequestGate\(\);/);
  assert.match(
    script,
    /function invalidateProxyTestResult\(\) \{\s*proxyTestGate\.invalidate\(\);\s*clearProxyTestResult\(\);\s*\}/,
  );
  assert.match(proxySource, /confProxy\.addEventListener\("input", invalidateProxyTestResult\);/);
  assert.match(proxySource, /const requestVersion = proxyTestGate\.begin\(\);/);
  assert.equal(
    (proxySource.match(/if \(!proxyTestGate\.isCurrent\(requestVersion\)\) return;/g) || []).length,
    2,
  );
  assert.match(
    proxySource,
    /finally \{\s*testProxyBtn\.disabled = false;/,
  );
  assert.match(
    configSource,
    /updateInputValue\(confProxy, data\.proxy \|\| "", invalidateProxyTestResult\);/,
  );
});
