# 管理后台 Token 页三项改进（卡片筛选 / 积分认证异常 / 代理测试）

日期：2026-07-16
修订：2026-07-17
状态：设计已确认

## 背景

管理后台 Token 管理页有三个改进点：

1. Token 概览的 4 张统计卡（账号总数 / 生效中 / 无积分账号 / 异常账号）目前纯展示，
   点击无行为；用户希望点卡片筛选出对应账号列表。
2. 部分账号积分区显示「刷新失败 credits request failed: 401」但 Token 状态仍「生效中」，
   页面没有区分 Token 是否可用于生成和积分接口是否健康。
3. 系统配置「代理与网络」的代理地址无连通性测试手段。

现状（已核对代码）：

- 表格前端分页渲染 `latestTokens`（`static/admin.js` `renderTable`/`getCurrentPageTokens`），
  统计卡在 `renderTokenSummary` 按全量列表计数。
- 积分刷新 `_fetch_credits_balance`（`core/refresh_mgr.py:694`）非 200 抛
  `RuntimeError("credits request failed: <code>")`，调用方仅 `set_credits_error` 写错误文案，
  不改 `status`。
- 已有 `token_manager.handle_auth_failure`（`core/token_mgr.py:333`）：自动号先
  `refresh_once` 试 cookie 刷新、超阈值标 `invalid`；手动号直接 `report_invalid`。
- `refresh_once`（`core/refresh_mgr.py:777`）更新 Token 后会在内部再次刷新积分。积分刷新流程若直接
  调 `handle_auth_failure`，必须避免 `refresh_once` 内外对新 Token 重复请求积分。
- 自动刷新循环（`core/refresh_mgr.py:828-842`）先 cookie 刷新拿到新 Token，再刷新积分；
  此路径的积分 401/403 只能证明积分接口异常，不能单独证明 Token 已无法用于生成。
- 代理配置项为 `confProxy`（`static/admin.html:182`）；`requests` 默认可能读取环境代理，
  因此“直连测试”必须显式关闭 `trust_env` 才能保证语义准确。

## 状态语义

- Token `status` 表示该 Token 是否可进入生成请求的可用池。
- `credits_error` 表示最近一次积分读取是否失败，是独立于 Token `status` 的健康信号。
- 自动账号用新 Token 读取积分仍返回 401/403 时，保持 `status=active`，同时保留
  `credits_error` 并归入「异常账号」。这样不会误禁用仍可生成的 Token，也不会继续把积分异常账号
  只显示为“生效中”。
- 手动 Token 没有 cookie 恢复能力，积分 401/403 按认证失败处理并置为 `invalid`。
- 统计卡分类允许重叠。例如一个 `status=active` 但存在 `credits_error` 的账号同时计入
  「生效中」和「异常账号」；4 张卡不是互斥分桶，其数量之和不要求等于账号总数。

## 功能 1：统计卡点击筛选（纯前端）

涉及 `static/admin.html`、`static/admin.js`、`static/admin.css`，无后端改动。

### 筛选状态与谓词

- 新增模块级筛选状态 `tokenFilter ∈ {null, "active", "zero_credit", "broken"}`。
- 提取共用纯函数，统计卡计数和列表筛选必须调用同一套函数，避免口径漂移：
  - `hasKnownCredits(value)`：仅当值不是 `null`、`undefined`、空字符串，且
    `Number(value)` 为有限数时返回 `true`；
  - `matchesTokenFilter(token, "active")`：
    `String(token.status || "").toLowerCase() === "active"`；
  - `matchesTokenFilter(token, "zero_credit")`：
    `hasKnownCredits(token.credits_available) && Number(token.credits_available) <= 0`；
  - `matchesTokenFilter(token, "broken")`：状态为 `invalid`/`error`，或 `is_expired`，
    或 `String(token.credits_error || "").trim()` 非空。
- `null`、`undefined`、`""`、`NaN` 不得计入「无积分账号」；数值 `0`、负数和可解析的
  数字字符串按实际数值处理。

### 卡片交互

- 4 张卡改为语义化 `<button type="button" class="stat-card">`，不使用 `div + role=button`
  模拟按钮；原有视觉样式通过 CSS reset 保持不变。
