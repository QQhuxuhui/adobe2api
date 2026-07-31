# Leonardo 类型接入：调研与落地方案

> 日期：2026-07-31 ｜ 状态：调研完成；机制已推断，**前半段（token→读 OTP 邮件）已实测**，**后半段（Canva 登录提交→SSO→抓 cookie→出图）尚未端到端验证**；待开发
> 目标：给 adobe2api 新增「Leonardo」账号类型，用手头的「Canva 会员（微软邮箱）」账号池驱动 Leonardo.ai 图像生成，对外仍是 OpenAI 兼容接口。

---

## 0. 一句话结论

手头账号是 **hotmail 邮箱 + 微软"邮箱访问"refresh token（无密码、无 SSO 能力）**。可行的接入机制**不是**微软 SSO，而是 **邮箱验证码（OTP）登录**：

```
refresh token 只用来读收件箱 → Canva 邮箱验证码登录 → Canva→Leonardo SSO
  → 拿到 Leonardo 的 better-auth 会话 cookie → cookie 换 Cognito JWT → GraphQL 出图
```

其中「读 OTP」纯 HTTP（已实测通过）；「Canva 登录 + SSO」必须跑一次无头浏览器（每账号一次，非每请求）；之后的出图是纯 HTTP。三段全部有开源参考。

---

## 1. 背景与账号形态

### 1.1 账号文件

`tests/canva_member_emails_*.txt`，管道分隔，**4 个字段**：

| 字段 | 内容 | 说明 |
|---|---|---|
| 0 | `xxx@hotmail.com` | 微软个人邮箱 |
| 1 | 13 位 b64url 串 | 未定性（疑似二次凭据/代理，**非**登录密码） |
| 2 | `M.C5xx_BAY.0.U.…` | **MSA（微软消费者）OAuth refresh token** |
| 3 | UUID `9e5f94bc-e8a4-4e73-b8be-63364c29d753` | **client_id** |

- **无账号密码**（用户确认）。
- 同一批邮箱也是 adobe2api 现有 Adobe 号的邮箱（收件箱里能看到 Adobe CC 邮件）。

### 1.2 client_id 的真身

`9e5f94bc-e8a4-4e73-b8be-63364c29d753` 是 **Mozilla Thunderbird 的公共 client_id**。采号工具常借它给 hotmail 号开通邮箱 OAuth 访问。它面向消费者账号（authorize 端点 `tenant=consumers`、跳 `login.live.com`），且接受 `localhost` 回调（典型桌面/CLI 公共客户端）。

### 1.3 refresh token 能兑换出什么（已实测）

在 `https://login.microsoftonline.com/consumers/oauth2/v2.0/token` 用
`grant_type=refresh_token & client_id=<字段3> & refresh_token=<字段2> & scope=https://graph.microsoft.com/.default`
兑换，拿到的 scope 是**纯邮箱权限**：

```
Mail.ReadWrite, IMAP.AccessAsUser.All, POP.AccessAsUser.All,
SMTP.Send, Mail.Send   （Graph 与 Outlook 两套等价）
```

**关键：无 `openid`、无 `id_token` → 不能做任何 OIDC/SSO 联邦。** 这个 token 的唯一能力是**读写这个邮箱**。这就排除了"用 token 直接换 Leonardo/Canva 会话"的一切幻想，也解释了为什么真实机制是邮箱 OTP。

### 1.4 ⚠️ token 轮换陷阱（已踩坑）

MSA refresh token **兑换成功即轮换**：响应里带一个新的 refresh_token，旧的立刻失效。
- 任何成功兑换后**必须把新 token 写回**，否则账号被烧。
- 失败的兑换（scope 不对 / `invalid_grant`）**不轮换**，可安全枚举 scope。
- 调研过程中第一次兑换已把原始 token 轮换掉，当前有效 token 已写回账号文件，号未丢。

---

## 2. 机制实测证据

用兑换出的 Graph token 调 `GET https://graph.microsoft.com/v1.0/me/messages?$top=15&$select=from,subject,receivedDateTime`，成功读到收件箱，其中：

