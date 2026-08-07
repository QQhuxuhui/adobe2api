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
    if (
      !Number.isFinite(value)
      || !["measured", "estimated", "upstream"].includes(source)
    ) {
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
    if (source === "upstream") {
      // Leonardo 的 Generate 直接回报本次 apiCreditCost：精确单张成本
      return { text: numberText, title: "上游回报(精确)", estimated: false };
    }
    return { text: numberText, title: "", estimated: false };
  }

  function formatCny(value) {
    if (value === null || value === undefined || value === "") return "";
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return "";
    const rounded = Math.round(number * 1000000) / 1000000;
    let text = rounded.toFixed(6);
    text = text.replace(/0+$/, "");
    if (!text.includes(".")) text += ".0000";
    else {
      const digits = text.length - text.indexOf(".") - 1;
      if (digits < 4) text += "0".repeat(4 - digits);
    }
    return `¥${text}`;
  }

  function formatLogCost(costCny, creditType, unitPriceCny, creditsSource) {
    const source = String(creditsSource || "").trim().toLowerCase();
    const provider = String(creditType || "").trim().toLowerCase();
    const costText = formatCny(costCny);
    const priceText = formatCny(unitPriceCny);
    if (
      !costText
      || !priceText
      || !["leonardo", "adobe"].includes(provider)
      || !["measured", "estimated", "upstream"].includes(source)
    ) {
      return unknownCredits();
    }
    const label = provider === "adobe" ? "Adobe" : "Leonardo";
    return {
      text: source === "estimated" ? `~${costText}` : costText,
      title: `${label} 单价 ${priceText}/积分`,
      estimated: source === "estimated",
    };
  }

  return { formatLogCredits, formatLogCost };
});
