# Leonardo 登录账号后台批量导入 + 状态告警 — 设计

## 背景与目标

Leonardo 的 better-auth 会话约 1.6h 被上游作废;已由 leonardo-refresher 自动登录重登解决
(v9:住宅代理 + YesCaptcha 解 Turnstile + `POST /api/auth/sign-in/email` 带 `x-captcha-response`)。
当前登录账号靠环境变量 `LEONARDO_LOGIN_ACCOUNTS`(JSON)配置,改一次要改 .env + 重建容器。

**目标**:把登录账号改为**后台页面批量导入**(email/password,每行一条),持久化存储,账号列表按
邮箱显示;并加**余额低 / 账号连续重登失败**告警。代理复用系统现有的 `LEONARDO_PROXY`。

## 已确认的决策

1. 导入格式:多行,每行 `邮箱:密码`,**按首个冒号分隔**(邮箱不含冒号;密码可含**除换行外任意字符**——
   含冒号/空格;解析时**不对密码 strip**,只去行尾 `\r\n`)。
2. 列表显示名称:**邮箱全文**(无需回写 user.name)。
3. 告警渠道:**后台页面 + 日志**(不加推送)。
4. 代理:复用现有 `LEONARDO_PROXY`——**仅 Chromium(登录 + get-session)走该代理**;YesCaptcha 用
   `TurnstileTaskProxyless`(其自有 IP),YesCaptcha API 及对 adobe2api 的回报**不走**该代理。
5. 状态/余额到后台:**方案 A —— refresher 回报**。

## 架构(镜像现有 Leonardo Cookie 导入)

三层,与 cookie 导入对称:adobe2api 存储 + 端点 / 后台 UI / refresher。**但存储不复用 cookie 的裸
`write_text`**(见 ①)。

## ① 存储 —— 单一 `LeonardoLoginStore` 类(线程安全 + 原子写)

现有 cookie 存储直接 `path.write_text`(`api/routes/leonardo_tokens.py:89`),**非原子、并发不安全**。
FastAPI 同步接口在线程池并发运行,导入/删除/状态回报/余额回报都是 read-modify-write,会互相覆盖或写坏文件。
新增 `LeonardoLoginStore`(独立模块,如 `api/routes/leonardo_login_store.py`;模块级单例),持久化
`config/leonardo_logins.json`:

```json
{
  "logins": [
    {"id": "<uuid12>", "email": "a@b.co", "password": "<明文>",
     "credential_rev": 1, "status": "pending", "fail_count": 0,
     "last_error_kind": null, "updated_at": 1730000000, "last_attempt_at": null}
  ],
  "yescaptcha_balance": null,
  "balance_at": null
}
```

存储不变量:
- **单一 `threading.RLock`**:所有读写走它;`report()` 的状态 + 余额**一次事务、只 save 一次**(不连续保存两次)。
- **原子写**:写临时文件 → `flush`+`os.fsync` → `os.replace`(参考 `core/token_mgr.py:117` 的安全落盘);
  新文件权限固定 `0600`。
- **损坏保护**:读到损坏 JSON → 抛错,**拒绝以空数据覆盖**(绝不把损坏当空继续保存,防止一次坏读清库)。
- `credential_rev`:密码变化 +1(驱动 ③ 立即重验);`status`: `pending`/`ok`/`login_required`;
  `last_error_kind`: `password`/`captcha`/`proxy`/`upstream`(区分失败原因)。
- 新 JSON 与 `leonardo_cookies.json` 同目录(`CONFIG_DIR`),该目录已由 Compose 持久化,**无需新增 volume**。

方法(全部在锁内):
- `import_lines(raw) -> {added,updated,skipped,count}`:按行 `partition(":")`;email=`strip(左).lower()` 规范化;
  password=右侧**仅 `rstrip("\r\n")`、不 strip**;email 去重 upsert(密码变→`credential_rev+=1`、`status="pending"`、
  `fail_count=0`);空行 / 无冒号 / 空邮箱 / 空密码 → 计入 skipped、不阻断其它行。
- `list_for_refresher() -> [{id,email,password,credential_rev}]`(含明文,仅 refresh-key 端点内部用)。
- `status_view() -> {logins:[{id,email,status,fail_count,last_error_kind,updated_at,last_attempt_at}], count, yescaptcha_balance, balance_at, thresholds:{fail_count, yescaptcha_balance}}`(**无密码**)。
  阈值取后端模块常量(`FAIL_ALERT_THRESHOLD=3`、`BALANCE_ALERT_THRESHOLD=1000`),**日志告警与前端共用同一来源**,避免只改一边。
