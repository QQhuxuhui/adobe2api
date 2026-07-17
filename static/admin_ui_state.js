(function initializeAdminUiState(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root) {
    root.AdminUiState = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createAdminUiState() {
  "use strict";

  function retainSelectedTokenIds(selectedIds, tokens) {
    const allowedIds = new Set(
      (Array.isArray(tokens) ? tokens : [])
        .map((token) => String(token?.id || "").trim())
        .filter(Boolean),
    );

    return Array.from(selectedIds || [], (id) => String(id || "").trim())
      .filter((id) => id && allowedIds.has(id));
  }

  function createLatestRequestGate() {
    let version = 0;
    return {
      begin() {
        version += 1;
        return version;
      },
      invalidate() {
        version += 1;
      },
      isCurrent(requestVersion) {
        return requestVersion === version;
      },
    };
  }

  async function runLatestRequest(gate, operation, handlers = {}) {
    const requestVersion = gate.begin();
    try {
      const value = await operation();
      if (!gate.isCurrent(requestVersion)) return { status: "stale" };
      if (typeof handlers.onSuccess === "function") handlers.onSuccess(value);
      return { status: "success", value };
    } catch (error) {
      if (!gate.isCurrent(requestVersion)) return { status: "stale" };
      if (typeof handlers.onFailure === "function") handlers.onFailure(error);
      return { status: "failure", error };
    }
  }

  async function fetchTokenList(fetchImpl) {
    const response = await fetchImpl("/api/v1/tokens");
    if (!response?.ok) {
      const status = Number(response?.status || 0);
      throw new Error(`token list request failed: ${status || "unknown"}`);
    }

    const data = await response.json();
    const tokens = Array.isArray(data?.tokens)
      ? data.tokens
      : Array.isArray(data?.items)
        ? data.items
        : [];
    return {
      tokens,
      summary: data?.summary || null,
    };
  }

  function updateInputValue(input, nextValue, onChange) {
    if (!input) return false;
    const value = String(nextValue ?? "");
    if (input.value === value) return false;
    input.value = value;
    if (typeof onChange === "function") onChange();
    return true;
  }

  return {
    createLatestRequestGate,
    fetchTokenList,
    retainSelectedTokenIds,
    runLatestRequest,
    updateInputValue,
  };
});