- 账号总数对应 `tokenFilter=null`；其余 3 张卡分别对应 `active`、`zero_credit`、`broken`。
- 再次点击当前激活的非总数卡时清除筛选，回到全部。
- 当前卡设置 `.is-active` 和 `aria-pressed="true"`；其余卡为 `aria-pressed="false"`。
  原生按钮自动支持回车和空格。

### 渲染、分页与选择

- 新增 `getFilteredTokens(tokens = latestTokens, filter = tokenFilter)`。
- `renderTokenPagination`、`getCurrentPageTokens`、全选框同步均以筛选后的列表为准。
- 筛选值实际发生变化时：
  - `tokenCurrentPage = 1`；
  - 清空 `tokenSelectedIds` 并同步全选框，避免批量删除或导出作用于当前筛选中不可见的账号；
  - 重新渲染表格和分页。
- 同一筛选内跨分页的选择行为维持现状。
- 统计卡计数和积分图仍基于全量 `latestTokens`，只有表格显示筛选子集。
- `latestTokens` 为空时显示原有“当前没有可用的 Token”空态；全量非空但筛选结果为空时显示
  「该筛选下暂无账号」，并隐藏分页。
- 「异常账号」卡片说明改为「失效 / 过期 / 积分异常」。

## 功能 2：积分 401/403 接入认证恢复和异常状态

涉及 `core/refresh_mgr.py`、`core/token_mgr.py`、`api/routes/admin.py`、`static/admin.js`。

### 异常类型

- 新增 `CreditsAuthError(RuntimeError)`，携带 `status_code`。
- `_fetch_credits_balance` 遇 401/403 抛 `CreditsAuthError`；其余非 200 仍抛
  `RuntimeError("credits request failed: <code>")`，保持现有行为。

### 消除重复积分请求

- `refresh_once(profile_id, *, refresh_credits: bool = True)` 增加仅供内部流程使用的关键字参数：
  - 默认 `True`，所有现有调用行为不变；
  - `False` 时只刷新 cookie/Token 和账号资料，不在 `refresh_once` 内调用
    `refresh_credits_for_token_id`；返回结构保持不变，`credits_error` 为空字符串。
- `token_manager.handle_auth_failure(value, *, refresh_credits: bool = True)` 增加对应关键字参数，
  并将其传给 `refresh_once`。现有生成请求调用保持默认值，不改变原有行为。
- 积分认证恢复调用
  `handle_auth_failure(token_value, refresh_credits=False)`。当结果为 `refreshed` 时，外层按
  `token_id` 重新读取真实 Token 值并重新提取 `account_id`，然后只对新 Token 请求一次积分。
- 一次自动账号恢复最多产生两次积分请求：旧 Token 一次、新 Token 一次；不得在
  `refresh_once` 内外对新 Token 重复请求。

### `refresh_credits_for_token_id`

- 签名改为 `refresh_credits_for_token_id(token_id, handle_auth: bool = False)`。
- `handle_auth=False`：直接向上抛 `CreditsAuthError`，由现有调用方写入 `credits_error`，
  不触发 cookie 刷新，也不修改 Token 状态。自动刷新循环和 `refresh_once` 内部使用此默认值。
- `handle_auth=True`：第一次遇 `CreditsAuthError` 后调用
  `handle_auth_failure(token_value, refresh_credits=False)`：
  - `refreshed`：重新读取该 `token_id` 的新 Token 和 `account_id`，重取一次积分；成功则
    `set_credits` 并返回；失败则向上抛实际的第二次异常；
  - `invalid`：Token 状态已置 `invalid`，向上抛带确定性文案的 `CreditsAuthError`；
  - `retry`：状态保持不变，向上抛带恢复失败原因的 `CreditsAuthError`。
- `refresh_credits_for_token_id` 成功时负责 `set_credits`；失败时不重复写 `credits_error`。
  单个、批量和自动刷新调用方各自捕获一次并调用 `set_credits_error`，保持单一写入责任。
- 新 Token 的第二次积分请求仍返回 401/403 时，不再递归刷新 cookie：状态保持 `active`，
  调用方写入 `credits_error`，账号通过功能 1 的谓词进入「异常账号」。