- `remove(id) -> {removed,count}`。
- `report(id, status, last_error_kind=None, balance=None) -> None`:一次事务(只 save 一次)。**固定状态转换**:
  - `ok` → `fail_count=0`、**`last_error_kind=None`(显式清旧错,避免恢复后还挂 password/captcha)**;
  - `login_required` → `fail_count+=1`、`last_error_kind=<新错误>`;
  - (`pending` 只由导入 / 改密码产生,不经 report。)
  给了 `balance` 就更新 `yescaptcha_balance`/`balance_at`(float);并做阈值跨越告警日志(见 ④)。

## ② 端点

**管理端点**(`admin.py`,管理会话鉴权,仿 cookie 三件套):
- `POST /api/v1/leonardo/login`  body `{text}` → `import_lines` → `{added,updated,skipped,count}`
- `GET  /api/v1/leonardo/login/status` → `status_view`
- `DELETE /api/v1/leonardo/login/{id}` → `remove`

**refresh-key 端点**(`leonardo_tokens.py`,`X-Leonardo-Refresh-Key` 鉴权):
- `GET  /api/v1/tokens/leonardo/logins` → `{logins: list_for_refresher()}`
- `POST /api/v1/tokens/leonardo/login/report` body `{id,status,last_error_kind?,balance?}` → `report`

## ③ refresher

- `Adobe2ApiCookieProvider`:加 `fetch_logins()`(GET `.../logins`,返回含 password+credential_rev)、
  `report_login(id,status,last_error_kind=None,balance=None)`(POST `.../login/report`)。网络/HTTP 错误
  归为可重试、**不打断刷新主流程**;登录端点拉取失败**不得影响 cookie 账号继续刷新**。
- `list_cookies()` 登录账号来源(**不做无条件 union**,否则同邮箱既有存储 UUID 又有 env 哈希 ID → 两个 context
  → 会话轮换污染,违反"每账号一 context"`adapters.py:205`):
  - 端点成功(**含空列表**)→ **以存储为准**,按规范化邮箱去重;空列表=无登录账号,**不回退**;
  - 端点拉取**异常**(网络/HTTP 错误)→ 回退 env `LEONARDO_LOGIN_ACCOUNTS`;
  - cid 用存储 `id`;登录条目 fingerprint 编码 `f"{LOGIN_MARKER}:{credential_rev}"`(承载 rev)。
- `fetch_token_for`:`fingerprint.startswith(LOGIN_MARKER)` → 走 `_fetch_token_login`(其余 cookie 路径不变)。
- **凭据变更立即重验(service 侧,仅登录账号)**:现有 gate 在 token 有效期内跳过刷新(`service.py:229`),
  且登录 fingerprint 恒定就永不重注入(`adapters.py:411`)——改密码近 1h 不生效、status 卡 pending。修法:
  service 记 `_login_fp={cid:fp}`;某**登录** cid 的 fp 变化(=rev 变)→ 清 `_known[cid]`、`_retry_after[cid]`
  并 `source.drop_context(cid)` → 本轮立即重登验证新密码。**cookie 账号不走此逻辑**(其指纹变化是正常轮换,清了会误刷)。
- `PlaywrightSessionSource.drop_context(cid)`:关闭并移除 `self._accounts[cid]`。**删除账号回收**:被删 cid 不再出现在
  list → service prune 阶段对缺席 cid 调 `drop_context` → 避免频繁导入/删除积累 context。
- `_fetch_token_login`:成功 → `report_login(id,"ok",balance=bal)`;失败按异常映射 `last_error_kind`
  → `report_login(id,"login_required",kind,bal)`。fail_count 全由存储维护,refresher 不记忆计数。
- 余额:**每次登录尝试都独立调一次 `getBalance`**(与验证码成功/失败无关——余额耗尽时 `createTask` 恰好会失败,
  正是此刻更要查到低余额、触发告警);`getBalance` 自身失败则 balance 传 `None`、保留旧值。随 `report_login` 上报。

## ④ 后台 UI + 日志告警

**前端**(`static/admin.js` + 模板):
- 「导入 Leonardo 账号」弹窗(与「导入 Cookie」并列,**cookie 入口保留**):多行 textarea(占位 `邮箱:密码,每行一条`);
  提交 `POST /api/v1/leonardo/login`,回显 added/updated/skipped。
- 账号列表每行:**邮箱全文** + 状态徽标(`ok`绿 / `login_required` 或 `fail_count≥3` 红 / `pending`灰)+
  `last_error_kind` + 删除钮(调 DELETE)。