- 一封 `canva.com`，主题为中文「你的Canva可画验证码是 &lt;6位数字&gt;」（实际验证码已脱敏）→ **证明 Canva 走邮箱 6 位验证码登录，且 token 能读到该验证码**。
- 一封 `canva.com`「invited to join a team on Canva」→ **证明账号是 Canva 团队会员**（= 有 Leonardo Essential 通道）。

结论：整条链在"能读到 Canva 验证码"这一步已验证通过；剩下的 Canva 登录提交 + Leonardo SSO 抓 cookie 有开源参考，尚未端到端跑通。

---

## 3. 两套账号体系的根本区分（务必牢记）

Leonardo 有两套**完全隔离**的账号/鉴权，接入前必须分清：

| | 官方 Production API | 网页登录体系（我们要走的） |
|---|---|---|
| 端点 | `cloud.leonardo.ai/api/rest/v1` | `api.leonardo.ai/v1/graphql` |
| 凭据 | API Key（UUID，`app.leonardo.ai/api-access` 生成） | 网页会话 cookie → Cognito JWT |
| 计费 | 独立付费、需绑卡（$5 起） | Canva 会员 SSO 白给的 Essential 额度 |
| 与手头账号 | **不通用**（官方 API 额度 ≠ 网页额度） | ✅ 这才是 Canva 会员能驱动的 |

**Canva 会员通道给的是网页额度，永远变不成官方 API credit。所以官方 API 路线（及其一切官方 SDK）对本任务无用，只能拿来对参数/模型 schema。**

---

## 4. 可参考的开源项目

### 4.1 `hirotomasato/leoapi`（Python+FastAPI，最高价值，可直接抄代码）

已 clone 到 `/usr/src/workspace/github/QQhuxuhui/leoapi`（与 adobe2api 平级）。负责**"拿到 Leonardo cookie 之后"的全部**，目录几乎与 adobe2api 一一对应。

核心文件：
- `app/leonardo_client.py`（585 行）：协议层。
- `app/leonardo_service.py`（462 行）：池编排（LRU 选号、每次生成前重解析 token、auth 错误重试 1 次）。
- `app/store.py`（446 行）：SQLite cookies 表。

已核实的协议细节（源码行号）：
- **取 token**：cookie → 从 CSRF cookie 取 csrfToken → `POST app.leonardo.ai/api/auth/session` → 回包里 walk 出 JWT（`leonardo_client.py:252-305`）。JWT 是 **AWS Cognito** 发的（认 `iss` 含 `cognito-idp`、`token_use`，`:134-153`）。
- **GraphQL 端点**：`https://api.leonardo.ai/v1/graphql`，头带 `authorization: Bearer <jwt>`、`x-leo-schema-version: latest`、sentry-trace/baggage（`:16-49`）。
- **出图 mutation** `Generate`：请求里 `model` 硬编码 `nano-banana-2`，`modelId` 参数动态（`:478-531`）。
- **查额度** `GetUserDetails` / 回退 `GetTokenBalance`：`subscriptionTokens+paidTokens+rolloverTokens`（`:307-377`）。
- **上传图** `UploadImage` → S3 分片 → `GetInitImageModeration` 轮询（`:379-461`）。
- **裸 `requests`，无 TLS 指纹伪装**，仍能跑通（风控不硬）。

⚠️ **移植时必改**：leoapi 是 **next-auth 版**（`/api/auth/session` + `__Secure-next-auth.session-token`），而 Leonardo 当前已迁 **better-auth**（见 §5），要以 better-auth cookie 为准。

### 4.2 `rangga2122/create-canva-new`（Python+Playwright，登录链前半段参考）

已 clone 到 `/usr/src/workspace/github/QQhuxuhui/create-canva-new`。负责 **"从账号到 Leonardo cookie"** 的登录链，**全程无头浏览器 UI 自动化**（不是纯 HTTP）。它本是"造新号"，我们只取"登录 + 抓鉴权"部分。

