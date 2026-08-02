# Leonardo Token 自动刷新 sidecar（A‴）设计

日期：2026-08-02
状态：设计已补充（v2，纳入代码评审 6 点），待写实现计划

## 目标

让 Leonardo 账号的出图 Bearer（AWS Cognito id_token，~1 小时过期）**自动续期**，把当前"每小时手动 `--update` 一次"降级为"~6 周远程登录一次"。产出接入 adobe2api 现有 token 池（`type=leonardo`），出图链路不变。

## 背景与关键实测结论（决定为什么是这个设计）

- Leonardo = AWS Cognito 用户池 `us-east-1_xkVMuCqeu` + app client `29lhcpsoi9crda0du1s0ampft3`（公共 client），经 Canva OIDC 联邦登录；出图用的 Bearer 是 Cognito **id_token**（RS256，iss 含 `cognito-idp`）。
- 当前 auth 栈是 **better-auth**（`GET https://app.leonardo.ai/api/auth/get-session`），不是 next-auth（`/api/auth/session` 已 404）。get-session 在**浏览器上下文里**返回 `session.accessToken` = 新鲜 Cognito id_token；会话 `expiresAt` 实测约 6 周。
- **纯 HTTP 服务端重放不可行**：即使 cookie / 出口 IP（9.142.50.206）/ User-Agent / 全部 header 完全一致、且用 curl_cffi(Chrome 指纹) 过了 Cloudflare，`get-session` 仍返回 `null`。会话有效性绑定活动浏览器上下文（CF bot-management/设备信号），拷贝 cookie 到异地服务端无法复现。
- **Cognito refresh_token 拿不到**：Leonardo 用 better-auth，refresh_token 扣在其后端 accounts 表，浏览器只拿短命 id_token；且 Cognito app client 的 redirect_uri 白名单是 Leonardo 自己的，我们无法用 CPA 那种 OAuth loopback/PKCE 流截获 code → 换不出 refresh_token。
- **微软 MSA refresh token 与 Cognito 无关**：只换邮箱 scope，用于读 hotmail 取 Canva OTP，跳不过 Canva 的 Cloudflare 登录。

**结论**：唯一稳的自动化 = 让"登录的浏览器"就是"刷新的浏览器"（同一上下文，绕开重放问题）。人工完成一次 CF Turnstile + Canva OTP 登录，之后浏览器在自己的上下文里定时 get-session 续期。

## 架构

独立 sidecar 容器 `leonardo-refresher`，与 adobe2api 并列（adobe2api 极简镜像不变，职责解耦）。

**组件**
- 基础镜像：Playwright Python（自带 Chromium）+ Xvfb + x11vnc + noVNC。
- 常驻 Python 进程：管理**持久化** Chrome 上下文（user-data-dir 挂 volume，重启免重登）+ 刷新循环 + 推送。
- **代理（P1.2，三处都要对）**：
  - Chromium 走 `launch_persistent_context(proxy={"server": "http://<代理>:10809"})` 显式传参，**不依赖进程环境变量**（对齐 `scripts/leonardo_bootstrap_spike.py:123` 的 launch proxy）。
  - adobe2api 容器也需配 `HTTP_PROXY/HTTPS_PROXY`：出图 GraphQL 在 `core/leonardo_client.py:_http_gql` 读环境代理访问 `api.leonardo.ai`。
  - sidecar → `http://adobe2api:6001` 的推送必须**绕过代理**：设 `NO_PROXY=adobe2api,127.0.0.1,localhost` 或推送 HTTP client `trust_env=False`。

**数据流**
1. 首次登录：打开 `http://<host>:<novnc_port>`（noVNC，带访问密码）→ 看到容器内 headful Chrome → 手动完成 `app.leonardo.ai` 登录（Turnstile + Canva OTP 人工点）。会话落进持久化 profile。
2. **刷新循环（P1.1，exp 驱动，非固定睡眠）**：
   - 启动后**立即刷新一次**（不先睡）。
   - 每轮：同一浏览器上下文 `fetch('/api/auth/get-session')` 取 `session.accessToken`，解析其 `exp`。
   - 调度下一轮：`next = now + max(MIN_INTERVAL, min(REFRESH_INTERVAL, exp - SAFETY_MARGIN))`（默认 `SAFETY_MARGIN=600s`、`MIN_INTERVAL=60s`、`REFRESH_INTERVAL=3000s`）。即 token 剩余 < REFRESH_INTERVAL+margin 就按 exp 提前刷、进短间隔，**绝不带快过期 token 睡满一整轮**。
   - 若取到的 token 剩余已 < SAFETY_MARGIN（get-session 给了旧值/未续期）→ 按 `MIN_INTERVAL` 短间隔重取，**不当作成功入睡**。
3. 推送：accessToken 按 `account_id` upsert 进池（见「adobe2api 侧改动」）。
4. 失效自愈：get-session 返回 null / 非 id_token → **不覆盖池中现有 token**，healthz 降级 + 标注需重登；人工再远程登一次。