- 顶部:**YesCaptcha 余额**(`< 阈值` 红字 + "余额偏低,请充值")。
- **状态获取时机**:不只弹窗打开——**页面加载 + Token 列表刷新时**一并拉 `login/status`(否则是查询、不像告警)。
- 阈值(前端常量,易改):`fail_count ≥ 3` 红;`balance < 1000` 红(YesCaptcha 单位=积分,1 元 = 1000 积分,
  <1000 约剩几十次解码)。

**日志告警**(在存储 `report()` 内,**仅状态跨阈值时记一行,避免每次 report 刷屏**):
- 某账号 `fail_count` 首次达到 3(连续登录失败)
- 失败账号恢复 `ok`
- 余额首次跌破阈值
- 余额恢复到阈值以上

## ⑤ 请求模型与校验(Pydantic)

- `POST /api/v1/leonardo/login`:独立模型 `{text: str}`,限制长度(如 ≤ 200KB,防超大 body)。
- `POST .../login/report`:`{id:str, status:Literal["ok","login_required"], last_error_kind:Optional[Literal["password","captcha","proxy","upstream"]], balance:Optional[float]}`;
  校验 status/last_error_kind 枚举、balance 为有限非负数(YesCaptcha 官方为 Decimal → 用 float/Decimal,不用 int)。
- **密码不出现在管理端响应、`status_view`、日志、导出中**;refresh-key `/logins` 因 refresher 登录**必须**返回密码
  (属内部信任通道);`[leo-login]` 只打邮箱前缀。

## 数据流

导入(admin)→ `LeonardoLoginStore` → refresher `fetch_logins`(含 rev)→ 登录/续期 → `report_login`
(status + last_error_kind + balance)→ store 一次事务更新 → 后台 `login/status` 显示。改密码 → rev+1 →
refresher 下轮清 _known/context → 立即重登验证。

## 错误处理

- 导入非法行 → 计入 skipped、不阻断其它行。
- 登录失败 → `status=login_required`、存储 `fail_count++`、记 `last_error_kind`;成功归零。前端连续≥3 红标。
- 登录端点拉取失败 → 回退 env;**不影响 cookie 账号刷新**。
- refresh-key 鉴权失败 401;未配 key 503(复用 `_require_refresh_key`)。
- 存储损坏 → 拒绝覆盖、抛错(不清库)。

## env 迁移

主路径改端点;`LEONARDO_LOGIN_ACCOUNTS` 仅在**端点拉取失败时**兜底(非 union)。部署时把现有 env 账号
(`arif95750@qw2.biz.id`)经新导入接口迁入存储一次,之后可从 .env 清掉。

## 测试(沿用现有 fake 模式)

- `LeonardoLoginStore`:首冒号分隔、密码不 strip、去重 upsert、改密码 `credential_rev+1`+status 重置、非法行 skipped;
  `report` 的 fail_count 增/归零 + 阈值跨越日志(捕获日志断言只在跨越时打);原子写(temp+replace)、0600、损坏拒覆盖。
- `status_view` 不漏密码;Pydantic 校验(status 枚举 / balance 非负 / text 超限)。
- 管理端点(仿 `tests/test_admin_leonardo_cookie.py`)。
- `fetch_logins`/`report_login` round-trip;`list_cookies()` 端点成功走存储、失败回退 env;`drop_context` 被调。
- refresher 登录路径:rev 变化清 _known+drop_context+立即重登(扩展现有 `_LoginCtx` fake);删除 → prune 掉 context。
- **并发** import/report/delete(多线程同时打 store,断言不丢数据/不损坏/计数正确)。
- 验证码失败(createTask 失败)**仍上报低余额**(getBalance 独立于 solve)。
- `report("ok")` **清空** 旧 `last_error_kind`;`login_required` 累加 fail_count。
- 端点返回**空列表**(成功但无账号)→ **不回退 env**(空是有效成功,非失败)。

## 部署

- adobe2api 改了 routes + 存储 + 前端 → 构建 v52;refresher 改了 adapters/provider → v10。
- **安全顺序**(端点成功但列表为空**不回退 env**,故必须先导入再升级 refresher,否则账号池瞬时清空):
  ①部署 adobe2api v52 → ②refresher **保持 v9 运行** → ③后台导入并确认账号入库 → ④部署 refresher v10 →
  ⑤清 .env 里 `LEONARDO_LOGIN_ACCOUNTS`。`--no-deps` 单独重建;`config/` 已持久化,无需新 volume。

## 非目标(YAGNI)

- 不做推送告警(仅后台 + 日志)。
- 不回写/显示 user.name(列表用邮箱)。
- 不移除 cookie 导入路径(保留)。
- 不做密码加密存储(与现有 cookie 明文同一信任边界;但**必须** 0600 + 不入日志/导出。如需加密另立项)。