- 非 401/403 的积分错误（429/500/网络等）一律不改 Token 状态，但仍写入
  `credits_error` 并归入「异常账号」。

### 调用点与前端刷新

- `api/routes/admin.py` 单个 `refresh_token_credits`：传 `handle_auth=True`。
- `api/routes/admin.py` 批量 `refresh_tokens_credits_batch`：传 `handle_auth=True`。
- `core/refresh_mgr.py` 自动刷新和 `refresh_once` 内部：保持默认 `handle_auth=False`。
- 单个积分刷新前端无论接口成功或失败都必须在 `finally` 中 `await loadTokens()`，再结束
  loading。这样后端已写入的 `invalid` 或 `credits_error` 会立即反映到状态列、积分列和统计卡。
- 批量接口继续返回 `ok/partial` 汇总并在完成后刷新列表。

## 功能 3：代理连通性测试（前后端）

涉及 `api/schemas.py`、`api/routes/admin.py`、`static/admin.html`、`static/admin.js`、
`static/admin.css`。

### 请求与校验

- 在 `api/schemas.py` 新增 `ProxyTestRequest(BaseModel)`，字段为 `proxy: str = ""`。
- 新端点 `POST /api/v1/config/test-proxy` 接收 JSON
  `{ "proxy": "<url 或空>" }`，执行管理鉴权；输入值来自当前输入框，不要求先保存配置。
- 非空代理 URL 使用结构化 URL 解析，并满足：
  - scheme 只允许 `http` 或 `https`；当前依赖未安装 SOCKS 支持，不接受 `socks*`；
  - 必须存在 hostname；
  - port 必须可解析且范围合法。
- 不满足校验时返回 400；响应和日志不得回显包含用户名、密码的完整代理 URL。

### 探测行为

- 固定探测 `https://firefly.adobe.io/`，不接受客户端提供目标 URL。
- 使用独立 `requests.Session()` 并设置 `session.trust_env = False`：
  - 非空代理构造 `{"http": proxy, "https": proxy}`；
  - 空代理不传代理映射，确保测试是真正直连，不读取 `HTTP_PROXY`/`HTTPS_PROXY`。
- 请求参数为 `timeout=10`、`allow_redirects=False`；用 `time.perf_counter()` 计算
  `latency_ms`，并在成功和失败响应中返回。
- 拿到任意 HTTP 响应（含 401/403/404）视为能到达 Adobe，返回
  `{ok:true,status_code,latency_ms,via:"proxy"|"direct"}`。
- 网络异常转换为稳定错误码，不直接返回 `str(exc)`：
  - `requests.exceptions.Timeout` -> `timeout`；
  - `requests.exceptions.ProxyError` -> `proxy_error`；
  - `requests.exceptions.ConnectionError` -> `connection_error`；
  - 其它 `requests.exceptions.RequestException` -> `request_error`。
- 连通性失败返回 HTTP 200 和 `{ok:false,error,latency_ms,via}`；鉴权、请求体或 URL 参数错误
  使用对应的 401/422/400。

### 安全边界

- 固定 Adobe 目标可防止客户端借此端点指定任意 HTTP 目标，但服务器仍必须连接管理员填写的
  代理主机；因此该能力不是“完全防 SSRF”。
- 本功能明确把代理地址视为受信管理员配置，并依赖现有管理鉴权。允许 loopback/内网代理，
  因为 `127.0.0.1` 等本地代理是核心使用场景。
- 为减少端口探测信息，不回显底层 socket 异常、DNS 结果、代理凭据或完整代理 URL；前端只展示
  稳定错误码对应的用户文案。

### 前端交互

- `confProxy` 输入框旁增加「测试连通性」按钮和结果行。
- 点击后读取当前输入值并 POST；请求期间禁用按钮并显示 loading，结束后恢复。
- 成功显示「连通（HTTP <code>，<ms>ms，经代理/直连）」；失败按稳定错误码映射为中文文案，
  使用既有成功/失败消息配色。
- `confProxy` 内容变化时立即清空上一次测试结果，防止修改地址后继续显示旧的“连通”。

## 错误处理

