"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
  cookieToHeaderString,
  toCookieBatchItems,
  parseCookieFilesToItems,
} = require("../static/admin_cookie_import.js");

test("CommonJS loading does not mutate the Node global object", () => {
  assert.equal(globalThis.AdminCookieImport, undefined);
});

test("browser loading exposes the complete AdminCookieImport contract", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin_cookie_import.js"),
    "utf8",
  );
  const context = {};

  vm.runInNewContext(source, context);

  assert.deepEqual(
    Object.keys(context.AdminCookieImport).sort(),
    [
      "collectRetryItems",
      "cookieToHeaderString",
      "parseCookieFilesToItems",
      "toCookieBatchItems",
    ],
  );
});

test("collectRetryItems keeps only accounts whose import failed", () => {
  const { collectRetryItems } = require("../static/admin_cookie_import.js");
  const items = [
    { name: "账号A", cookie: "a=1" },
    { name: "账号B", cookie: "b=2" },
    { name: "账号C", cookie: "c=3" },
    { name: "账号D", cookie: "d=4" },
  ];
  const results = [
    { profile: { import_action: "created" } },
    { error: new Error("导入失败") },
    { profile: { import_action: "updated" }, refresh_error: "刷新失败" },
    undefined,
  ];

  assert.deepEqual(collectRetryItems(items, results), [
    { name: "账号B", cookie: "b=2" },
    { name: "账号D", cookie: "d=4" },
  ]);
});

test("collectRetryItems returns nothing when every import succeeded", () => {
  const { collectRetryItems } = require("../static/admin_cookie_import.js");
  const items = [{ name: "账号A", cookie: "a=1" }];

  assert.deepEqual(collectRetryItems(items, [{ profile: {} }]), []);
  assert.deepEqual(collectRetryItems([], []), []);
  assert.deepEqual(collectRetryItems(null, null), []);
});

test("admin wires a retry button that re-imports only failed accounts", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.js"),
    "utf8",
  );
  const html = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.html"),
    "utf8",
  );

  assert.match(html, /id="retryCookieImportBtn"/);
  assert.match(source, /retryCookieImportBtn/);
  assert.match(source, /collectRetryItems/);
});

test("cookieToHeaderString handles strings, arrays, and wrappers", () => {
  assert.equal(cookieToHeaderString("Cookie: a=1; b=2"), "a=1; b=2");
  assert.equal(cookieToHeaderString("a=1; b=2"), "a=1; b=2");
  assert.equal(
    cookieToHeaderString([
      { name: "a", value: "1" },
      { name: "b", value: "2" },
    ]),
    "a=1; b=2",
  );
  assert.equal(cookieToHeaderString({ cookies: [{ name: "a", value: "1" }] }), "a=1");
  assert.equal(cookieToHeaderString({ cookie: "a=1" }), "a=1");
  assert.equal(cookieToHeaderString(""), "");
});

test("toCookieBatchItems expands batch arrays and export payloads", () => {
  assert.deepEqual(
    toCookieBatchItems([
      { name: "账号A", cookie: "a=1" },
      { name: "账号B", cookie: "b=2" },
    ]),
    [
      { name: "账号A", cookie: "a=1" },
      { name: "账号B", cookie: "b=2" },
    ],
  );
  assert.deepEqual(
    toCookieBatchItems({
      exported_at: 1752624000,
      total: 2,
      items: [
        { id: 1, name: "账号A", cookie: "a=1" },
        { id: 2, name: "账号B", cookie: "b=2" },
      ],
    }),
    [
      { name: "账号A", cookie: "a=1" },
      { name: "账号B", cookie: "b=2" },
    ],
  );
  assert.deepEqual(
    toCookieBatchItems([
      { name: "a", value: "1" },
      { name: "b", value: "2" },
    ]),
    [{ name: null, cookie: "a=1; b=2" }],
  );
  assert.deepEqual(toCookieBatchItems("a=1; b=2"), [{ name: null, cookie: "a=1; b=2" }]);
});

test("multi-file batch contents expand into per-account items, not file names", () => {
  const { items, errors } = parseCookieFilesToItems([
    {
      name: "export-1.json",
      text: JSON.stringify([
        { name: "账号A", cookie: "a=1" },
        { name: "账号B", cookie: "b=2" },
      ]),
    },
    {
      name: "export-2.json",
      text: JSON.stringify({
        exported_at: 1752624000,
        total: 2,
        items: [
          { name: "账号C", cookie: "c=3" },
          { name: "账号D", cookie: "d=4" },
        ],
      }),
    },
  ]);

  assert.deepEqual(errors, []);
  assert.deepEqual(items, [
    { name: "账号A", cookie: "a=1" },
    { name: "账号B", cookie: "b=2" },
    { name: "账号C", cookie: "c=3" },
    { name: "账号D", cookie: "d=4" },
  ]);
});

test("file basename is only a fallback name for unnamed cookies", () => {
  const { items, errors } = parseCookieFilesToItems([
    { name: "alice.txt", text: "a=1; b=2" },
    {
      name: "bob.json",
      text: JSON.stringify([
        { name: "a", value: "1" },
        { name: "b", value: "2" },
      ]),
    },
  ]);

  assert.deepEqual(errors, []);
  assert.deepEqual(items, [
    { name: "alice", cookie: "a=1; b=2" },
    { name: "bob", cookie: "a=1; b=2" },
  ]);
});

test("multiple unnamed cookies in one file get indexed fallback names", () => {
  const { items, errors } = parseCookieFilesToItems([
    {
      name: "pool.json",
      text: JSON.stringify([{ cookie: "a=1" }, { cookie: "b=2" }]),
    },
  ]);

  assert.deepEqual(errors, []);
  assert.deepEqual(items, [
    { name: "pool-1", cookie: "a=1" },
    { name: "pool-2", cookie: "b=2" },
  ]);
});

test("a broken file is reported without dropping the remaining files", () => {
  const { items, errors } = parseCookieFilesToItems([
    { name: "empty.txt", text: "   " },
    { name: "bad.json", text: JSON.stringify([{ name: "无 cookie" }]) },
    { name: "good.txt", text: "a=1" },
  ]);

  assert.deepEqual(items, [{ name: "good", cookie: "a=1" }]);
  assert.equal(errors.length, 2);
  assert.equal(errors[0].file, "empty.txt");
  assert.equal(errors[1].file, "bad.json");
});

test("admin.js delegates file parsing to AdminCookieImport and supports drag-drop", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.js"),
    "utf8",
  );

  assert.match(source, /window\.AdminCookieImport/);
  assert.match(source, /parseCookieFilesToItems/);
  assert.doesNotMatch(source, /function cookieToHeaderString/);
  assert.doesNotMatch(source, /function toCookieBatchItems/);
  assert.match(source, /addEventListener\("drop"/);
  assert.match(source, /addEventListener\("dragover"/);
});

test("admin markup loads the cookie import helpers before the controller", () => {
  const html = fs.readFileSync(
    path.join(__dirname, "..", "static", "admin.html"),
    "utf8",
  );

  assert.ok(html.includes("/static/admin_cookie_import.js"));
  assert.ok(
    html.indexOf("/static/admin_cookie_import.js") < html.indexOf("/static/admin.js"),
  );
});