⚠️⚠️ **绝不可原样运行**：它把抓到的 Leonardo bearer/cookie **回传到作者硬编码的 VPS**（`43.133.150.196:1940` / `leonardo.azkazamdigital.com`，还带明文 SSH 密码，`leonardo_auth_capture.py:37-42、300-401`）。**只读流程和 selector，代码自己重写。**

### 4.3 其它（仅架构参考）

- `hirotomasato/leostudio`（Go）：提供最新 better-auth cookie 证据、失效恢复策略。
- `TheSmallHanCat/flow2api`（2.7k★，Python）：账号池工程化天花板（双令牌、`/health` 四态池），抄架构。
- `one-api / new-api / uni-api`：**全线无 Leonardo 渠道**，且渠道抽象假设"无状态 API key"，与"会过期的网页 cookie 池"不兼容。正确姿势：adobe2api 自己吃掉账号池，对外暴露 OpenAI 兼容端点，再作为"自定义 OpenAI 渠道"注册进 new-api。

---

## 5. 完整登录链（selector 源自 create-canva-new 源码，本项目尚未实跑验证）

来源：`create-canva-new/leonardo_auto_create.py`（selector 为读源码提取，非我们实测；Canva 前端改版可能失效，实现时以真实页面为准）。**全程一个浏览器上下文**——邮箱 OTP 把 Canva 会话种进浏览器，再去 Leonardo 点 Canva SSO 就自动过。

### A. Canva 邮箱验证码登录（已是会员，是登录非注册）
1. 打开 Canva（`:1702`）
2. 点邮箱登录：`button[aria-label="Email"]`（`:1712`）
3. 填邮箱：`input[inputmode="email"]`（`:1717`）
4. 提交 `button[type="submit"]`（点两次，`:1723`）→ Canva 往 hotmail 发验证码
5. **读验证码**（我们用 Graph，纯 HTTP，替代它的 Gmail-UI 抓信）：⚠️ 参考仓库的正则是**英文/印尼文** `(?:verification|security|login)\s+code\s*(?:is|:)?\s*(\d{6})` 或 `kode\s+canva.*?(\d{6})`（`:1309-1313`），**匹配不上手头样本的中文主题「…验证码是 &lt;6位&gt;」**。实现时**必须新增中文模式**（如 `验证码是?\s*[:：]?\s*(\d{6})`），并按邮箱 locale 兼容中/英/印尼。OTP 通常直接在主题行，正文兜底。
6. 填验证码：`input[inputmode="numeric"]`（`:1865`），提交 `button[type="submit"]`（`:1872`）→ Canva 会话建立
7. ~~团队邀请 `www.canva.com/brand/join?token=…`（`:1893`）~~ → **跳过**（新号才需要）

### B. Canva → Leonardo SSO + 抓鉴权
8. 打开 `https://app.leonardo.ai/auth/login`（`:1935`）
9. 点「Continue with Canva」SSO 按钮（多 selector 兜底，回退 `button.hw3gKA`，`:1938-1987`）。同浏览器已有 Canva 会话 → 静默完成 SSO
10. 落 dashboard 后抓两样：
    - 拦 `api.leonardo.ai` 请求头的 `Bearer`（Cognito JWT）
    - cookie **`__Secure-better-auth.session_token`**（`leonardo_auto_create.py:479` 证实当前是 better-auth）

**Canva 登录与 SSO 都是带反爬的 UI 流，无干净 HTTP 接口 → bootstrap 必须跑浏览器，但每账号只跑一次。**

---

## 6. 额度 / 可用性

- Canva-SSO 派生的 Leonardo 号，额度字段含 `apiCredit`（约 8500），`extract_credit_balance` 把 `subscriptionTokens+paidTokens+rolloverTokens+apiCredit+streamTokens` 求和（`leonardo_auto_create.py:374-375`）。
- 用户第一步手工验证已确认：这批账号确有额度、能出图。

---

## 7. 落地到 adobe2api 的架构

### 7.1 模块划分

