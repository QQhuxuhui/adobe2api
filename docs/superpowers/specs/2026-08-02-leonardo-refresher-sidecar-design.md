# Leonardo Token 自动刷新 sidecar（A‴）设计

日期：2026-08-02
状态：设计已补充（v3，纳入两轮代码评审），待写实现计划

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
  - 两个容器都从专用变量 `LEONARDO_PROXY=http://<代理>:10809` 读取代理，不设置容器级 `HTTP_PROXY/HTTPS_PROXY`，避免 Adobe、CDN 下载等无关请求被全局代理接管。
  - Chromium 走 `launch_persistent_context(proxy={"server": LEONARDO_PROXY})` 显式传参（对齐 `scripts/leonardo_bootstrap_spike.py:123` 的 launch proxy）。
  - adobe2api 的 `core/leonardo_client.py:_http_gql` 改为读取 `LEONARDO_PROXY`，仅给 Leonardo GraphQL 请求显式传 `proxies={"http": proxy, "https": proxy}`。
  - sidecar → `http://adobe2api:6001` 的推送 HTTP client 固定 `trust_env=False`，保证内网调用不受宿主机或基础镜像代理变量影响。

**数据流**
1. 首次登录：打开 `http://<host>:<novnc_port>`（noVNC，带访问密码）→ 看到容器内 headful Chrome → 手动完成 `app.leonardo.ai` 登录（Turnstile + Canva OTP 人工点）。会话落进持久化 profile。
2. **刷新循环（P1.1，exp 驱动，非固定睡眠）**：
   - 启动后**立即刷新一次**（不先睡）。
   - 每轮：同一浏览器上下文 `fetch('/api/auth/get-session')` 取 `session.accessToken`，解析其 `exp`。
   - 调度使用相对延迟，不能把 Unix 时间戳直接当秒数：`delay = max(MIN_INTERVAL, min(REFRESH_INTERVAL, exp - now - SAFETY_MARGIN))`，`next_at = now + delay`。默认 `SAFETY_MARGIN=600s`、`MIN_INTERVAL=60s`、`REFRESH_INTERVAL=3000s`。
   - 若 `exp - now < SAFETY_MARGIN`（get-session 给了旧值/未续期），**不推送该 token**，按 `MIN_INTERVAL` 短间隔重取；绝不带快过期 token 睡满一整轮。
3. 推送：仅将剩余时间不小于 `SAFETY_MARGIN` 的 accessToken 按 `account_id` upsert 进池（见「adobe2api 侧改动」）。
4. 失效自愈：get-session 返回 null / 非 id_token → **不覆盖池中现有 token**，healthz 降级 + 标注需重登；人工再远程登一次。

## adobe2api 侧改动

### 新增端点 `POST /api/v1/tokens/leonardo`
入参 `{token, label?}`。流程：① 校验独立共享密钥；② 严格校验 token（下）不合格 → 400，绝不入池；③ `token_mgr.upsert_leonardo_token(value, account_id, label)`。成功响应包含 `status=created|updated|noop`、`token_id`、`account_id` 和 `expires_at`；不得返回原始 token。

### token_mgr.upsert_leonardo_token（P1.3，同一把 `self._lock` 内原子）
现有坑：`add()` 只按 value 去重（`token_mgr.py:66`），选号把**所有** active `type=="leonardo"` 纳入轮询（`token_mgr.py:247`）→ 普通 add 会累积多条并被并发选号。故新方法必须：
- 只在 `type=="leonardo"` 集合里，按 `account_id`（存储字段，缺失则回解 JWT `sub`）匹配。
- **命中**：先以 `exp` 最大的现有 token 为保留项并删除其余重复项。若传入 token 的 `exp` 小于保留项，保持保留项的值和状态并返回 `status=noop`；否则更新保留项的 `value`、`status=active`、`fails=0`、`error_until=0`、`type=leonardo`、`source=leonardo_refresher`、`account_id`、`updated_at`。
- `label` 写入现有 `refresh_profile_name` 字段（空值回退到 `account_id`），让当前 token 列表、请求日志和 credit 归属无需新增 UI 契约即可显示账号标签；不另存一个当前消费者看不到的 `label` 字段。
- **未命中**：新增一条并设置上述字段，返回 `status=created`；实际替换或状态复位返回 `status=updated`。若 value、状态和元数据均无需变化，则返回 `status=noop`，重复请求保持幂等。
- 结束后该 account_id 恒 1 条记录；`created/updated` 后该记录为 active，因 `exp` 倒退而 `noop` 时保留原状态。
- 测试覆盖起始态：该账号 **0 / 1 / 多条重复** 三种，以及传入 token **更新 / 同值幂等 / `exp` 倒退** 三种。

### 严格 token 校验（P2.4，防错 token 覆盖正常账号）
比 `is_likely_leonardo_token`（过宽，`leonardo_client.py:47` 会把任意 cognito token 认成 leonardo）更严：
- issuer 精确 == `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xkVMuCqeu`（可配，默认此值）
- `aud` == `29lhcpsoi9crda0du1s0ampft3`（可配）
- `token_use == "id"`
- `sub` 非空（作 account_id）
- `exp - now >= LEONARDO_TOKEN_MIN_TTL_SECONDS`（默认 600，与 sidecar `SAFETY_MARGIN_SECONDS` 默认值一致）
任一不满足 → 400。

### 鉴权（P2.5）：独立共享密钥（优于复用 admin 全权）
- 该端点只用独立密钥门控：env `LEONARDO_REFRESH_KEY` + 请求头 `X-Leonardo-Refresh-Key`；不复用 admin session。
- 启用 sidecar 时，adobe2api 与 sidecar 必须从同一 secret source 注入该值。adobe2api 未配置密钥时仅禁用该专用端点（返回 503），不影响其他功能；sidecar 已启用但密钥为空时启动失败。任何情况都不允许退化成无鉴权端点。
- 服务端使用常量时间比较；缺失或错误密钥统一返回 401，不在日志中输出请求头或密钥内容。

