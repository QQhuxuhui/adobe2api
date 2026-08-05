# Leonardo 登录账号后台批量导入 + 状态告警 — 设计

## 背景与目标

Leonardo 的 better-auth 会话约 1.6h 被上游作废;已由 leonardo-refresher 自动登录重登解决
(v9:住宅代理 + YesCaptcha 解 Turnstile + `POST /api/auth/sign-in/email` 带 `x-captcha-response`)。
当前登录账号靠环境变量 `LEONARDO_LOGIN_ACCOUNTS`(JSON)配置,改一次要改 .env + 重建容器。

**目标**:把登录账号改为**后台页面批量导入**(email/password,每行一条),持久化存储,账号列表按
邮箱显示;并加**余额低 / 账号连续重登失败**告警。代理复用系统现有的 `LEONARDO_PROXY`。

## 已确认的决策

1. 导入格式:多行,每行 `邮箱:密码`,**按首个冒号分隔**(邮箱不含冒号;密码可含任意字符含冒号)。
2. 列表显示名称:**邮箱全文**(无需回写 user.name)。
3. 告警渠道:**后台页面 + 日志**(不加推送)。
4. 代理:复用现有 `LEONARDO_PROXY`(refresher 已全程走它)。
5. 状态/余额到后台:**方案 A —— refresher 回报**(YesCaptcha key 只在 refresher 一处;
   状态与余额走同一条回写通道,与现有"推 token"模式一致)。

## 架构(镜像现有 Leonardo Cookie 导入)

三层,与 cookie 导入完全对称:

- **adobe2api 存储 + 端点**(`api/routes/leonardo_tokens.py` 存储helper、`api/routes/admin.py` 管理端点)
- **后台 UI**(`static/admin.js` + 模板:新增「导入 Leonardo 账号」弹窗与账号列表)
- **refresher**(`leonardo_refresher/`:从端点拉登录账号、登录后回写状态与余额)

## ① 存储(adobe2api,`api/routes/leonardo_tokens.py`)

新增 `config/leonardo_logins.json`,镜像 `leonardo_cookies.json`:

```json
{
  "logins": [
    {"id": "<uuid12>", "email": "a@b.co", "password": "<明文>",
     "updated_at": 1730000000, "status": "pending",
     "fail_count": 0, "last_attempt_at": null}
  ],
  "yescaptcha_balance": null,
  "balance_at": null
}
```

- `status`: `pending`(刚导入未登录)/ `ok`(登录+续期正常)/ `login_required`(登录失败)。
- 密码**明文存储**(与 cookie 明文一致,同一信任边界);状态接口**绝不回传密码**。

函数(纯函数,便于测试):
- `store_leonardo_logins(raw: str) -> {added, updated, skipped, count}`:按行 `split(":", 1)`;
  邮箱去重 upsert(已存在则更新密码、`status="pending"`、`fail_count=0`);空行/无冒号/空邮箱→skipped。
- `list_leonardo_logins() -> [{id, email, password}]`:含明文,仅 refresh-key 端点内部用。
- `read_leonardo_login_status() -> {logins:[{id,email,status,fail_count,updated_at,last_attempt_at}], count, yescaptcha_balance, balance_at}`:**不含密码**。
- `remove_leonardo_login(id) -> {removed, count}`。
- `update_login_report(id, status) -> {updated}`:按 id 就地更新 `status` + `last_attempt_at`;
  **`fail_count` 由存储侧维护**:`status="login_required"` → `fail_count += 1`;`status="ok"` → `fail_count = 0`。
  (存储是唯一真相,survive refresher 重启;refresher 只需报 status。)
- `set_yescaptcha_balance(n) -> None`。

## ② 端点

**管理端点**(`admin.py`,走管理会话鉴权,仿 cookie 三件套):
- `POST /api/v1/leonardo/login`  body `{text}` → `store_leonardo_logins` → `{added,updated,skipped,count}`
- `GET  /api/v1/leonardo/login/status` → `read_leonardo_login_status`
- `DELETE /api/v1/leonardo/login/{id}` → `remove_leonardo_login`