| 模块 | 职责 | 来源 |
|---|---|---|
| `MailOTPReader`（新增） | `(client_id, refresh_token)` → 兑 Graph token → 读收件箱抠 6 位 OTP；**兑换后写回轮换 token** | §1.3/§5 已验证的 Graph 流程 |
| `LeonardoBootstrapper`（新增） | 无头浏览器跑 §5 的 A+B，产出 `__Secure-better-auth.session_token` cookie（+ bearer） | §5 selector 序列，代码自写 |
| `core/leonardo_client.py`（新增） | cookie → Cognito JWT → GraphQL 出图/上传/查额度 | 移植 leoapi，**next-auth 改 better-auth** |
| `refresh_mgr` 加 `type` 字段 | profile 区分 `adobe`/`leonardo`；Leonardo profile 存 `{client_id, refresh_token, leonardo_cookie}`；cookie 失效时重跑 Bootstrapper | 新增分支 |
| `token_mgr` / `credits_tracker` | 选号、失效摘除、额度记账 | 现有抽象基本账号类型无关，可复用 |

### 7.2 成本模型

**bootstrap（带浏览器，每号一次）→ 之后纯 HTTP 出图 → Leonardo cookie 失效才重 bootstrap**。与 adobe2api 现有"刷新失效才动"的懒刷新逻辑对齐。Leonardo better-auth 会话通常能活数天到数周。

### 7.3 与现有 refresh_profile 的差异

现有 `config/refresh_profile.json` 的 profile 硬编码 Adobe 的 `projectx_webapp` client_id 走纯 HTTP OAuth refresh。Leonardo 分支语义完全不同（邮箱 OTP 重登换 cookie，而非 OAuth grant），故建议加 `type` 字段分流，而非硬塞进现有结构。

---

## 8. 风险与注意事项

1. **token 轮换烧号**（§1.4）：任何成功兑换必须持久化新 refresh_token。
2. **参考仓库外泄**（§4.2）：`create-canva-new` 会把 token 传给第三方 VPS，绝不原样运行。
3. **鉴权栈漂移**：Leonardo 半年内从 next-auth 迁到 better-auth，内部 API "小版本间会变"，开发前需对着真实浏览器再核一次 cookie 名与 GraphQL schema。
4. **风控**：`app.leonardo.ai` 在 Vercel Security Checkpoint 后（leostudio 用 TLS 伪装佐证）；bootstrap 用真实浏览器天然规避，纯 HTTP 段（GraphQL）leoapi 裸 requests 目前够用，必要时上 `curl_cffi`/`tls-client`。
5. **ToS / 封号**：所有网页会话自动化违反 Canva/Leonardo 条款，预期高封号率与 cookie 频繁失效——账号池要按"随时会死"设计。
6. **多进程一致性**：沿用 adobe2api 现状（单进程 + 内存锁）；若上多 worker，账号池需先解决文件锁问题（见账号流程分析文档）。
7. **数据卫生**：账号文件含真实凭据，`.gitignore` 未覆盖 `tests/*.txt`，需加忽略或移出仓库，防 `git add .` 误提交。

---

## 9. 未决 / 下一步

**未端到端验证**：Canva 登录提交 + Canva→Leonardo SSO 抓 cookie 这段（§5 A6–B10）尚未在本环境跑通，只验证到"能读 Canva 验证码"。

**下一步**：写 `MailOTPReader` + `LeonardoBootstrapper` 的最小可跑版本，用样本账号端到端跑通「token → 读 OTP → Canva 登录 → Leonardo SSO → 拿到 better-auth cookie」。跑通后接 leoapi 的出图链即完成。需在环境装 `playwright` + chromium；OTP 段用已验证的 Graph 代码；浏览器段按 §5 selector 自写，**不回传任何 token 到外部**。

**待定项**：账号文件字段 1（13 位串）的用途；bootstrap 失败率与 cookie 实际寿命（决定池规模与重登频率）；是否需要代理池降低风控。

---

## 附：相关文档与仓库

- 账号流程分析（adobe2api 现有 token 池机制）：见 Artifact「adobe2api 账号使用流程分析」。
- 参考仓库（本机，与 adobe2api 平级）：`../leoapi`、`../create-canva-new`。
- 记忆：`memory/leonardo-type-research.md`。
