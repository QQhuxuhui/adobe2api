# 管理后台 Token 页三项改进（卡片筛选 / 积分401接入失效 / 代理测试）

日期：2026-07-16
状态：设计已确认

## 背景

管理后台 Token 管理页三个改进点：

1. Token 概览的 4 张统计卡（账号总数 / 生效中 / 无积分账号 / 异常账号）目前纯展示，
   点击无行为；用户希望点卡片筛选出对应账号列表。
2. 部分账号积分区显示「刷新失败 credits request failed: 401」但状态仍「生效中」，
   状态与实际不一致。
3. 系统配置「代理与网络」的代理地址无连通性测试手段。

现状（已核对代码）：

- 表格前端分页渲染 `latestTokens`（`static/admin.js` `renderTable`/`getCurrentPageTokens`），
  统计卡在 `renderTokenSummary` 按全量列表计数。
- 积分刷新 `_fetch_credits_balance`（`core/refresh_mgr.py:694`）非 200 抛
  `RuntimeError("credits request failed: <code>")`，调用方仅 `set_credits_error` 写错误
  文案，**不改 status**。已有 `token_manager.handle_auth_failure`（`core/token_mgr.py:333`）：
  自动号先 `refresh_once` 试 cookie 刷新、超阈值标 `invalid`；手动号直接 `report_invalid`。
- 自动刷新循环（`core/refresh_mgr.py:828-842`）先 cookie 刷新拿到**新** token，再调
  `refresh_credits_for_token_id`——此路径的积分 401 不代表 token 失效。
- 代理配置项 `confProxy`（`static/admin.html:182`），后端有 `_requests_proxies()` 可复用。

## 功能 1：统计卡点击筛选（纯前端）

`static/admin.html` / `static/admin.js` / `static/admin.css`，无后端改动。

- 新增筛选状态变量 `tokenFilter ∈ {null, "active", "zero_credit", "broken"}`（模块级）。
- 4 张卡绑定点击：
  - 账号总数 → `tokenFilter = null`（显示全部 / 清除筛选）；
  - 生效中 → `"active"`；无积分账号 → `"zero_credit"`；异常账号 → `"broken"`；
  - 再次点击当前已激活的卡 → 清除筛选（回到全部）。
- 筛选谓词（与 `renderTokenSummary` 计数口径严格一致）：
  - active：`String(status).toLowerCase() === "active"`；
  - zero_credit：`Number.isFinite(credits_available) && credits_available <= 0`；
  - broken：`status ∈ {invalid, error}` 或 `is_expired`。
- 渲染管线：新增 `getFilteredTokens()`，`renderTokenPagination` 与
  `getCurrentPageTokens` 改用筛选后的列表；切换筛选时 `tokensPage = 1`。
- 统计卡计数、积分图仍基于**全量** `latestTokens`（卡片始终显示总量），只有表格显示子集。
- 可访问性与反馈：被选中的卡加 `.is-active` 高亮；卡片加 `role="button"`、`tabindex="0"`、
  `aria-pressed`，支持回车/空格触发；`cursor: pointer`。
- 空筛选结果：表格显示「该筛选下暂无账号」空态，分页隐藏。

## 功能 2：积分 401/403 接入失效机制（后端）

`core/refresh_mgr.py`、`core/token_mgr.py`（如需）、`api/routes/admin.py` 调用点。

- 新增异常 `CreditsAuthError(RuntimeError)`，携带 `status_code`。
  `_fetch_credits_balance` 遇 **401/403** 抛 `CreditsAuthError`；其余非 200 仍抛原
  `RuntimeError("credits request failed: <code>")`（行为不变）。