### 出图业务链路不改
沿用 `type=leonardo` 按类型选号；仅把 `core/leonardo_client.py` 的代理配置源从全局 `HTTP_PROXY/HTTPS_PROXY` 收窄为 `LEONARDO_PROXY`。

## 部署

- `docker-compose.yml` 增 service `leonardo-refresher`：
  - **独立 named volume** `leo-profile:/profile`（持久化 Chrome user-data-dir；P2.6：不放进 adobe2api 已挂的 `./data`，隔离权限）。
  - noVNC 端口**默认绑 `127.0.0.1`**（`127.0.0.1:<port>:<port>`）+ 访问密码；远程走 SSH 隧道，不对公网裸开。
  - `/healthz`（P2.6）：进程与浏览器控制通道可响应时返回 200，并暴露 `state`、`session_state`、`last_success_at`、`current_token_exp`、`consecutive_failures`、`last_error_kind`。Docker healthcheck 只判断进程存活，不因登录过期反复重启容器；告警系统根据状态字段和 `last_success_at` 判断业务降级。
- 单 sidecar 管**一个** Leonardo 账号。

两个容器的环境变量必须分别配置；环境变量不会在 Compose service 之间自动继承：

| service | 必需环境变量 | 用途 |
| --- | --- | --- |
| `adobe2api` | 启用 refresher 时设置 `LEONARDO_REFRESH_KEY`；Leonardo 出图设置 `LEONARDO_PROXY`；`LEONARDO_TOKEN_MIN_TTL_SECONDS=600` | 校验推送、代理 Leonardo GraphQL、拒绝快过期 token |
| `leonardo-refresher` | `ADOBE2API_BASE_URL=http://adobe2api:6001`、同一个 `LEONARDO_REFRESH_KEY`、同一个 `LEONARDO_PROXY`、`LEONARDO_ACCOUNT_LABEL`、`REFRESH_INTERVAL_SECONDS=3000`、`SAFETY_MARGIN_SECONDS=600`、`MIN_INTERVAL_SECONDS=60`、`NOVNC_PASSWORD` | 浏览器刷新、推送与 noVNC |

`LEONARDO_REFRESH_KEY` 应通过同一个 Compose 插值或 secret 注入两侧，不能在文件中硬编码。`LEONARDO_TOKEN_MIN_TTL_SECONDS` 应与 `SAFETY_MARGIN_SECONDS` 保持一致；启动时校验 `MIN_INTERVAL_SECONDS > 0`、`SAFETY_MARGIN_SECONDS > MIN_INTERVAL_SECONDS`、`REFRESH_INTERVAL_SECONDS > MIN_INTERVAL_SECONDS`。

## 安全

- noVNC **默认绑 127.0.0.1** + 访问密码；远程走 SSH 隧道。
- profile 用独立 named volume（非 `./data` 子目录），含账号会话＝账号访问权，权限收紧，不入库/不进镜像。
- 推送用独立 `LEONARDO_REFRESH_KEY`（非 admin 全权），env 注入。
- 严格 token 校验（issuer/aud/token_use/sub/exp）防错值覆盖正常账号。
- 使用专用 `LEONARDO_PROXY`，不通过全局代理变量扩大代理影响面。

## 失败与边界处理

- get-session 明确返回 null、登录页或非 id_token：**不覆盖**池中现有 token，进入 `state=login_required`、`session_state=login_required`，`consecutive_failures++`，标注需重登。
- get-session 超时、代理不通或地理封锁：不推断已经掉登录，进入 `state=refresh_retrying`、`session_state=unknown`；日志用 `last_error_kind` 区分 `network`、`proxy`、`geo_embargo`，按 MIN_INTERVAL 重试。
- accessToken 剩余 < SAFETY_MARGIN：不推送，进入 `state=refresh_retrying`，按 MIN_INTERVAL 短间隔重取（P1.1）。
- sidecar 已取得合格 token、但推送超时或返回非 2xx：进入 `state=push_failed`，保留浏览器登录状态并按 MIN_INTERVAL 重试推送/刷新；不得删除池中旧 token。
- 浏览器控制通道不可用（launch/goto 失败、页面崩溃、懒开异常）：进入 `state=browser_unavailable`（第 5 态，spec:79 healthz 唯一返回 503 的状态），按 MIN_INTERVAL 重试懒开；连续 `MAX_BROWSER_CONTROL_FAILURES`(=3) 次仍不可用则进程退出交由容器重启。**启动时不 eager-open**：首个 `run_once` 经 `fetch_token` 懒加载驱动，避免 eager open 半开状态绕过降级导致崩溃循环。
- 刷新并推送成功：进入 `state=healthy`、`session_state=authenticated`，更新 `last_success_at`、`current_token_exp` 并清零 `consecutive_failures`。
- 容器重启：从持久化 profile 恢复；profile 失效则 healthz 降级、回到"需重新登录"。

## 不做（YAGNI）

- 多账号编排（先支持 1 个，结构上不排斥以后扩）。
- 自动过 Turnstile（登录始终人工）。
- 把 refresher 合进 adobe2api 镜像。
- 纯 HTTP / utls 重放路径（已实测不可行，放弃）。

## 相关

- [[leonardo-type-research]]：Leonardo 接入调研与多模型、slug=sdVersion 规律。
- adobe2api：`core/token_mgr.py`（token 池、type、account_id）、`api/routes/admin.py`（token 端点）、`core/refresh_mgr.py`（Adobe 的 cookie→IMS 自动续期，对照参考）。
