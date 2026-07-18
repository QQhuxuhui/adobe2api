document.addEventListener("DOMContentLoaded", async () => {
  const rawFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await rawFetch(...args);
    if (res.status === 401) {
      window.location.href = "/login";
    }
    return res;
  };

  async function ensureAuthenticated() {
    try {
      const res = await rawFetch("/api/v1/auth/me", { method: "GET" });
      if (!res.ok) {
        window.location.href = "/login";
        return false;
      }
      return true;
    } catch (err) {
      window.location.href = "/login";
      return false;
    }
  }

  if (!(await ensureAuthenticated())) {
    return;
  }

  // Tabs
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");
  const LOGS_POLL_MS = 10000;

  function isLogsTabActive() {
    const logsPane = document.getElementById("logs");
    return Boolean(logsPane && logsPane.classList.contains("active"));
  }

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      tabPanes.forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.target).classList.add("active");
      if (btn.dataset.target === "logs") {
        logsCurrentPage = 1;
        loadLogs();
      } else if (logsAutoTimer) {
        clearTimeout(logsAutoTimer);
        logsAutoTimer = null;
      }
    });
  });

  // Token Management
  const tokenInput = document.getElementById("tokenInput");
  const tokenFile = document.getElementById("tokenFile");
  const addBtn = document.getElementById("addBtn");
  const addMsg = document.getElementById("addMsg");
  const openAddTokenModalBtn = document.getElementById("openAddTokenModalBtn");
  const tokenModal = document.getElementById("tokenModal");
  const tokenModalCloseBtn = document.getElementById("tokenModalCloseBtn");
  const openCookieImportBtn = document.getElementById("openCookieImportBtn");
  const exportTokensBtn = document.getElementById("exportTokensBtn");
  const exportCookiesBtn = document.getElementById("exportCookiesBtn");
  const deleteTokensBatchBtn = document.getElementById("deleteTokensBatchBtn");
  const dedupeByEmailBtn = document.getElementById("dedupeByEmailBtn");
  const refreshModal = document.getElementById("refreshModal");
  const refreshModalCloseBtn = document.getElementById("refreshModalCloseBtn");
  const refreshBtn = document.getElementById("refreshBtn");
  const refreshCreditsBatchBtn = document.getElementById("refreshCreditsBatchBtn");
  const tokenSelectAll = document.getElementById("tokenSelectAll");
  const tbody = document.querySelector("#tokenTable tbody");
  const tokenTotalCount = document.getElementById("tokenTotalCount");
  const tokenActiveCount = document.getElementById("tokenActiveCount");
  const tokenZeroCreditCount = document.getElementById("tokenZeroCreditCount");
  const tokenBrokenCount = document.getElementById("tokenBrokenCount");
  const tokenAutoRefreshFoot = document.getElementById("tokenAutoRefreshFoot");
  const tokenActiveFoot = document.getElementById("tokenActiveFoot");
  const tokenZeroCreditFoot = document.getElementById("tokenZeroCreditFoot");
  const tokenBrokenFoot = document.getElementById("tokenBrokenFoot");
  const creditsChartSub = document.getElementById("creditsChartSub");
  const creditsChartNote = document.getElementById("creditsChartNote");
  const creditsTotalBar = document.getElementById("creditsTotalBar");
  const creditsAvailableBar = document.getElementById("creditsAvailableBar");
  const creditsTotalValue = document.getElementById("creditsTotalValue");
  const creditsAvailableValue = document.getElementById("creditsAvailableValue");
  const tokenPagination = document.getElementById("tokenPagination");
  const tokenPrevBtn = document.getElementById("tokenPrevBtn");
  const tokenNextBtn = document.getElementById("tokenNextBtn");
  const tokenPageInfo = document.getElementById("tokenPageInfo");
  const tokenFilterCards = document.querySelectorAll("[data-token-filter]");
  const {
    getFilteredTokens,
    hasKnownCredits,
    matchesTokenFilter,
    resolveTokenFilter,
  } = window.AdminTokenFilters;
  const {
    createLatestRequestGate,
    fetchTokenList,
    retainSelectedTokenIds,
    runLatestRequest,
    updateInputValue,
  } = window.AdminUiState;
  const {
    parseCookieFilesToItems,
    toCookieBatchItems,
  } = window.AdminCookieImport;
  const { formatLogCredits } = window.AdminLogCredits;
  const tokenSelectedIds = new Set();
  let logsAutoTimer = null;
  let latestTokens = [];
  const TOKENS_PAGE_SIZE = 20;
  let tokenCurrentPage = 1;
  let tokenTotalPages = 1;
  let tokenFilter = null;
  const tokenLoadGate = createLatestRequestGate();

  const STATUS_MAP = {
    "active": "生效中",
    "exhausted": "额度耗尽",
    "invalid": "已失效",
    "error": "请求异常",
    "disabled": "已禁用"
  };

  async function loadTokens() {
    return runLatestRequest(
      tokenLoadGate,
      () => fetchTokenList(fetch),
      {
        onSuccess: ({ tokens, summary }) => renderTable(tokens, summary),
        onFailure: (err) => {
          console.error(err);
          latestTokens = [];
          tokenSelectedIds.clear();
          tokenCurrentPage = 1;
          renderTokenSummary([]);
          renderTokenPagination(0);
          tbody.innerHTML = `<tr><td colspan="9" class="empty-state" style="color: var(--critical);">加载失败</td></tr>`;
          syncTokenSelectAllState();
        },
      },
    );
  }

  function getCurrentPageTokens(tokens = getFilteredTokens(latestTokens, tokenFilter)) {
    const list = Array.isArray(tokens) ? tokens : [];
    const start = (tokenCurrentPage - 1) * TOKENS_PAGE_SIZE;
    return list.slice(start, start + TOKENS_PAGE_SIZE);
  }

  function renderTokenSummary(tokens, summary = null) {
    const list = Array.isArray(tokens) ? tokens : [];
    const total = list.length;
    const active = list.filter((token) => matchesTokenFilter(token, "active")).length;

    let creditsTotalSum = 0;
    let creditsAvailableSum = 0;
    let zeroCredit = 0;
    let unknownCredit = 0;
    let broken = 0;
    let autoRefresh = 0;

    list.forEach((token) => {
      const available = token?.credits_available;
      const capacity = token?.credits_total;
      if (hasKnownCredits(capacity)) creditsTotalSum += Number(capacity);
      if (hasKnownCredits(available)) {
        creditsAvailableSum += Number(available);
      } else {
        unknownCredit += 1;
      }
      if (matchesTokenFilter(token, "zero_credit")) zeroCredit += 1;
      if (matchesTokenFilter(token, "broken")) broken += 1;
      if (token?.auto_refresh) autoRefresh += 1;
    });

    const summaryAvailable = Number(summary?.credits_available_total);
    if (Number.isFinite(summaryAvailable)) creditsAvailableSum = summaryAvailable;

    if (tokenTotalCount) tokenTotalCount.textContent = String(total);
    if (tokenActiveCount) tokenActiveCount.textContent = String(active);
    if (tokenZeroCreditCount) {
      tokenZeroCreditCount.textContent = String(zeroCredit);
      tokenZeroCreditCount.classList.toggle("is-critical", zeroCredit > 0);
    }
    if (tokenBrokenCount) {
      tokenBrokenCount.textContent = String(broken);
      tokenBrokenCount.classList.toggle("is-critical", broken > 0);
    }

    if (tokenAutoRefreshFoot) {
      tokenAutoRefreshFoot.textContent = `自动刷新 ${autoRefresh} 个`;
    }
    if (tokenActiveFoot) {
      const pct = total > 0 ? Math.round((active / total) * 100) : 0;
      tokenActiveFoot.textContent = total > 0 ? `占账号总数 ${pct}%` : "暂无账号";
    }
    if (tokenZeroCreditFoot) {
      tokenZeroCreditFoot.textContent = unknownCredit > 0
        ? `余额为 0，另有 ${unknownCredit} 个未获取`
        : "余额为 0";
    }
    if (tokenBrokenFoot) {
      tokenBrokenFoot.textContent = "失效 / 过期 / 积分异常";
    }

    renderCreditsChart(creditsTotalSum, creditsAvailableSum, unknownCredit);
  }

  function renderCreditsChart(totalCredits, availableCredits, unknownCount = 0) {
    const capacity = Math.max(0, Number(totalCredits) || 0);
    const available = Math.max(0, Math.min(Number(availableCredits) || 0, capacity || Infinity));
    const used = Math.max(0, capacity - available);
    // Both bars share one scale: the total is the 100% reference, the balance is measured against it.
    const availablePct = capacity > 0 ? (available / capacity) * 100 : 0;

    if (creditsTotalBar) creditsTotalBar.style.width = capacity > 0 ? "100%" : "0%";
    if (creditsAvailableBar) {
      creditsAvailableBar.style.width = `${availablePct}%`;
    }
    if (creditsTotalValue) {
      creditsTotalValue.textContent = capacity > 0 ? formatCreditsTotal(capacity) : "-";
    }
    if (creditsAvailableValue) {
      creditsAvailableValue.textContent = capacity > 0 ? formatCreditsTotal(available) : "-";
    }
    if (creditsChartSub) {
      creditsChartSub.textContent = capacity > 0
        ? `已用 ${formatCreditsTotal(used)}（${Math.round((used / capacity) * 100)}%）`
        : "暂无积分数据";
    }
    if (creditsChartNote) {
      creditsChartNote.textContent = unknownCount > 0 ? `${unknownCount} 个账号积分未获取` : "";
    }
  }

  function formatCreditsTotal(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "-";
    const rounded = Math.round(num * 100) / 100;
    return rounded.toLocaleString("zh-CN", {
      minimumFractionDigits: Number.isInteger(rounded) ? 0 : 2,
      maximumFractionDigits: 2,
    });
  }

  function renderTokenPagination(totalCount) {
    const total = Math.max(0, Number(totalCount || 0));
    tokenTotalPages = Math.max(1, Math.ceil(total / TOKENS_PAGE_SIZE));
    tokenCurrentPage = Math.min(Math.max(1, tokenCurrentPage), tokenTotalPages);

    if (tokenPageInfo) {
      tokenPageInfo.textContent = `第 ${tokenCurrentPage} / ${tokenTotalPages} 页`;
    }
    if (tokenPrevBtn) tokenPrevBtn.disabled = tokenCurrentPage <= 1;
    if (tokenNextBtn) tokenNextBtn.disabled = tokenCurrentPage >= tokenTotalPages;
    if (tokenPagination) tokenPagination.style.display = total > TOKENS_PAGE_SIZE ? "flex" : "none";
  }

  function syncTokenFilterCards() {
    tokenFilterCards.forEach((card) => {
      const cardFilter = String(card.dataset.tokenFilter || "") || null;
      const isActive = cardFilter === tokenFilter;
      card.classList.toggle("is-active", isActive);
      card.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function setTokenFilter(requestedFilter) {
    const nextFilter = resolveTokenFilter(tokenFilter, requestedFilter);
    if (nextFilter === tokenFilter) {
      syncTokenFilterCards();
      return;
    }

    tokenFilter = nextFilter;
    tokenCurrentPage = 1;
    tokenSelectedIds.clear();
    syncTokenFilterCards();
    renderTable(latestTokens, null);
  }

  tokenFilterCards.forEach((card) => {
    card.addEventListener("click", () => {
      const requestedFilter = String(card.dataset.tokenFilter || "") || null;
      setTokenFilter(requestedFilter);
    });
  });

  function syncTokenSelectAllState() {
    if (!tokenSelectAll) return;
    const tokenIds = getCurrentPageTokens().map((t) => String(t.id || "")).filter(Boolean);
    const selectedCount = tokenIds.filter((id) => tokenSelectedIds.has(id)).length;
    const total = tokenIds.length;
    if (total === 0) {
      tokenSelectAll.indeterminate = false;
      tokenSelectAll.checked = false;
      return;
    }
    tokenSelectAll.indeterminate = selectedCount > 0 && selectedCount < total;
    tokenSelectAll.checked = total > 0 && selectedCount === total;
  }

  function openDialog(modalEl) {
    if (!modalEl) return;
    modalEl.classList.add("open");
    modalEl.setAttribute("aria-hidden", "false");
  }

  function closeDialog(modalEl) {
    if (!modalEl) return;
    modalEl.classList.remove("open");
    modalEl.setAttribute("aria-hidden", "true");
  }

  function formatExpiry(token) {
    if (!token || token.expires_at == null) {
      return '<span class="expiry-sub">未知</span>';
    }
    const remain = Number(token.remaining_seconds || 0);
    const abs = Math.abs(remain);
    const days = Math.floor(abs / 86400);
    const hours = Math.floor((abs % 86400) / 3600);
    const mins = Math.floor((abs % 3600) / 60);
    const rel = days > 0 ? `${days}天${hours}小时` : `${hours}小时${mins}分`;
    const stampSafe = escapeHtml(token.expires_at_text || "-");
    if (remain <= 0 || token.is_expired) {
      return `<span class="expiry-gone">已过期</span><br><span class="expiry-sub">${stampSafe}</span>`;
    }
    const cls = remain < 3600 * 6 ? "expiry-soon" : "expiry-ok";
    return `<span class="${cls}">剩余 ${rel}</span><br><span class="expiry-sub">${stampSafe}</span>`;
  }

  function formatCredits(token) {
    const available = Number(token?.credits_available);
    const total = Number(token?.credits_total);
    const availableUntil = String(token?.credits_available_until || "").trim();
    const err = String(token?.credits_error || "").trim();

    if (err) {
      return `<div class="credit-meter-error">刷新失败</div><div class="credit-meter-foot">${escapeHtml(truncateText(err, 40))}</div>`;
    }
    if (!Number.isFinite(available) || !Number.isFinite(total) || total <= 0) {
      return `<span class="credit-meter-empty">未获取</span>`;
    }

    const safeAvailable = Math.max(0, Math.min(available, total));
    const pct = (safeAvailable / total) * 100;
    // Meter fill carries severity; the track is a lighter step of the same ramp.
    const severity = safeAvailable <= 0 ? "is-critical" : pct <= 20 ? "is-warning" : "";
    const resetText = availableUntil
      ? new Date(availableUntil).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })
      : "";

    return `
      <div class="credit-meter">
        <div class="credit-meter-head">
          <span class="credit-meter-value">${formatCreditsTotal(safeAvailable)} / ${formatCreditsTotal(total)}</span>
          <span class="credit-meter-pct">${Math.round(pct)}%</span>
        </div>
        <div class="credit-meter-track ${severity}">
          <div class="credit-meter-fill ${severity}" style="width: ${pct}%;"></div>
        </div>
        ${resetText ? `<div class="credit-meter-foot">重置 ${escapeHtml(resetText)}</div>` : ""}
      </div>
    `;
  }

  function renderTable(tokens, summary = null) {
    latestTokens = Array.isArray(tokens) ? tokens : [];
    renderTokenSummary(latestTokens, summary);
    syncTokenFilterCards();

    const filteredTokens = getFilteredTokens(latestTokens, tokenFilter);
    const retainedSelectedIds = new Set(
      retainSelectedTokenIds(tokenSelectedIds, filteredTokens),
    );
    Array.from(tokenSelectedIds).forEach((id) => {
      if (!retainedSelectedIds.has(id)) tokenSelectedIds.delete(id);
    });

    renderTokenPagination(filteredTokens.length);
    const pageTokens = getCurrentPageTokens(filteredTokens);

    if (!latestTokens.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">当前没有可用的 Token，请在上方添加。</td></tr>`;
      syncTokenSelectAllState();
      return;
    }

    if (!filteredTokens.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">该筛选下暂无账号</td></tr>`;
      syncTokenSelectAllState();
      return;
    }

    tbody.innerHTML = "";
    pageTokens.forEach(t => {
      const tr = document.createElement("tr");
      const tokenId = String(t.id || "").trim();
      const selectedAttr = tokenSelectedIds.has(tokenId) ? "checked" : "";

      const statusClass = `status-${t.status.toLowerCase()}`;
      const isStatusActive = t.status === "active";
      const isFrozen = t.status === "exhausted" || t.status === "invalid";
      const displayStatus = STATUS_MAP[t.status.toLowerCase()] || t.status;
      const tokenProfileName = String(t.refresh_profile_name || "").trim();
      const tokenProfileEmail = String(t.refresh_profile_email || "").trim();
      const refreshProfileNameSafe = escapeHtml(tokenProfileName);
      const refreshProfileEmailSafe = escapeHtml(tokenProfileEmail);
      const accountName = refreshProfileNameSafe
        ? `<span class="account-name">${refreshProfileNameSafe}</span>`
        : '<span class="account-name">手动 Token</span>';
      const accountEmail = refreshProfileEmailSafe
        ? `<span class="account-email">${refreshProfileEmailSafe}</span>`
        : '<span class="account-meta">无绑定邮箱</span>';
      const autoEnabled = t.auto_refresh && t.auto_refresh_enabled !== false;
      const autoRefreshCell = t.auto_refresh
        ? `<div style="display: flex; align-items: center;"><button class="switch-btn ${autoEnabled ? "on" : "off"}" onclick="toggleAutoRefresh('${t.id}', ${autoEnabled ? "false" : "true"})" title="${autoEnabled ? "点击关闭自动刷新" : "点击开启自动刷新"}"><span class="switch-knob"></span></button><span class="switch-text">${autoEnabled ? "开启" : "关闭"}</span></div>`
        : `<div style="display: flex; align-items: center;"><button class="switch-btn off" disabled title="手动 token 不支持自动刷新"><span class="switch-knob"></span></button><span class="switch-text">手动</span></div>`;
      
      const d = new Date(t.added_at * 1000);
      const dateStr = d.toLocaleString();
      const importedAtText = String(t.refresh_profile_imported_at_text || "").trim();
      const importedLine = importedAtText
        ? `<br><span class="account-meta">导入 ${escapeHtml(importedAtText)}</span>`
        : "";

      const refreshTokenBtn = t.auto_refresh
        ? `<button class="action-mini" onclick="refreshToken('${t.id}')">刷新Token</button>`
        : `<button class="action-mini" disabled title="仅自动刷新 token 支持刷新">刷新Token</button>`;
      const statusBtn = isFrozen
        ? `<button class="action-mini" disabled title="额度耗尽或已失效 token 不可启用">不可启用</button>`
        : `<button class="action-mini" onclick="toggleToken('${t.id}', '${isStatusActive ? 'disabled' : 'active'}')">${isStatusActive ? '禁用Token' : '启用Token'}</button>`;
      const actionsGrid = `
        <div class="action-btns">
          <button class="action-mini" onclick="refreshTokenCredits('${t.id}')">刷新积分</button>
          ${refreshTokenBtn}
          ${statusBtn}
          <button class="action-mini danger" onclick="deleteToken('${t.id}')">删除Token</button>
        </div>
      `;

      tr.innerHTML = `
        <td><input type="checkbox" class="token-select" data-id="${tokenId}" ${selectedAttr} /></td>
        <td title="添加时间: ${dateStr}">${accountName}<br>${accountEmail}${importedLine}</td>
        <td class="token-val">${t.value}</td>
        <td><span class="status-badge ${statusClass}">${displayStatus}</span></td>
        <td>${autoRefreshCell}</td>
        <td>${formatCredits(t)}</td>
        <td class="${t.fails > 0 ? "expiry-gone" : ""}">${t.fails}</td>
        <td style="line-height:1.4;">${formatExpiry(t)}</td>
        <td>${actionsGrid}</td>
      `;
      tbody.appendChild(tr);
    });
    syncTokenSelectAllState();
  }

  addBtn.addEventListener("click", async () => {
    let tokens = [];
    try {
      tokens = await collectTokensFromInputs();
    } catch (err) {
      showMsg(addMsg, err.message || "文件解析失败", true);
      return;
    }

    if (!tokens.length) {
      showMsg(addMsg, "请先输入 Token 内容或上传文件", true);
      return;
    }

    addBtn.disabled = true;
    try {
      const endpoint = tokens.length > 1 ? "/api/v1/tokens/batch" : "/api/v1/tokens";
      const payload = tokens.length > 1 ? { tokens } : { token: tokens[0] };
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        tokenInput.value = "";
        if (tokenFile) tokenFile.value = "";
        if (tokens.length > 1) {
          const data = await res.json();
          const addedCount = Number(data?.added_count || 0);
          showMsg(addMsg, `批量添加成功（${addedCount} 个）`, false);
        } else {
          showMsg(addMsg, "添加成功", false);
        }
        loadTokens();
        closeDialog(tokenModal);
      } else {
        let detail = "添加失败，请重试";
        try {
          const body = await res.json();
          detail = body.detail || detail;
        } catch (_) {
          // ignore json parse errors
        }
        showMsg(addMsg, detail, true);
      }
    } catch (err) {
      showMsg(addMsg, err.message, true);
    }
    addBtn.disabled = false;
  });

  refreshBtn.addEventListener("click", async () => {
    showToast("Token 列表刷新中...", false, { duration: 0 });
    try {
      const loadResult = await loadTokens();
      if (loadResult.status === "success") {
        showToast("Token 列表已刷新", false);
      } else if (loadResult.status === "failure") {
        showToast("Token 列表刷新失败", true);
      }
    } catch (err) {
      showToast("Token 列表刷新失败", true);
    }
  });

  if (tokenSelectAll) {
    tokenSelectAll.addEventListener("change", () => {
      const checked = Boolean(tokenSelectAll.checked);
      const pageTokens = getCurrentPageTokens();
      if (checked) {
        pageTokens.forEach((t) => {
          const tid = String(t.id || "").trim();
          if (tid) tokenSelectedIds.add(tid);
        });
      } else {
        pageTokens.forEach((t) => {
          const tid = String(t.id || "").trim();
          if (tid) tokenSelectedIds.delete(tid);
        });
      }
      tbody.querySelectorAll("input.token-select").forEach((el) => {
        el.checked = checked;
      });
      syncTokenSelectAllState();
    });
  }

  if (tbody) {
    tbody.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (!target.classList.contains("token-select")) return;
      const tid = String(target.dataset.id || "").trim();
      if (!tid) return;
      if (target.checked) tokenSelectedIds.add(tid);
      else tokenSelectedIds.delete(tid);
      syncTokenSelectAllState();
    });
  }

  if (openAddTokenModalBtn) {
    openAddTokenModalBtn.addEventListener("click", () => openDialog(tokenModal));
  }
  if (tokenModalCloseBtn) {
    tokenModalCloseBtn.addEventListener("click", () => closeDialog(tokenModal));
  }
  if (tokenModal) {
    tokenModal.addEventListener("click", (event) => {
      if (event.target === tokenModal) closeDialog(tokenModal);
    });
  }

  if (openCookieImportBtn) {
    openCookieImportBtn.addEventListener("click", async () => {
      openDialog(refreshModal);
      if (cookieInput) cookieInput.focus();
    });
  }
  if (refreshModalCloseBtn) {
    refreshModalCloseBtn.addEventListener("click", () => closeDialog(refreshModal));
  }
  if (refreshModal) {
    refreshModal.addEventListener("click", (event) => {
      if (event.target === refreshModal) closeDialog(refreshModal);
    });
  }

  window.deleteToken = async (id) => {
    if (!confirm("确定要删除这个 Token 吗？")) return;
    try {
      const res = await fetch(`/api/v1/tokens/${id}`, { method: "DELETE" });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || "删除失败");
      }
      await loadTokens();
    } catch (err) {
      alert(err.message || "删除失败");
    }
  };

  window.toggleToken = async (id, newStatus) => {
    try {
      const res = await fetch(`/api/v1/tokens/${id}/status?status=${newStatus}`, { method: "PUT" });
      if (!res.ok) {
        const text = await res.text();
        alert(`状态更新失败: ${text}`);
        return;
      }
      loadTokens();
    } catch (err) {
      alert("状态更新失败");
    }
  };

  window.refreshToken = async (id) => {
    showToast("Token 刷新中...", false, { duration: 0 });
    try {
      const res = await fetch(`/api/v1/tokens/${id}/refresh`, { method: "POST" });
      if (!res.ok) {
        let detail = "刷新失败";
        try {
          const body = await res.json();
          detail = body.detail || JSON.stringify(body);
        } catch (_) {
          detail = await res.text();
        }
        alert(`刷新失败: ${detail || "unknown error"}`);
        showToast(`Token 刷新失败：${detail || "unknown error"}`, true);
        return;
      }
      showMsg(refreshMsg, "刷新成功", false);
      showToast("Token 刷新成功", false);
      await loadTokens();
    } catch (err) {
      alert("刷新失败");
      showToast("Token 刷新失败", true);
    }
  };

  window.refreshTokenCredits = async (id) => {
    showToast("Token 积分刷新中...", false, { duration: 0 });
    try {
      const res = await fetch(`/api/v1/tokens/${id}/credits/refresh`, { method: "POST" });
      if (!res.ok) {
        let detail = "刷新积分失败";
        try {
          const body = await res.json();
          detail = body.detail || JSON.stringify(body);
        } catch (parseError) {
          detail = await res.text();
        }
        alert(detail || "刷新积分失败");
        showToast(`刷新积分失败：${detail || "unknown error"}`, true);
        return;
      }
      showToast("Token 积分刷新成功", false);
    } catch (err) {
      alert("刷新积分失败");
      showToast("Token 积分刷新失败", true);
    } finally {
      await loadTokens();
    }
  };

  window.toggleAutoRefresh = async (id, enabled) => {
    try {
      const res = await fetch(`/api/v1/tokens/${id}/auto-refresh?enabled=${enabled ? "true" : "false"}`, {
        method: "PUT"
      });
      if (!res.ok) {
        let detail = "自动刷新设置失败";
        try {
          const body = await res.json();
          detail = body.detail || JSON.stringify(body);
        } catch (_) {
          detail = await res.text();
        }
        alert(detail || "自动刷新设置失败");
        return;
      }
      await loadTokens();
    } catch (err) {
      alert("自动刷新设置失败");
    }
  };

  if (refreshCreditsBatchBtn) {
    refreshCreditsBatchBtn.addEventListener("click", async () => {
      refreshCreditsBatchBtn.disabled = true;
      showToast("批量刷新积分中...", false, { duration: 0 });
      try {
        const res = await fetch("/api/v1/tokens/credits/refresh-batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!res.ok) {
          let detail = "批量刷新积分失败";
          try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
          } catch (_) {
            detail = await res.text();
          }
          showToast(`批量刷新积分失败：${detail || "unknown error"}`, true);
          return;
        }
        const data = await res.json();
        const ok = Number(data.refreshed_count || 0);
        const fail = Number(data.failed_count || 0);
        showToast(`批量刷新完成：成功 ${ok}，失败 ${fail}`, false);
        await loadTokens();
      } catch (err) {
        showToast("批量刷新积分失败", true);
      } finally {
        refreshCreditsBatchBtn.disabled = false;
      }
    });
  }

  if (deleteTokensBatchBtn) {
    deleteTokensBatchBtn.addEventListener("click", async () => {
      const selectedIds = Array.from(tokenSelectedIds);
      if (!selectedIds.length) {
        alert("请先选择要删除的 Token");
        return;
      }
      if (!confirm(`确定批量删除选中的 ${selectedIds.length} 个 Token 吗？`)) return;

      deleteTokensBatchBtn.disabled = true;
      try {
        const res = await fetch("/api/v1/tokens/delete-batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: selectedIds }),
        });
        if (!res.ok) {
          let detail = "批量删除失败";
          try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
          } catch (_) {
            detail = await res.text();
          }
          throw new Error(detail || "批量删除失败");
        }

        const data = await res.json();
        const deletedIds = Array.isArray(data.deleted_ids) ? data.deleted_ids : [];
        deletedIds.forEach((id) => tokenSelectedIds.delete(String(id || "")));
        await loadTokens();

        const deletedCount = Number(data.deleted_count || 0);
        const missingCount = Number(data.missing_count || 0);
        showToast(
          missingCount > 0
            ? `批量删除完成：成功 ${deletedCount}，未找到 ${missingCount}`
            : `批量删除完成：成功删除 ${deletedCount} 个 Token`,
          false,
          { duration: 5000 }
        );
      } catch (err) {
        alert(err.message || "批量删除失败");
        showToast(err.message || "批量删除失败", true);
      } finally {
        deleteTokensBatchBtn.disabled = false;
      }
    });
  }

  if (dedupeByEmailBtn) {
    dedupeByEmailBtn.addEventListener("click", async () => {
      if (!confirm("将按邮箱对账号去重：同一邮箱仅保留最近导入的账号，其余重复账号及其 Token 将被删除。是否继续？")) return;
      dedupeByEmailBtn.disabled = true;
      showToast("邮箱去重中...", false, { duration: 0 });
      try {
        const res = await fetch("/api/v1/refresh-profiles/dedupe-by-email", {
          method: "POST",
        });
        if (!res.ok) {
          let detail = "邮箱去重失败";
          try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
          } catch (_) {
            detail = await res.text();
          }
          throw new Error(detail || "邮箱去重失败");
        }
        const data = await res.json();
        const removedCount = Number(data.removed_count || 0);
        showToast(
          removedCount > 0
            ? `邮箱去重完成：删除 ${removedCount} 个重复账号`
            : "没有发现重复邮箱的账号",
          false,
          { duration: 5000 }
        );
        await loadTokens();
      } catch (err) {
        showToast(err.message || "邮箱去重失败", true, { duration: 5000 });
      } finally {
        dedupeByEmailBtn.disabled = false;
      }
    });
  }

  if (exportTokensBtn) {
    exportTokensBtn.addEventListener("click", async () => {
      exportTokensBtn.disabled = true;
      try {
        const selectedIds = Array.from(tokenSelectedIds);
        const payload = selectedIds.length ? { ids: selectedIds } : { ids: null };
        const res = await fetch("/api/v1/tokens/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const txt = await res.text();
          throw new Error(txt || "导出 Token 失败");
        }
        const data = await res.json();
        const total = Number(data.total || 0);
        if (total <= 0) {
          alert("没有可导出的 Token");
          return;
        }
        downloadJsonFile(`tokens-export-${nowStamp()}.json`, data);
        alert(`导出成功：${total} 个 Token`);
      } catch (err) {
        alert(err.message || "导出 Token 失败");
      } finally {
        exportTokensBtn.disabled = false;
      }
    });
  }

  if (exportCookiesBtn) {
    exportCookiesBtn.addEventListener("click", async () => {
      exportCookiesBtn.disabled = true;
      try {
        const selectedIds = Array.from(tokenSelectedIds);
        const payload = selectedIds.length ? { ids: selectedIds } : { ids: null };
        const res = await fetch("/api/v1/refresh-profiles/export-cookies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const txt = await res.text();
          throw new Error(txt || "导出 Cookie 失败");
        }
        const data = await res.json();
        const total = Number(data.total || 0);
        if (total <= 0) {
          alert("没有可导出的 Cookie");
          return;
        }
        const output = {
          exported_at: Math.floor(Date.now() / 1000),
          total,
          items: Array.isArray(data.items)
            ? data.items.map((it) => ({
                id: it.id,
                name: it.name,
                cookie: it.cookie,
              }))
            : [],
        };
        downloadJsonFile(`refresh-cookies-export-${nowStamp()}.json`, output);
        alert(`导出成功：${total} 个 Cookie`);
      } catch (err) {
        alert(err.message || "导出 Cookie 失败");
      } finally {
        exportCookiesBtn.disabled = false;
      }
    });
  }

  // Config Management
  const confApiKey = document.getElementById("confApiKey");
  const confAdminUsername = document.getElementById("confAdminUsername");
  const confAdminPassword = document.getElementById("confAdminPassword");
  const confPublicBaseUrl = document.getElementById("confPublicBaseUrl");
  const confUseProxy = document.getElementById("confUseProxy");
  const confProxy = document.getElementById("confProxy");
  const testProxyBtn = document.getElementById("testProxyBtn");
  const proxyTestResult = document.getElementById("proxyTestResult");
  const confGenerateTimeout = document.getElementById("confGenerateTimeout");
  const confGptImageQuality = document.getElementById("confGptImageQuality");
  const confRetryEnabled = document.getElementById("confRetryEnabled");
  const confRetryMaxAttempts = document.getElementById("confRetryMaxAttempts");
  const confRetryBackoffSeconds = document.getElementById("confRetryBackoffSeconds");
  const confRetryOnStatusCodes = document.getElementById("confRetryOnStatusCodes");
  const confRetryOnErrorTypes = document.getElementById("confRetryOnErrorTypes");
  const confTokenRotationStrategy = document.getElementById("confTokenRotationStrategy");
  const confRefreshIntervalHours = document.getElementById("confRefreshIntervalHours");
  const confBatchConcurrency = document.getElementById("confBatchConcurrency");
  const confGeneratedMaxSizeMb = document.getElementById("confGeneratedMaxSizeMb");
  const confGeneratedPruneSizeMb = document.getElementById("confGeneratedPruneSizeMb");
  const generatedUsageInfo = document.getElementById("generatedUsageInfo");
  const configCatBtns = document.querySelectorAll(".config-cat-btn");
  const configCatPanes = document.querySelectorAll(".config-cat-pane");
  const saveConfigBtn = document.getElementById("saveConfigBtn");
  const configMsg = document.getElementById("configMsg");
  const cookieInput = document.getElementById("cookieInput");
  const cookieFile = document.getElementById("cookieFile");
  const importCookieBtn = document.getElementById("importCookieBtn");
  const refreshMsg = document.getElementById("refreshMsg");
  let currentBatchConcurrency = 5;
  const PROXY_TEST_ERROR_MESSAGES = {
    timeout: "连接超时",
    proxy_error: "代理连接失败",
    connection_error: "目标连接失败",
    request_error: "网络请求失败",
  };
  const proxyTestGate = createLatestRequestGate();

  function clearProxyTestResult() {
    if (!proxyTestResult) return;
    proxyTestResult.textContent = "";
    proxyTestResult.classList.remove("is-success", "is-error");
  }

  function invalidateProxyTestResult() {
    proxyTestGate.invalidate();
    clearProxyTestResult();
  }

  function showProxyTestResult(text, isError) {
    if (!proxyTestResult) return;
    proxyTestResult.textContent = text;
    proxyTestResult.classList.toggle("is-success", !isError);
    proxyTestResult.classList.toggle("is-error", isError);
  }
  // Logs
  const logsTbody = document.querySelector("#logsTable tbody");
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const clearLogsBtn = document.getElementById("clearLogsBtn");
  const logStatsRange = document.getElementById("logStatsRange");
  const logStatsUpdatedAt = document.getElementById("logStatsUpdatedAt");
  const logsStatsImageCount = document.getElementById("logsStatsImageCount");
  const logsStatsVideoCount = document.getElementById("logsStatsVideoCount");
  const logsStatsTotalCount = document.getElementById("logsStatsTotalCount");
  const logsStatsFailCount = document.getElementById("logsStatsFailCount");
  const logsPrevBtn = document.getElementById("logsPrevBtn");
  const logsNextBtn = document.getElementById("logsNextBtn");
  const logsPageInfo = document.getElementById("logsPageInfo");
  const previewModal = document.getElementById("previewModal");
  const previewContent = document.getElementById("previewContent");
  const previewCloseBtn = document.getElementById("previewCloseBtn");
  const previewDownloadBtn = document.getElementById("previewDownloadBtn");
  const errorDetailModal = document.getElementById("errorDetailModal");
  const errorDetailCode = document.getElementById("errorDetailCode");
  const errorDetailContent = document.getElementById("errorDetailContent");
  const errorDetailCloseBtn = document.getElementById("errorDetailCloseBtn");
  const appToast = document.getElementById("appToast");
  const LOGS_PAGE_SIZE = 20;
  let logsCurrentPage = 1;
  let logsTotalPages = 1;
  let logsRunningTotal = 0;

  function switchConfigPane(targetId) {
    if (!targetId) return;
    configCatBtns.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.target === targetId);
    });
    configCatPanes.forEach((pane) => {
      pane.classList.toggle("active", pane.id === targetId);
    });
  }

  configCatBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      switchConfigPane(String(btn.dataset.target || ""));
    });
  });

  if (configCatBtns.length > 0) {
    const currentActive = Array.from(configCatBtns).find((btn) =>
      btn.classList.contains("active")
    );
    switchConfigPane(
      String(currentActive?.dataset?.target || configCatBtns[0]?.dataset?.target || "")
    );
  }

  if (confProxy) {
    confProxy.addEventListener("input", invalidateProxyTestResult);
  }

  if (testProxyBtn) {
    testProxyBtn.addEventListener("click", async () => {
      const requestVersion = proxyTestGate.begin();
      const originalText = testProxyBtn.textContent || "测试连通性";
      testProxyBtn.disabled = true;
      testProxyBtn.setAttribute("aria-busy", "true");
      testProxyBtn.textContent = "测试中...";
      clearProxyTestResult();
      try {
        const res = await fetch("/api/v1/config/test-proxy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proxy: String(confProxy?.value || "").trim() }),
        });
        const data = await res.json();
        if (!proxyTestGate.isCurrent(requestVersion)) return;
        if (!res.ok) {
          const detail = typeof data.detail === "string" ? data.detail : "代理地址无效";
          throw new Error(detail);
        }

        const latency = Math.max(0, Math.round(Number(data.latency_ms || 0)));
        if (data.ok) {
          const routeText = data.via === "proxy" ? "经代理" : "直连";
          showProxyTestResult(
            `连通（HTTP ${Number(data.status_code)}，${latency}ms，${routeText}）`,
            false,
          );
          return;
        }

        const message = PROXY_TEST_ERROR_MESSAGES[data.error] || "连接失败";
        showProxyTestResult(`${message}（${latency}ms）`, true);
      } catch (err) {
        if (!proxyTestGate.isCurrent(requestVersion)) return;
        showProxyTestResult(err.message || "代理测试失败", true);
      } finally {
        testProxyBtn.disabled = false;
        testProxyBtn.setAttribute("aria-busy", "false");
        testProxyBtn.textContent = originalText;
      }
    });
  }

  async function loadConfig() {
    try {
      const res = await fetch("/api/v1/config");
      if (res.ok) {
        const data = await res.json();
        confApiKey.value = data.api_key || "";
        confAdminUsername.value = data.admin_username || "admin";
        confAdminPassword.value = data.admin_password || "admin";
        confPublicBaseUrl.value = data.public_base_url || "";
        confUseProxy.checked = data.use_proxy || false;
        updateInputValue(confProxy, data.proxy || "", invalidateProxyTestResult);
        confGenerateTimeout.value = Number(data.generate_timeout || 300);
        confGptImageQuality.value = String(data.gpt_image_quality || "low");
        confRetryEnabled.checked = Boolean(data.retry_enabled ?? true);
        confRetryMaxAttempts.value = Number(data.retry_max_attempts || 3);
        confRetryBackoffSeconds.value = Number(data.retry_backoff_seconds ?? 1.0);
        confRetryOnStatusCodes.value = Array.isArray(data.retry_on_status_codes)
          ? data.retry_on_status_codes.join(",")
          : "429,451,500,502,503,504";
        confRetryOnErrorTypes.value = Array.isArray(data.retry_on_error_types)
          ? data.retry_on_error_types.join(",")
          : "timeout,connection,proxy";
        confTokenRotationStrategy.value = String(data.token_rotation_strategy || "round_robin");
        confRefreshIntervalHours.value = Number(data.refresh_interval_hours || 15);
        currentBatchConcurrency = Math.max(1, Math.min(100, Number(data.batch_concurrency || 5)));
        confBatchConcurrency.value = currentBatchConcurrency;
        confGeneratedMaxSizeMb.value = Number(data.generated_max_size_mb || 1024);
        confGeneratedPruneSizeMb.value = Number(data.generated_prune_size_mb || 200);
        if (generatedUsageInfo) {
          const usageMb = Number(data.generated_usage_mb || 0);
          const fileCount = Number(data.generated_file_count || 0);
          generatedUsageInfo.textContent = `当前占用：${Number.isFinite(usageMb) ? usageMb : 0} MB（${Number.isFinite(fileCount) ? fileCount : 0} 个文件）`;
        }
      }
    } catch (err) {
      console.error("加载配置失败", err);
    }
  }

  saveConfigBtn.addEventListener("click", async () => {
    saveConfigBtn.disabled = true;
    try {
      // 保留未在此页面显示的配置项
      const currentRes = await fetch("/api/v1/config");
      const currentData = await currentRes.json();
      
      const payload = {
        ...currentData,
        api_key: confApiKey.value.trim(),
        admin_username: confAdminUsername.value.trim() || "admin",
        admin_password: confAdminPassword.value || "admin",
        public_base_url: confPublicBaseUrl.value.trim(),
        use_proxy: confUseProxy.checked,
        proxy: confProxy.value.trim(),
        generate_timeout: Math.max(1, Number(confGenerateTimeout.value || 300)),
        gpt_image_quality: String(confGptImageQuality.value || "low").trim().toLowerCase() || "low",
        retry_enabled: confRetryEnabled.checked,
        retry_max_attempts: Math.max(1, Math.min(10, Number(confRetryMaxAttempts.value || 3))),
        retry_backoff_seconds: Math.max(0, Math.min(30, Number(confRetryBackoffSeconds.value || 1))),
        retry_on_status_codes: String(confRetryOnStatusCodes.value || "")
          .split(",")
          .map(s => Number(String(s).trim()))
          .filter(n => Number.isInteger(n) && n >= 100 && n <= 599),
        retry_on_error_types: String(confRetryOnErrorTypes.value || "")
          .split(",")
          .map(s => String(s).trim().toLowerCase())
          .filter(Boolean),
        token_rotation_strategy: String(confTokenRotationStrategy.value || "round_robin").trim() || "round_robin",
        refresh_interval_hours: Number(confRefreshIntervalHours.value || 15),
        batch_concurrency: Math.max(1, Math.min(100, Number(confBatchConcurrency.value || 5))),
        generated_max_size_mb: Math.max(100, Math.min(102400, Number(confGeneratedMaxSizeMb.value || 1024))),
        generated_prune_size_mb: Math.max(10, Math.min(10240, Number(confGeneratedPruneSizeMb.value || 200))),
      };

      if (!payload.admin_username) {
        throw new Error("管理员账号不能为空");
      }
      if (!payload.admin_password) {
        throw new Error("管理员密码不能为空");
      }

      if (!Number.isInteger(payload.refresh_interval_hours) || payload.refresh_interval_hours < 1 || payload.refresh_interval_hours > 24) {
        throw new Error("自动刷新间隔必须是 1-24 的整数小时");
      }
      if (!["low", "medium", "high"].includes(payload.gpt_image_quality)) {
        throw new Error("GPT Image 默认质量必须是 low、medium 或 high");
      }
      if (!Number.isInteger(payload.batch_concurrency) || payload.batch_concurrency < 1 || payload.batch_concurrency > 100) {
        throw new Error("批量导入/积分并发数必须是 1-100 的整数");
      }
      if (!Number.isInteger(payload.generated_max_size_mb) || payload.generated_max_size_mb < 100 || payload.generated_max_size_mb > 102400) {
        throw new Error("生成文件空间上限必须是 100-102400 的整数 MB");
      }
      if (!Number.isInteger(payload.generated_prune_size_mb) || payload.generated_prune_size_mb < 10 || payload.generated_prune_size_mb > 10240) {
        throw new Error("触发后清理量必须是 10-10240 的整数 MB");
      }
      if (payload.generated_prune_size_mb >= payload.generated_max_size_mb) {
        throw new Error("触发后清理量必须小于生成文件空间上限");
      }
      if (!Number.isInteger(payload.retry_max_attempts) || payload.retry_max_attempts < 1 || payload.retry_max_attempts > 10) {
        throw new Error("最大尝试次数必须是 1-10 的整数");
      }
      if (!Number.isFinite(payload.retry_backoff_seconds) || payload.retry_backoff_seconds < 0 || payload.retry_backoff_seconds > 30) {
        throw new Error("重试退避基数必须是 0-30 的数字");
      }
      if (!["round_robin", "random"].includes(payload.token_rotation_strategy)) {
        throw new Error("Token 轮换策略无效");
      }

      const res = await fetch("/api/v1/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        showMsg(configMsg, "配置已保存", false);
        showToast("配置已保存", false);
        await loadConfig();
      } else {
        showMsg(configMsg, "保存失败，请检查服务状态", true);
        showToast("保存失败，请检查服务状态", true);
      }
    } catch (err) {
      showMsg(configMsg, err.message, true);
      showToast(err.message || "保存失败", true);
    }
    saveConfigBtn.disabled = false;
  });

  function formatTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleString();
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function truncateText(value, maxLen) {
    const text = String(value || "");
    if (text.length <= maxLen) return text;
    return `${text.slice(0, maxLen)}...`;
  }

  function parseTokenJsonPayload(value) {
    if (Array.isArray(value)) {
      return value.map((v) => String(v || "").trim()).filter(Boolean);
    }
    if (value && typeof value === "object") {
      if (Array.isArray(value.tokens)) {
        return value.tokens.map((v) => String(v || "").trim()).filter(Boolean);
      }
      if (typeof value.token === "string") {
        const single = value.token.trim();
        return single ? [single] : [];
      }
    }
    return [];
  }

  async function collectTokensFromInputs() {
    const textTokens = String(tokenInput?.value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);

    const fileList = Array.from(tokenFile?.files || []);
    const fileTokens = [];
    for (const file of fileList) {
      const raw = await file.text();
      const trimmed = String(raw || "").trim();
      if (!trimmed) continue;

      const lowerName = String(file.name || "").toLowerCase();
      if (lowerName.endsWith(".json")) {
        let parsed;
        try {
          parsed = JSON.parse(trimmed);
        } catch (_) {
          throw new Error(`文件 ${file.name} 不是有效 JSON`);
        }
        const parsedTokens = parseTokenJsonPayload(parsed);
        if (!parsedTokens.length) {
          throw new Error(`文件 ${file.name} 未找到可用 token`);
        }
        fileTokens.push(...parsedTokens);
        continue;
      }

      fileTokens.push(
        ...trimmed
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean)
      );
    }

    const unique = [];
    const seen = new Set();
    for (const token of [...textTokens, ...fileTokens]) {
      const key = String(token || "").trim();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      unique.push(key);
    }
    return unique;
  }

  function downloadJsonFile(filename, payload) {
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json;charset=utf-8"
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function nowStamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  async function importCookies() {
    const text = String(cookieInput?.value || "").trim();
    if (!text) {
      showMsg(refreshMsg, "请先粘贴或上传 Cookie", true);
      return;
    }

    let items = [];
    try {
      let parsed = text;
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        parsed = text;
      }
      items = toCookieBatchItems(parsed);
    } catch (err) {
      showMsg(refreshMsg, err.message || "Cookie 解析失败", true);
      return;
    }

    if (!items.length) {
      showMsg(refreshMsg, "未找到可导入的 Cookie", true);
      return;
    }

    const batchLimit = Math.max(1, Math.min(100, Number(confBatchConcurrency?.value || currentBatchConcurrency || 5)));
    try {
      if (importCookieBtn) importCookieBtn.disabled = true;
      const workerCount = Math.min(batchLimit, items.length);
      const progress = {
        total: items.length,
        completed: 0,
        imported: 0,
        deduped: 0,
        failed: 0,
        refreshFailed: 0,
      };
      const results = new Array(items.length);

      const updateImportProgress = () => {
        showMsg(
          refreshMsg,
          `已解析 ${progress.total} 个 Cookie，处理中 ${progress.completed}/${progress.total}，导入成功 ${progress.imported}（含去重更新 ${progress.deduped}），导入失败 ${progress.failed}，刷新失败 ${progress.refreshFailed}（并行 ${workerCount} 个）...`,
          progress.failed > 0 || progress.refreshFailed > 0,
          { duration: 0 }
        );
      };

      const importOne = async (item) => {
        const res = await fetch("/api/v1/refresh-profiles/import-cookie", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cookie: item.cookie, name: item.name || null }),
        });
        if (!res.ok) {
          let detailText = "Cookie 导入失败";
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detailText = body.detail;
          } catch (_) {
            const txt = await res.text();
            if (txt) detailText = txt;
          }
          throw new Error(detailText);
        }
        return res.json();
      };

      updateImportProgress();
      let nextIndex = 0;
      const runWorker = async () => {
        while (true) {
          const currentIndex = nextIndex;
          nextIndex += 1;
          if (currentIndex >= items.length) return;

          try {
            const result = await importOne(items[currentIndex]);
            results[currentIndex] = result;
            progress.imported += 1;
            if (String(result?.profile?.import_action || "") === "updated") {
              progress.deduped += 1;
            }
            if (String(result.refresh_error || "").trim()) {
              progress.refreshFailed += 1;
            }
          } catch (err) {
            results[currentIndex] = { error: err };
            progress.failed += 1;
          } finally {
            progress.completed += 1;
            updateImportProgress();
          }
        }
      };

      await Promise.all(Array.from({ length: workerCount }, () => runWorker()));

      if (items.length > 1) {
        showMsg(
          refreshMsg,
          `批量 Cookie 导入完成：成功 ${progress.imported}（含去重更新 ${progress.deduped}），导入失败 ${progress.failed}，刷新失败 ${progress.refreshFailed}`,
          progress.failed > 0 || progress.refreshFailed > 0,
          { duration: 8000 }
        );
      } else {
        const singleResult = results[0];
        const refreshError = String(singleResult?.refresh_error || "").trim();
        if (singleResult?.error) {
          throw singleResult.error;
        }
        const dedupNote = String(singleResult?.profile?.import_action || "") === "updated"
          ? "（邮箱已存在，已更新原账号）"
          : "";
        if (refreshError) {
          showMsg(refreshMsg, `Cookie 导入成功${dedupNote}，但自动刷新失败：${refreshError}`, true, { duration: 8000 });
        } else {
          showMsg(refreshMsg, `Cookie 导入成功${dedupNote}，并已自动刷新`, false, { duration: 8000 });
        }
      }
      if (cookieInput) cookieInput.value = "";
      if (cookieFile) cookieFile.value = "";
      await loadTokens();
    } catch (err) {
      showMsg(refreshMsg, err.message || "Cookie 导入失败", true, { duration: 8000 });
    } finally {
      if (importCookieBtn) importCookieBtn.disabled = false;
    }
  }

  async function handleCookieFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    try {
      if (files.length === 1) {
        const text = await files[0].text();
        if (cookieInput) cookieInput.value = text;
        showMsg(refreshMsg, `已读取 1 个文件：${files[0].name}`, false, { duration: 5000 });
        return;
      }

      const entries = [];
      for (const file of files) {
        entries.push({ name: file.name, text: await file.text() });
      }
      const { items, errors } = parseCookieFilesToItems(entries);
      if (cookieInput) {
        cookieInput.value = items.length ? JSON.stringify(items, null, 2) : "";
      }
      const errorNote = errors.length
        ? `，${errors.length} 个文件解析失败：${errors.map((e) => `${e.file}（${e.error}）`).join("、")}`
        : "";
      showMsg(
        refreshMsg,
        `已读取 ${files.length} 个文件，解析出 ${items.length} 个账号${errorNote}`,
        errors.length > 0,
        { duration: 8000 }
      );
    } catch (err) {
      showMsg(refreshMsg, "读取 Cookie 文件失败", true);
    }
  }

  if (cookieFile) {
    cookieFile.addEventListener("change", () => handleCookieFiles(cookieFile.files));
  }

  const refreshModalOverlay = document.getElementById("refreshModal");
  const refreshModalCard = refreshModalOverlay ? refreshModalOverlay.querySelector(".dialog-card") : null;
  if (refreshModalOverlay && refreshModalCard) {
    refreshModalOverlay.addEventListener("dragover", (e) => {
      e.preventDefault();
      refreshModalCard.classList.add("drag-over");
    });
    refreshModalOverlay.addEventListener("dragleave", (e) => {
      if (e.relatedTarget && refreshModalOverlay.contains(e.relatedTarget)) return;
      refreshModalCard.classList.remove("drag-over");
    });
    refreshModalOverlay.addEventListener("drop", (e) => {
      e.preventDefault();
      refreshModalCard.classList.remove("drag-over");
      const files = e.dataTransfer ? e.dataTransfer.files : null;
      if (files && files.length) {
        if (cookieFile) cookieFile.value = "";
        handleCookieFiles(files);
      }
    });
  }

  if (importCookieBtn) importCookieBtn.addEventListener("click", importCookies);
  // profile operation handlers are attached as window methods above.

  async function loadLogs() {
    if (!logsTbody) return;
    try {
      const rangeValue = logStatsRange ? String(logStatsRange.value || "today") : "today";
      const [runningResult, logsResult, statsResult] = await Promise.allSettled([
        fetch("/api/v1/logs/running?limit=200"),
        fetch(`/api/v1/logs?limit=${LOGS_PAGE_SIZE}&page=${logsCurrentPage}`),
        fetch(`/api/v1/logs/stats?range=${encodeURIComponent(rangeValue)}`),
      ]);

      let runningItems = [];
      if (runningResult.status === "fulfilled" && runningResult.value.ok) {
        const runningData = await runningResult.value.json();
        runningItems = Array.isArray(runningData.items) ? runningData.items : [];
      }

      if (logsResult.status !== "fulfilled" || !logsResult.value.ok) {
        throw new Error("加载日志失败");
      }

      const logsData = await logsResult.value.json();
      logsCurrentPage = Math.max(1, Number(logsData.page || logsCurrentPage || 1));
      logsTotalPages = Math.max(1, Number(logsData.total_pages || 1));
      renderLogsPagination();
      renderLogs(logsData.logs || [], runningItems);

      if (statsResult.status === "fulfilled" && statsResult.value.ok) {
        const statsData = await statsResult.value.json();
        renderLogStats(statsData);
      } else {
        renderLogStats(null);
      }
    } catch (err) {
      logsTbody.innerHTML = `<tr><td colspan="9" class="empty-state" style="color: var(--critical);">${err.message || "日志加载失败"}</td></tr>`;
      logsRunningTotal = 0;
      logsTotalPages = Math.max(1, logsCurrentPage || 1);
      renderLogsPagination();
      renderLogStats(null);
    }
  }

  function renderLogStats(stats) {
    const imageCount = Number(stats?.generated_images || 0);
    const videoCount = Number(stats?.generated_videos || 0);
    const totalCount = Number(stats?.total_requests || 0);
    const failCount = Number(stats?.failed_requests || 0);

    if (logsStatsImageCount) logsStatsImageCount.textContent = String(imageCount);
    if (logsStatsVideoCount) logsStatsVideoCount.textContent = String(videoCount);
    if (logsStatsTotalCount) logsStatsTotalCount.textContent = String(totalCount);
    if (logsStatsFailCount) logsStatsFailCount.textContent = String(failCount);

    if (!logStatsUpdatedAt) return;
    if (!stats) {
      logStatsUpdatedAt.textContent = "统计信息暂不可用";
      return;
    }

    const selectedLabel = logStatsRange?.selectedOptions?.[0]?.textContent || "当前范围";
    const endTs = Number(stats.end_ts || 0);
    const updatedText = endTs > 0 ? new Date(endTs * 1000).toLocaleString() : "-";
    logStatsUpdatedAt.textContent = `${selectedLabel}统计，更新于 ${updatedText}`;
  }

  function renderLogsPagination() {
    const safeTotalPages = Math.max(1, Number(logsTotalPages || 1));
    const safeCurrent = Math.min(Math.max(1, Number(logsCurrentPage || 1)), safeTotalPages);
    logsCurrentPage = safeCurrent;
    logsTotalPages = safeTotalPages;

    if (logsPageInfo) {
      logsPageInfo.textContent = `第 ${safeCurrent} / ${safeTotalPages} 页`;
    }
    if (logsPrevBtn) {
      logsPrevBtn.disabled = safeCurrent <= 1;
    }
    if (logsNextBtn) {
      logsNextBtn.disabled = safeCurrent >= safeTotalPages;
    }
  }

  function buildLogRow(item, { forceInProgress = false } = {}) {
    const tr = document.createElement("tr");
    const dt = new Date((item.ts || 0) * 1000);
    const dateText = dt.toLocaleDateString();
    const timeText = dt.toLocaleTimeString();
    const t = Number(item.duration_sec || 0);
    const status = Number(item.status_code || 0);
    const taskStatus = forceInProgress ? "IN_PROGRESS" : String(item.task_status || "").toUpperCase();
    const isFailed = !forceInProgress && status >= 400;
    const isRunning = !isFailed && taskStatus === "IN_PROGRESS";
    const isSuccess = !isRunning && !isFailed;
    const stateClass = isRunning ? "running" : (isFailed ? "failed" : "success");
    const stateLabel = isRunning
      ? "进行中"
      : (isFailed ? `错误 ${status || "-"}` : "已完成");
    const stateIcon = isRunning
      ? `<span class="icon-spinner" aria-hidden="true"></span>`
      : (isFailed
        ? `<span class="icon-error" aria-hidden="true">!</span>`
        : `<span class="icon-check" aria-hidden="true">✓</span>`);
    const errCode = String(item.error_code || "").trim();
    const failedStatusText = status > 0 ? String(status) : "-";
    const failedStateContent = errCode
      ? `<button class="log-state log-state-btn failed" data-error-code="${escapeHtml(errCode)}" type="button">${stateIcon}<span>${escapeHtml(failedStatusText)}</span></button>`
      : `<span class="log-state failed"><span class="icon-error" aria-hidden="true">!</span><span>${escapeHtml(failedStatusText)}</span></span>`;
    const stateContent = isFailed ? failedStateContent : `${stateIcon}<span>${stateLabel}</span>`;
    const statusCell = isFailed ? stateContent : `<span class="log-state ${stateClass}">${stateContent}</span>`;
    const taskProgressRaw = Number(item.task_progress);
    const progressCell = taskStatus === "IN_PROGRESS"
      ? `<span class="status-badge status-active">${Number.isFinite(taskProgressRaw) ? Math.round(taskProgressRaw) : 0}%</span>`
      : `<span class="account-meta">-</span>`;
    const previewUrl = normalizePreviewUrl(String(item.preview_url || "").trim());
    const previewKind = String(item.preview_kind || "").trim();
    const tokenName = String(item.token_account_name || "").trim();
    const tokenEmail = String(item.token_account_email || "").trim();
    const tokenId = String(item.token_id || "").trim();
    const tokenSource = String(item.token_source || "").trim();
    const tokenAttempt = Number(item.token_attempt || 0);
    const tokenTitleParts = [];
    if (tokenName) tokenTitleParts.push(`账号: ${tokenName}`);
    if (tokenId) tokenTitleParts.push(`ID: ${tokenId}`);
    if (tokenSource) tokenTitleParts.push(`来源: ${tokenSource}`);
    if (tokenAttempt > 0) tokenTitleParts.push(`尝试: 第${tokenAttempt}次`);
    const tokenTitle = escapeHtml(tokenTitleParts.join(" | "));
    const accountParts = [];
    accountParts.push(
      tokenEmail
        ? `<span class="log-account-email">${escapeHtml(tokenEmail)}</span>`
        : `<span class="log-account-email">-</span>`
    );
    const modelText = String(item.model || "-");
    const promptText = String(item.prompt_preview || "-");
    const credits = formatLogCredits(item.credits_used, item.credits_source);
    const creditsTitle = credits.title
      ? ` title="${escapeHtml(credits.title)}"`
      : "";
    const tokenCell = `<div class="log-account-cell">${accountParts.join("<br>")}</div>`;
    const previewCell = previewUrl
      ? `<button class="small preview-btn" data-url="${encodeURIComponent(previewUrl)}" data-kind="${previewKind || ""}">查看</button>`
      : `<span class="account-meta">-</span>`;
    tr.innerHTML = `
      <td class="log-time-cell"><span class="date">${dateText}</span><span class="time">${timeText}</span></td>
      <td>${statusCell}</td>
      <td>${t}</td>
      <td>${progressCell}</td>
      <td title="${tokenTitle}">${tokenCell}</td>
      <td class="log-model-cell" title="${escapeHtml(modelText)}">${escapeHtml(modelText)}</td>
      <td class="log-credits-cell${credits.estimated ? " estimated" : ""}"${creditsTitle}>${escapeHtml(credits.text)}</td>
      <td class="log-prompt-cell" title="${escapeHtml(promptText)}">${escapeHtml(promptText)}</td>
      <td>${previewCell}</td>
    `;
    if (isRunning) tr.classList.add("log-row-running");
    return tr;
  }

  function renderLogs(logs, runningItems = []) {
    if (logsAutoTimer) {
      clearTimeout(logsAutoTimer);
      logsAutoTimer = null;
    }
    const runningRows = Array.isArray(runningItems) ? runningItems : [];
    logsRunningTotal = runningRows.length;
    const allRows = [
      ...runningRows,
      ...(Array.isArray(logs) ? logs : []),
    ];

    if (!allRows.length) {
      logsTbody.innerHTML = `<tr><td colspan="9" class="empty-state">暂无请求日志</td></tr>`;
      return;
    }

    logsTbody.innerHTML = "";
    runningRows.forEach((item) => {
      logsTbody.appendChild(buildLogRow(item, { forceInProgress: true }));
    });
    (Array.isArray(logs) ? logs : []).forEach((item) => {
      logsTbody.appendChild(buildLogRow(item));
    });

    if (logsRunningTotal > 0 && isLogsTabActive()) {
      logsAutoTimer = setTimeout(() => {
        if (isLogsTabActive()) loadLogs();
      }, LOGS_POLL_MS);
    }
  }

  function inferPreviewKind(url) {
    const lowered = String(url || "").toLowerCase();
    if (/(\.mp4|\.webm|\.ogg)(\?|$)/.test(lowered)) return "video";
    return "image";
  }

  function normalizePreviewUrl(url) {
    const raw = String(url || "").trim();
    if (!raw) return "";

    if (/^https?:\/\//i.test(raw)) {
      try {
        const u = new URL(raw);
        if (/^\/(generated)\//.test(u.pathname)) {
          return `${window.location.origin}${u.pathname}${u.search || ""}`;
        }
      } catch (_) {
        // ignore parse errors and return original
      }
      return raw;
    }

    if (raw.startsWith("/")) {
      return `${window.location.origin}${raw}`;
    }
    return raw;
  }

  function closePreview() {
    if (!previewModal || !previewContent) return;
    previewModal.classList.remove("open");
    previewModal.setAttribute("aria-hidden", "true");
    previewContent.innerHTML = "";
    if (previewDownloadBtn) {
      previewDownloadBtn.setAttribute("href", "#");
      previewDownloadBtn.setAttribute("download", "");
    }
  }

  function closeErrorDetail() {
    if (!errorDetailModal || !errorDetailContent || !errorDetailCode) return;
    errorDetailModal.classList.remove("open");
    errorDetailModal.setAttribute("aria-hidden", "true");
    errorDetailCode.textContent = "错误信息";
    errorDetailContent.innerHTML = "";
  }

  async function openErrorDetailByCode(code) {
    const errCode = String(code || "").trim();
    if (!errCode || !errorDetailModal || !errorDetailCode || !errorDetailContent) return;
    errorDetailCode.textContent = "错误信息";
    errorDetailContent.innerHTML = `<pre>加载中...</pre>`;
    errorDetailModal.classList.add("open");
    errorDetailModal.setAttribute("aria-hidden", "false");
    try {
      const res = await fetch(`/api/v1/logs/errors/${encodeURIComponent(errCode)}`);
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || `获取错误详情失败 (${res.status})`);
      }
      const data = await res.json();
      const message = String(data?.message || "").trim() || "暂无错误信息";
      errorDetailContent.innerHTML = `<pre>${escapeHtml(message)}</pre>`;
    } catch (err) {
      errorDetailContent.innerHTML = `<pre>${escapeHtml(err.message || "获取错误详情失败")}</pre>`;
    }
  }

  function buildDownloadFilename(url, kind) {
    try {
      const u = new URL(url, window.location.origin);
      const fromPath = (u.pathname.split("/").pop() || "").trim();
      if (fromPath) return fromPath;
    } catch (err) {
      // ignore parse errors and fallback
    }
    const ext = kind === "video" ? "mp4" : "png";
    return `asset-${Date.now()}.${ext}`;
  }

  function openPreview(url, kind) {
    if (!previewModal || !previewContent || !url) return;
    const mediaKind = kind || inferPreviewKind(url);
    if (mediaKind === "video") {
      previewContent.innerHTML = `<video controls autoplay playsinline src="${url}"></video>`;
    } else {
      previewContent.innerHTML = `<img src="${url}" alt="预览图" />`;
    }
    if (previewDownloadBtn) {
      previewDownloadBtn.setAttribute("href", url);
      previewDownloadBtn.setAttribute("download", buildDownloadFilename(url, mediaKind));
    }
    previewModal.classList.add("open");
    previewModal.setAttribute("aria-hidden", "false");
  }

  if (logsTbody) {
    logsTbody.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.classList.contains("preview-btn")) {
        const encodedUrl = target.getAttribute("data-url") || "";
        const kind = (target.getAttribute("data-kind") || "").trim();
        if (!encodedUrl) return;
        openPreview(decodeURIComponent(encodedUrl), kind);
        return;
      }
      const clickableErrorEl = target.closest("[data-error-code]");
      if (clickableErrorEl instanceof HTMLElement) {
        const code = String(clickableErrorEl.getAttribute("data-error-code") || "").trim();
        if (!code) return;
        openErrorDetailByCode(code);
      }
    });
  }

  if (previewCloseBtn) {
    previewCloseBtn.addEventListener("click", closePreview);
  }

  if (previewModal) {
    previewModal.addEventListener("click", (event) => {
      if (event.target === previewModal) closePreview();
    });
  }

  if (errorDetailCloseBtn) {
    errorDetailCloseBtn.addEventListener("click", closeErrorDetail);
  }

  if (errorDetailModal) {
    errorDetailModal.addEventListener("click", (event) => {
      if (event.target === errorDetailModal) closeErrorDetail();
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePreview();
      closeErrorDetail();
      closeDialog(tokenModal);
      closeDialog(refreshModal);
    }
  });

  if (refreshLogsBtn) {
    refreshLogsBtn.addEventListener("click", () => {
      logsCurrentPage = 1;
      loadLogs();
    });
  }

  if (logStatsRange) {
    logStatsRange.addEventListener("change", () => {
      logsCurrentPage = 1;
      loadLogs();
    });
  }

  if (logsPrevBtn) {
    logsPrevBtn.addEventListener("click", () => {
      if (logsCurrentPage <= 1) return;
      logsCurrentPage -= 1;
      loadLogs();
    });
  }

  if (tokenPrevBtn) {
    tokenPrevBtn.addEventListener("click", () => {
      if (tokenCurrentPage <= 1) return;
      tokenCurrentPage -= 1;
      renderTable(latestTokens, null);
    });
  }

  if (tokenNextBtn) {
    tokenNextBtn.addEventListener("click", () => {
      if (tokenCurrentPage >= tokenTotalPages) return;
      tokenCurrentPage += 1;
      renderTable(latestTokens, null);
    });
  }

  if (logsNextBtn) {
    logsNextBtn.addEventListener("click", () => {
      if (logsCurrentPage >= logsTotalPages) return;
      logsCurrentPage += 1;
      loadLogs();
    });
  }

  if (clearLogsBtn) {
    clearLogsBtn.addEventListener("click", async () => {
      if (!confirm("确定清空请求日志吗？")) return;
      try {
        const res = await fetch("/api/v1/logs", { method: "DELETE" });
        if (!res.ok) throw new Error("清空失败");
        logsCurrentPage = 1;
        loadLogs();
      } catch (err) {
        alert(err.message || "清空失败");
      }
    });
  }


  function showMsg(el, text, isError, options = {}) {
    if (!el) return;
    const duration = Number(options?.duration ?? 3000);
    if (el._msgTimer) {
      clearTimeout(el._msgTimer);
      el._msgTimer = null;
    }
    el.textContent = text;
    el.style.color = isError ? "var(--critical)" : "var(--good)";
    if (duration > 0) {
      el._msgTimer = setTimeout(() => {
        el.textContent = "";
        el._msgTimer = null;
      }, duration);
    }
  }

  let toastTimer = null;
  function showToast(text, isError = false, options = {}) {
    if (!appToast) return;
    const duration = Number(options?.duration ?? 2200);
    appToast.textContent = String(text || "").trim();
    appToast.classList.remove("success", "error", "show");
    appToast.classList.add(isError ? "error" : "success");
    appToast.classList.add("show");
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    if (duration > 0) {
      toastTimer = setTimeout(() => {
        appToast.classList.remove("show");
      }, duration);
    }
  }

  // Init
  loadTokens();
  loadConfig();
  renderLogsPagination();
});