- `refresh_credits_for_token_id(token_id, handle_auth: bool = False)` 增加参数：
  - `handle_auth=False`（默认，自动刷新循环用）：捕获 `CreditsAuthError` 时按现状处理
    （向上抛 → 调用方 `set_credits_error`，状态不变）。理由：该路径刚 cookie 刷新过
    token，401 不是 token 死亡信号，且避免重复 `refresh_once`。
  - `handle_auth=True`（手动单个 + 批量刷新按钮用）：捕获 `CreditsAuthError` 时调
    `token_manager.handle_auth_failure(token_value)`：
    - 返回 `"refreshed"`（自动号 cookie 刷新成功）→ 用新 token 值**重取一次**积分；
      成功则 `set_credits` 返回；仍失败则 `set_credits_error`（状态维持，属真实积分问题）；
    - 返回 `"invalid"`（手动号 / 自动号超阈值）→ 状态已被置 `invalid`，`set_credits_error`
      后向上抛，UI 显示失败且状态转「已失效」；
    - 返回 `"retry"`（自动号未超阈值）→ `set_credits_error`，状态不变，向上抛。
- 调用点：
  - `api/routes/admin.py:355` 单个 `refresh_token_credits` → `handle_auth=True`；
  - `api/routes/admin.py:370` 批量 `refresh_tokens_credits_batch` → `handle_auth=True`；
  - `core/refresh_mgr.py:839` 自动刷新循环 → 保持默认 `handle_auth=False`。
- 非 401/403 的积分错误（429/500/网络等）一律维持现状，绝不改状态。

## 功能 3：代理连通性测试（前后端）

后端新端点 `POST /api/v1/config/test-proxy`（`api/routes/admin.py`，管理鉴权）：

- 请求体 `{ "proxy": "<url 或空>" }`（取自前端输入框当前值，未落盘也可测）。
- 行为：用该代理值 `requests.get` 探测 Adobe firefly 主机
  `https://firefly.adobe.io/`（`timeout≈10s`，`allow_redirects=False`）；
  代理为空 → 直连测试。
- 结果：
  - 拿到任意 HTTP 响应（含 401/403/404）→ `{ok: true, status_code, latency_ms, via: "proxy"|"direct"}`
    （能到 Adobe 即视为连通）；
  - `requests.Timeout` → `{ok:false, error:"timeout", latency_ms}`；
  - `ProxyError`/`ConnectionError`/其它 → `{ok:false, error:<简短原因>}`。
  - 代理 URL 非法（无法解析 scheme/host）→ 400。
- 安全：仅探测固定 Adobe 主机常量，不接受任意目标 URL（防 SSRF 探测滥用）。

前端 `static/admin.html` / `static/admin.js` / `static/admin.css`：

- `confProxy` 输入框旁加「测试连通性」按钮 + 结果行；
- 点击：读输入框当前值 → POST 端点 → 按钮进入 loading；
- 结果就地展示：成功「✓ 连通 (HTTP <code>, <ms>ms, 经代理/直连)」，
  失败「✗ <原因>」，用既有 msg 样式（成功/失败配色）。

## 错误处理

- 功能 1 纯前端，筛选谓词对缺字段（undefined/NaN）安全降级（不计入）。
- 功能 2：`handle_auth_failure` 内部已 try/except；重取积分失败不得抛未捕获异常导致
  接口 500——包在 try 内，最终以 `set_credits_error` + 抛已知错误收尾。
- 功能 3：一切网络异常转结构化 `{ok:false,error}`，端点自身返回 200（除非鉴权/参数错）。

## 测试

- 后端离线单测（`tests/`）：
  - `_fetch_credits_balance` 401/403 → `CreditsAuthError`，500/429 → 普通 RuntimeError（打桩 requests）；
  - `refresh_credits_for_token_id(handle_auth=True)`：手动号 401 → 调 handle_auth_failure、
    状态 invalid；自动号 401 且 cookie 刷新成功 → 重取积分成功路径；`handle_auth=False`
    时 401 不触发 handle_auth_failure；
  - `test-proxy` 端点：桩 requests 返回 401 → `ok:true`；桩 Timeout → `ok:false,timeout`；
    非法代理 URL → 400。
- 前端筛选逻辑若可抽纯函数（`getFilteredTokens` 接受列表+filter）则加纯函数单测；
  否则以手动核对 + E2E 冒烟覆盖。

## 不做（YAGNI）

- 卡片筛选不加多选/组合筛选、不加 URL 持久化；
- 代理测试不支持自定义目标 URL、不做并发多目标探测；
- 积分 401 的自动刷新循环路径不改（维持现状，避免重复刷新）。