**refresh-key 端点**(`leonardo_tokens.py`,`X-Leonardo-Refresh-Key` 鉴权):
- `GET  /api/v1/tokens/leonardo/logins` → `{logins:[{id,email,password}]}`
- `POST /api/v1/tokens/leonardo/login/report` body `{id,status,balance?}` → `update_login_report` (+ `set_yescaptcha_balance`;fail_count 由存储维护)

## ③ refresher(`leonardo_refresher/`)

- `Adobe2ApiCookieProvider`:加 `fetch_logins()`(GET `.../logins`)、`report_login(id,status,balance=None)`(POST `.../login/report`)。网络/HTTP 错误按现有风格归类(不打断刷新主流程)。
- `PlaywrightSessionSource.list_cookies()`:登录账号来源从 **env 改为 `fetch_logins()`**;cid 用存储的 `id`;yield `(id, "email\npassword", LOGIN_MARKER)`。env `LEONARDO_LOGIN_ACCOUNTS` **保留为兜底 union**(端点拿不到时仍可用),但主路径是端点。
- `_fetch_token_login`:成功 → `report_login(id, "ok", balance)`;失败(LoginRequiredError)→ `report_login(id, "login_required", balance)`。fail_count 全由存储侧维护(见 ①),refresher 不记忆计数。
- 余额:`_solve_turnstile` 成功后顺带调一次 YesCaptcha `getBalance`(登录本就低频,~1.6h 一次),把余额随 `report_login` 一起上报。避免额外定时器。

## ④ 后台 UI(`static/admin.js` + 模板)

- 新增「导入 Leonardo 账号」弹窗(与现有「导入 Cookie」并列,**cookie 入口保留**):多行 textarea,
  占位提示 `邮箱:密码(每行一条)`;提交调 `POST /api/v1/leonardo/login`,回显 `added/updated/skipped`。
- 账号列表:每行 **邮箱全文** + 状态徽标(`ok`绿 / `login_required` 或 `fail_count≥3` 红 / `pending` 灰)+ 删除钮(调 DELETE)。
- 顶部:**YesCaptcha 余额**(`balance < 阈值` 红字 + "余额偏低,请充值")。
- 阈值(前端常量,易改):连续失败 `fail_count ≥ 3` 标红;`balance < 500` 标红。

## 数据流

导入(admin)→ 存储 → refresher `fetch_logins` → 登录/续期 → `report_login`(status + balance)→ 存储
→ 后台 `login/status` 显示。

## 错误处理

- 导入非法行(无冒号/空邮箱/空密码)→ 跳过并计入 `skipped`,不阻断其它行。
- 登录失败 → `status=login_required`、`fail_count++`;成功归零。前端连续≥3 红标。
- refresh-key 鉴权失败 401;未配 key 503(复用现有 `_require_refresh_key`)。
- **密码绝不出现在** `login/status` 响应或任何日志(`[leo-login]` 只打邮箱前缀)。

## env 迁移

主路径改端点;`LEONARDO_LOGIN_ACCOUNTS` 保留兜底。部署时把现有 env 里的账号
(`arif95750@qw2.biz.id`)通过新导入接口迁入存储一次,之后可从 .env 清掉。

## 测试(沿用现有 fake 模式)

- `store_leonardo_logins`:首冒号分隔、去重 upsert、非法行跳过、count 正确。
- `read_leonardo_login_status`:不漏密码。
- `remove_leonardo_login` / `update_login_report` / `set_yescaptcha_balance`。
- 管理端点(仿 `test_admin_leonardo_cookie.py`)。
- `fetch_logins` / `report_login` round-trip;`list_cookies()` 走端点(fake provider)。
- refresher 登录路径回写状态(扩展现有 `_LoginCtx` fake)。

## 部署

- adobe2api 改了 routes + 前端 → 构建 v52。
- refresher 改了 adapters → 构建 v10。
- 顺序:先 adobe2api(端点就绪)后 refresher;迁移 env 账号;`--no-deps` 单独重建各容器。

## 非目标(YAGNI)

- 不做推送告警(仅后台+日志)。
- 不回写/显示 user.name(列表用邮箱)。
- 不移除 cookie 导入路径(仍保留)。
- 不做密码加密存储(与现有 cookie 明文同一边界;如需加密另立项)。