- 功能 1 对所有缺失积分值安全降级；`null` 等未知值不计入零积分。
- 功能 2 不递归执行认证恢复；失败异常由 API/自动刷新调用方统一捕获并写一次
  `credits_error`。单个刷新接口即使返回非 2xx，前端也会重新加载最新状态。
- 功能 3 的网络失败均转换为结构化 HTTP 200 响应；只有鉴权和输入错误返回非 2xx。
- 所有前端 loading 状态都在 `finally` 中恢复，避免异常路径留下不可用按钮。

### 2026-07-17 审查修复约束

- `test-proxy` 必须在读取或校验请求体之前完成管理鉴权。已登录请求的 JSON 解析或
  `ProxyTestRequest` 校验失败时只返回固定脱敏文案，不得返回 Pydantic 的原始 `input`。
- Token 数据重新加载后，已选 ID 必须收缩到当前筛选结果的完整 ID 集合；允许在同一筛选内
  跨页保留选择，但不得保留当前筛选中不可见的账号。
- 代理输入变化时必须使正在执行的测试结果失效。旧请求可以自然结束，但不得覆盖新输入对应的
  空状态或更新结果。
- Token 列表加载失败时必须清空 `latestTokens`、已选 ID 和当前页，避免后续点击筛选卡重新渲染
  上一次成功请求的旧数据。
- 并发 Token 列表请求只有最新一次可以提交成功或失败状态；旧请求晚到时返回 `stale` 且不得
  渲染或清空列表。`/api/v1/tokens` 的非 2xx 响应必须进入失败分支，不能当作空列表成功。
- `loadConfig()` 等程序化代理值更新在有效值发生变化时，也必须失效正在执行的代理测试并清空
  结果，不能只依赖用户触发的 `input` 事件。

## 测试与验收

### 后端离线单测

- `_fetch_credits_balance`：401/403 -> `CreditsAuthError` 且保留状态码；429/500 -> 普通
  `RuntimeError`。
- `refresh_credits_for_token_id(handle_auth=False)`：401/403 不调用 `handle_auth_failure`，
  不修改 Token 状态。
- 手动 Token + `handle_auth=True`：401/403 调用认证失败机制，状态变为 `invalid`。
- 自动 Token + `handle_auth=True`：旧 Token 401、cookie 刷新成功、新 Token 积分成功；断言
  `refresh_once` 跳过内部积分刷新，积分网络调用总数严格为 2。
- 自动 Token 的新 Token 仍返回 401/403：不再次刷新 cookie，状态保持 `active`，调用方写入
  `credits_error`。
- 自动 Token cookie 刷新失败：覆盖 `retry` 和达到阈值后的 `invalid`。
- `test-proxy`：
  - 代理或直连拿到 401 -> `ok:true`；
  - Timeout/ProxyError/ConnectionError -> 对应稳定错误码且不泄露底层异常文本；
  - 空代理时即使环境设置 `HTTPS_PROXY` 也不使用环境代理；
  - 缺 scheme/host、非法 port、`ftp`/`socks5` -> 400；
  - 未登录请求 -> 401。

### 前端逻辑与冒烟

- 共用谓词覆盖 `null`、`undefined`、空字符串、`NaN`、`0`、负数和数字字符串。
- `credits_error` 非空且 `status=active` 的账号同时计入「生效中」和「异常账号」。
- 切换筛选后页码回到 1、选中项被清空、全选框状态正确；筛选空结果隐藏分页。
- 单个积分刷新接口失败后仍调用 `loadTokens()`，页面显示最新 `invalid` 或积分异常。
- 修改代理输入后旧测试结果被清空；测试期间按钮不可重复点击，成功和失败后均恢复。
- 卡片使用原生按钮，鼠标、回车、空格均可触发，焦点样式和 `aria-pressed` 正确。

## 不做（YAGNI）

- 卡片筛选不加多选/组合筛选，不加 URL 持久化。
- 不新增持久化的 `credits_status` 字段；本次直接以现有 `credits_error` 作为积分健康信号。
- 代理测试不支持自定义目标 URL，不做并发多目标探测，不支持 SOCKS。
- 不因自动刷新所得新 Token 的积分 401/403 直接禁用 Token；是否能生成仍由实际生成请求的
  认证失败机制判断。
