"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.join(__dirname, "..");

test("credit formatter renders measured, estimated, and unknown states", () => {
  assert.equal(globalThis.AdminLogCredits, undefined);
  const { formatLogCredits } = require("../static/admin_log_credits.js");
  assert.equal(globalThis.AdminLogCredits, undefined);

  assert.deepEqual(formatLogCredits(12, "measured"), {
    text: "12",
    title: "",
    estimated: false,
  });
  assert.deepEqual(formatLogCredits("12.5", "estimated"), {
    text: "~12.5",
    title: "估算值(按历史实测)",
    estimated: true,
  });
  [null, undefined, "", "bad", Infinity].forEach((value) => {
    assert.deepEqual(formatLogCredits(value, "measured"), {
      text: "-",
      title: "",
      estimated: false,
    });
  });
  assert.equal(formatLogCredits(5, null).text, "-");
  assert.equal(formatLogCredits(5, "unexpected").text, "-");
});

test("logs table declares the credit column and nine-column empty states", () => {
  const html = fs.readFileSync(path.join(repoRoot, "static", "admin.html"), "utf8");
  const source = fs.readFileSync(path.join(repoRoot, "static", "admin.js"), "utf8");
  const table = html.match(/<table id="logsTable">[\s\S]*?<\/table>/)?.[0] || "";
  const headers = Array.from(table.matchAll(/<th>([^<]+)<\/th>/g), (match) => match[1]);

  assert.deepEqual(headers, [
    "时间",
    "状态",
    "耗时/秒",
    "进度",
    "账号",
    "模型",
    "积分",
    "提示词摘要",
    "预览",
  ]);
  assert.match(table, /colspan="9"/);
  assert.doesNotMatch(table, /colspan="8"/);
  assert.match(source, /colspan="9"/);
  assert.doesNotMatch(source, /colspan="8"/);
});

test("admin controller loads and uses the credit formatter", () => {
  const html = fs.readFileSync(path.join(repoRoot, "static", "admin.html"), "utf8");
  const source = fs.readFileSync(path.join(repoRoot, "static", "admin.js"), "utf8");
  const css = fs.readFileSync(path.join(repoRoot, "static", "admin.css"), "utf8");

  assert.ok(
    html.indexOf("/static/admin_log_credits.js") < html.indexOf("/static/admin.js"),
  );
  assert.match(source, /const \{ formatLogCredits \} = window\.AdminLogCredits;/);
  assert.match(source, /formatLogCredits\(item\.credits_used, item\.credits_source\)/);
  assert.match(source, /class="log-credits-cell/);
  assert.match(css, /#logsTable th:nth-child\(7\)/);
  assert.match(css, /\.log-prompt-cell[\s\S]*text-overflow:\s*ellipsis/);
  assert.match(css, /\.log-credits-cell\.estimated/);
});