## adobe2api 侧改动

### 新增端点 `POST /api/v1/tokens/leonardo`
入参 `{token, label?}`。流程：① 严格校验 token（下）不合格 → 400，绝不入池；② `token_mgr.upsert_leonardo_token(value, account_id, label)`。

### token_mgr.upsert_leonardo_token（P1.3，同一把 `self._lock` 内原子）
现有坑：`add()` 只按 value 去重（`token_mgr.py:66`），选号把**所有** active `type=="leonardo"` 纳入轮询（`token_mgr.py:247`）→ 普通 add 会累积多条并被并发选号。故新方法必须：
- 只在 `type=="leonardo"` 集合里，按 `account_id`（存储字段，缺失则回解 JWT `sub`）匹配。
- **命中**：更新那一条的 `value`、`status=active`、`fails=0`、`error_until=0`、`type=leonardo`、`source=leonardo_refresher`、`label`、`account_id`、`updated_at`；并**删除其余重复项**（清脏轮询）。
- **未命中**：新增一条，设同样字段。
- 结束后该 account_id 恒 1 条 active。
- 测试覆盖起始态：该账号 **0 / 1 / 多条重复** 三种。

### 严格 token 校验（P2.4，防错 token 覆盖正常账号）
比 `is_likely_leonardo_token`（过宽，`leonardo_client.py:47` 会把任意 cognito token 认成 leonardo）更严：
- issuer 精确 == `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu`（可配，默认此值）
- `aud` == `29lhcpsoi9crda0du1s0ampft3`（可配）
- `token_use == "id"`
- `sub` 非空（作 account_id）
- `exp` 在未来
任一不满足 → 400。

### 鉴权（P2.5）：独立共享密钥（优于复用 admin 全权）
- 该端点用独立密钥门控：env `LEONARDO_REFRESH_KEY` + 请求头 `X-Leonardo-Refresh-Key` 校验；最小权限，机器间用。
- 备选：若复用 admin session，则**必须**遇 401 自动重登 + 限定超时与重试次数。

### 出图链路不改
沿用 `type=leonardo` 自动识别与按类型选号。

## 部署

- `docker-compose.yml` 增 service `leonardo-refresher`：
  - **独立 named volume** `leo-profile:/profile`（持久化 Chrome user-data-dir；P2.6：不放进 adobe2api 已挂的 `./data`，隔离权限）。
  - noVNC 端口**默认绑 `127.0.0.1`**（`127.0.0.1:<port>:<port>`）+ 访问密码；远程走 SSH 隧道，不对公网裸开。
  - `/healthz`（P2.6）：暴露 `logged_in`、`last_success_at`、`current_token_exp`、`consecutive_failures`，供探活/告警。
  - env：`ADOBE2API_BASE_URL`、`LEONARDO_REFRESH_KEY`、`HTTP_PROXY/HTTPS_PROXY` + `NO_PROXY`（chromium 与出图用，内网调用绕过）、`LEONARDO_ACCOUNT_LABEL`、`REFRESH_INTERVAL_SECONDS`、`SAFETY_MARGIN_SECONDS`、`MIN_INTERVAL_SECONDS`、`NOVNC_PASSWORD`。
- 单 sidecar 管**一个** Leonardo 账号。

## 安全

- noVNC **默认绑 127.0.0.1** + 访问密码；远程走 SSH 隧道。
- profile 用独立 named volume（非 `./data` 子目录），含账号会话＝账号访问权，权限收紧，不入库/不进镜像。
- 推送用独立 `LEONARDO_REFRESH_KEY`（非 admin 全权），env 注入。
- 严格 token 校验（issuer/aud/token_use/sub/exp）防错值覆盖正常账号。

## 失败与边界处理

- get-session null / 非 id_token / 抛错：**不覆盖**池中现有 token，记日志 + healthz `logged_in=false` + `consecutive_failures++`，标注需重登。
- accessToken 剩余 < SAFETY_MARGIN：不当成功，按 MIN_INTERVAL 短间隔重取（P1.1）。
- 代理不通/地理封锁：日志区分 geo-embargo 403 vs 网络错。
- 容器重启：从持久化 profile 恢复；profile 失效则 healthz 降级、回到"需重新登录"。

## 不做（YAGNI）

- 多账号编排（先支持 1 个，结构上不排斥以后扩）。
- 自动过 Turnstile（登录始终人工）。
- 把 refresher 合进 adobe2api 镜像。
- 纯 HTTP / utls 重放路径（已实测不可行，放弃）。

## 相关

- [[leonardo-type-research]]：Leonardo 接入调研与多模型、slug=sdVersion 规律。
- adobe2api：`core/token_mgr.py`（token 池、type、account_id）、`api/routes/admin.py`（token 端点）、`core/refresh_mgr.py`（Adobe 的 cookie→IMS 自动续期，对照参考）。
