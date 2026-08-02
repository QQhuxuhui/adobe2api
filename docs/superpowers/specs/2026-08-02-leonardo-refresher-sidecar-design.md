# Leonardo Token 自动刷新 sidecar（A‴）设计

日期：2026-08-02
状态：已通过设计评审，待写实现计划

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
- 通过 `HTTP_PROXY/HTTPS_PROXY=http://<代理>:10809` 访问 Leonardo（过地理限制）。

**数据流**
1. 首次登录：打开 `http://<host>:<novnc_port>`（noVNC，带访问密码）→ 看到容器内 headful Chrome → 手动完成 `app.leonardo.ai` 登录（Turnstile + Canva OTP 人工点）。会话落进持久化 profile。
2. 刷新循环：每 `REFRESH_INTERVAL`（默认 ~50 分钟）在同一浏览器上下文里执行 `fetch('/api/auth/get-session')`，取 `session.accessToken`，校验其 exp 新鲜（>120s）。
3. 推送：把 accessToken 推给 adobe2api，按 `account_id` upsert（替换该账号旧 token，池里恒 1 条，不累积）。
4. 失效自愈：get-session 返回 null（会话过期/掉线）→ 在 noVNC 页面/日志标注"需重新登录" + 告警；人工再远程登一次。

## adobe2api 侧改动（小）

- 新增 upsert 端点：`POST /api/v1/tokens/leonardo`，按 `account_id`（Cognito id_token 的 `sub`）upsert leonardo token —— 存在则替换 value + 重置 status/fails，不存在则新增；保证一个 Leonardo 账号池中恒一条。约 15–25 行（admin.py 路由 + token_mgr upsert 方法）。
- 复用现有 `type=leonardo` 自动识别（`is_likely_leonardo_token`）与按类型选号，无需改出图链路。
- 鉴权：refresher 用 admin 凭据（env）登录 admin API 后调用；沿用现有 session 鉴权。

## 部署

- `docker-compose.yml` 增 service `leonardo-refresher`：
  - volume `./data/leo-profile:/profile`（持久化 Chrome user-data-dir）。
  - 暴露 noVNC 端口（**必须设访问密码**，勿裸奔公网）。
  - env：`ADOBE2API_BASE_URL`、`ADOBE2API_ADMIN_USER/PASS`、`HTTP_PROXY/HTTPS_PROXY`、`LEONARDO_ACCOUNT_LABEL`、`REFRESH_INTERVAL_SECONDS`、`NOVNC_PASSWORD`。
- 单 sidecar 管**一个** Leonardo 账号。

## 安全

- noVNC 加访问密码，端口不对公网裸开（建议仅内网/SSH 隧道/加密）。
- 持久化 profile 含账号会话，等同账号访问权：volume 权限收紧，不入库、不进镜像、`.gitignore` 覆盖 `data/leo-profile`。
- admin 凭据经 env 注入，不硬编码。

## 失败与边界处理

- get-session null / 抛错：不覆盖池中现有 token（避免用坏值顶掉可用值），记日志 + 标注需重登。
- accessToken exp 不新鲜：视为一次失败，等下一轮或触发一次即时重取。
- 代理不通/地理封锁：日志明确区分（geo-embargo 403 vs 网络错）。
- 容器重启：从持久化 profile 恢复；profile 失效则回到"需重新登录"。

## 不做（YAGNI）

- 多账号编排（先支持 1 个，结构上不排斥以后扩）。
- 自动过 Turnstile（登录始终人工）。
- 把 refresher 合进 adobe2api 镜像。
- 纯 HTTP / utls 重放路径（已实测不可行，放弃）。

## 相关

- [[leonardo-type-research]]：Leonardo 接入调研与多模型、slug=sdVersion 规律。
- adobe2api：`core/token_mgr.py`（token 池、type、account_id）、`api/routes/admin.py`（token 端点）、`core/refresh_mgr.py`（Adobe 的 cookie→IMS 自动续期，对照参考）。
