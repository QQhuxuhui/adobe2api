(function initializeAdminTokenFilters(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root) {
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
