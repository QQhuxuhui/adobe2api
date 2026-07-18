(function initAdminLogCredits(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root && typeof root === "object") {
    root.AdminLogCredits = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createApi() {
  "use strict";

  function unknownCredits() {
    return { text: "-", title: "", estimated: false };
  }

  function formatLogCredits(creditsUsed, creditsSource) {
    if (creditsUsed === null || creditsUsed === undefined || creditsUsed === "") {
      return unknownCredits();
    }
    const value = Number(creditsUsed);
    const source = String(creditsSource || "").trim().toLowerCase();
    if (!Number.isFinite(value) || !["measured", "estimated"].includes(source)) {
      return unknownCredits();
    }
    const rounded = Math.round(value * 1000000) / 1000000;
    const numberText = String(rounded);
    if (source === "estimated") {
      return {
        text: `~${numberText}`,
        title: "估算值(按历史实测)",
        estimated: true,
      };
    }
    return { text: numberText, title: "", estimated: false };
  }

  return { formatLogCredits };
});
