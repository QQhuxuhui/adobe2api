"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(repoRoot, "static", "admin.html"), "utf8");
const js = fs.readFileSync(path.join(repoRoot, "static", "admin.js"), "utf8");

test("toolbar exposes a Leonardo cookie import entry", () => {
  // 之前只有 Adobe 的「导入 Cookie」，用户只能把 Leonardo cookie 粘错框
  assert.match(html, /id="openLeoCookieBtn"[^>]*>导入 Leonardo Cookie</);
  assert.match(html, /id="leoCookieModal"/);
  assert.match(html, /id="leoCookieInput"/);
  assert.match(html, /id="leoCookieSubmitBtn"/);
});

test("dialog states the required cookie names and separates the two flows", () => {
  const modal = html.match(/<div id="leoCookieModal"[\s\S]*?<\/div>\s*<\/div>\s*<\/div>/)?.[0] || "";
  assert.match(modal, /__Secure-better-auth\.session_token/);
  assert.match(modal, /session_data\.0/);
  // 必须说明与 Adobe 导入/添加 Token 的区别，避免再次粘错
  assert.match(modal, /Adobe 账号请走「导入 Cookie」/);
  assert.match(modal, /添加 Token/);
});

test("controller posts to the admin leonardo cookie endpoint and shows status", () => {
  assert.match(js, /fetch\("\/api\/v1\/leonardo\/cookie",\s*\{/);
  assert.match(js, /method:\s*"POST"/);
  assert.match(js, /fetch\("\/api\/v1\/leonardo\/cookie\/status"\)/);
  // 失败时展示后端给的指引文案，而不是吞掉
  assert.match(js, /data\.detail \|\| `失败（HTTP \$\{res\.status\}）`/);
});
