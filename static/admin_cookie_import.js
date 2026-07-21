(function initializeAdminCookieImport(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root) {
    root.AdminCookieImport = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createAdminCookieImport() {
  "use strict";

  function cookieToHeaderString(value) {
    if (typeof value === "string") {
      const txt = value.trim();
      if (!txt) return "";
      if (txt.toLowerCase().startsWith("cookie:")) {
        return txt.slice(7).trim();
      }
      return txt;
    }
    if (Array.isArray(value)) {
      const pairs = [];
      value.forEach((item) => {
        if (typeof item === "string") {
          const txt = item.trim();
          if (txt) pairs.push(txt);
          return;
        }
        if (!item || typeof item !== "object") return;
        const name = String(item.name || "").trim();
        if (!name) return;
        pairs.push(`${name}=${String(item.value || "").trim()}`);
      });
      return pairs.join("; ");
    }
    if (value && typeof value === "object") {
      if (Array.isArray(value.cookies)) return cookieToHeaderString(value.cookies);
      if (value.cookie != null) return cookieToHeaderString(value.cookie);
    }
    return "";
  }

  function toCookieBatchItems(value) {
    if (Array.isArray(value)) {
      if (value.length > 0 && value.every((item) => item && typeof item === "object" && "name" in item && "value" in item)) {
        const cookie = cookieToHeaderString(value);
        return cookie ? [{ name: null, cookie }] : [];
      }
      return value.map((item, idx) => {
        if (!item || typeof item !== "object") {
          throw new Error(`第 ${idx + 1} 项不是对象`);
        }
        const cookie = cookieToHeaderString(item.cookie != null ? item.cookie : item.cookies != null ? item.cookies : item);
        if (!cookie) {
          throw new Error(`第 ${idx + 1} 项缺少 cookie`);
        }
        return {
          name: String(item.name || item.email || "").trim() || null,
          cookie,
        };
      });
    }
    if (value && typeof value === "object") {
      if (Array.isArray(value.items)) return toCookieBatchItems(value.items);
      const cookie = cookieToHeaderString(value.cookie != null ? value.cookie : value.cookies != null ? value.cookies : value);
      if (!cookie) throw new Error("cookie 内容为空");
      return [{ name: String(value.name || value.email || "").trim() || null, cookie }];
    }
    const cookie = cookieToHeaderString(value);
    if (!cookie) throw new Error("cookie 内容为空");
    return [{ name: null, cookie }];
  }

  function parseCookieFilesToItems(files) {
    const items = [];
    const errors = [];
    const list = Array.isArray(files) ? files : [];

    list.forEach((file) => {
      const fileName = String((file && file.name) || "").trim();
      const baseName = fileName.replace(/\.(json|txt)$/i, "").trim();
      const raw = String((file && file.text) || "");
      if (!raw.trim()) {
        errors.push({ file: fileName, error: "文件内容为空" });
        return;
      }

      let parsed = raw;
      try {
        parsed = JSON.parse(raw);
      } catch (_) {
        parsed = raw;
      }

      try {
        const fileItems = toCookieBatchItems(parsed);
        if (!fileItems.length) {
          errors.push({ file: fileName, error: "未找到可导入的 Cookie" });
          return;
        }
        fileItems.forEach((item, idx) => {
          const fallback = fileItems.length > 1 ? `${baseName}-${idx + 1}` : baseName;
          items.push({
            name: item.name || fallback || null,
            cookie: item.cookie,
          });
        });
      } catch (err) {
        errors.push({ file: fileName, error: (err && err.message) || "解析失败" });
      }
    });

    return { items, errors };
  }

  function collectRetryItems(items, results) {
    const itemList = Array.isArray(items) ? items : [];
    const resultList = Array.isArray(results) ? results : [];
    return itemList.filter((_, idx) => {
      const result = resultList[idx];
      return !result || result.error != null;
    });
  }

  return {
    collectRetryItems,
    cookieToHeaderString,
    parseCookieFilesToItems,
    toCookieBatchItems,
  };
});
