# Leonardo live-testing fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复或安全收敛真实 Leonardo/Canva API 实测暴露、单测漏掉的 4 个缺陷，测试基准改用真实响应样本，并避免网络重试重复提交付费生成。

**Architecture:** 两个已存在模块的针对性修复。`core/mail_otp.py` 的 OTP 正则补「登录码」并严格限制为 6 位；`core/leonardo_client.py` 三处加固：完成状态字符串、GraphQL errors 不再被吞、只读 GraphQL 操作的传输异常重试。`Generate` mutation 在本仓库尚未确认并接入服务端幂等机制的前提下不自动重放，避免重复生成/扣费；全部沿用注入接缝/monkeypatch，单测零真实网络。

**Tech Stack:** Python 3.10、`pytest`（含 `monkeypatch` fixture）、`requests`。无新依赖。

## Global Constraints

- **Python 3.10**；无新依赖。
- **测试断言以真实 API 响应为准**（本次实测采集）：生成完成态是 `"COMPLETE"`（非 `"COMPLETED"`）；鉴权失败响应形如 `{"errors":[{"message":"Could not verify JWT: JWSError JWSInvalidSignature","extensions":{"code":"invalid-jwt"}}]}`；GraphQL POST 可能瞬时抛 `requests` 传输异常（如 SSLError）。
- **向后兼容**：仍接受历史值 `"COMPLETED"`；不得破坏 `core/leonardo_client.py` / `core/mail_otp.py` 现有通过的单测。
- **重试安全**：仅重试明确列入白名单的只读 GraphQL operation；`Generate` 是非幂等 mutation，本仓库未确认并接入服务端幂等机制前不得自动重试。
- **不打印凭据**；Bearer/OTP 仅作数据处理。

---

### Task 1: `mail_otp` OTP 正则补「登录码」

**Files:**
- Modify: `core/mail_otp.py`
- Test: `tests/test_mail_otp.py`

**Interfaces:**
- 不变对外签名。`extract_canva_otp` 现额外识别「登录码/登陆码/动态码/安全码 + 严格 6 位」，所有语言模式均不得截取 7 位及以上数字串的前 6 位。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_otp.py （追加到文件末尾）
@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("你的登录码是161153", "161153"),
        ("你的登陆码是654321", "654321"),
    ],
)
def test_extract_otp_chinese_login_code(subject, expected):
    # 真实样本：Canva 登录邮件主题用「登录码」而非「验证码」
    assert extract_canva_otp(subject) == expected


def test_extract_otp_rejects_long_digit_run():
    assert extract_canva_otp("Canva code: 1611537") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mail_otp.py -k login_code -v`
Expected: 2 FAIL —— 现有中文正则只认「验证码」，两个参数用例均返回 None。

Run: `python -m pytest tests/test_mail_otp.py -k long_digit_run -v`
Expected: FAIL —— 现有 Canva 通用模式会错误截取 7 位数字的前 6 位。

- [ ] **Step 3: Write minimal implementation**

在 `core/mail_otp.py` 中，将 `_OTP_PATTERNS`：

```python
    r"验证码[是为:：]?\s*(\d{6})",                                       # zh: 验证码是100581
    r"(?:verification|security|login|one[-\s]?time)\s+code\s*(?:is|:)?\s*(\d{6})",  # en
    r"kode\s+canva(?:\s+anda)?\s*(?:adalah|:)?\s*(\d{6})",              # id
    r"\bcode\b[^0-9]{0,10}(\d{6})",                                     # generic: code ... 6 digits
    r"\bcanva\b[^0-9]{0,20}?(\d{6})",                                   # generic: canva ... 6 digits
```

替换为（覆盖中文变体，并给每种语言的 6 位捕获加后置数字边界）：

```python
    r"(?:验证码|登录码|登陆码|动态码|安全码)[是为:：]?\s*(\d{6})(?!\d)",  # zh
    r"(?:verification|security|login|one[-\s]?time)\s+code\s*(?:is|:)?\s*(\d{6})(?!\d)",  # en
    r"kode\s+canva(?:\s+anda)?\s*(?:adalah|:)?\s*(\d{6})(?!\d)",        # id
    r"\bcode\b[^0-9]{0,10}(\d{6})(?!\d)",                               # generic: code
    r"\bcanva\b[^0-9]{0,20}?(\d{6})(?!\d)",                             # generic: canva
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mail_otp.py -v`
Expected: 17 PASS（原 14 + 3 个参数化/边界用例，且原「验证码」用例仍绿）。

- [ ] **Step 5: Commit**

```bash
git add core/mail_otp.py tests/test_mail_otp.py
git commit -m "fix(mail-otp): 识别 Canva「登录码」而非仅「验证码」" \
  -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `leonardo_client` 三处真实 API 加固

**Files:**
- Modify: `core/leonardo_client.py`
- Test: `tests/test_leonardo_client.py`

**Interfaces:**
- `wait_for_completion`：完成态判定改为 `status in ("COMPLETE","COMPLETED")`。
- `LeonardoClient._call`：响应含非空 `errors` 时抛 `LeonardoError`（不再被下游当空数据吞掉）。
- `LeonardoClient._http_gql`：白名单内只读 operation 遇到 `requests` 传输异常时最多请求 `_HTTP_ATTEMPTS` 次；非白名单 operation（含 `Generate`）只请求一次，失败后抛 `LeonardoError`。新增模块常量 `_HTTP_ATTEMPTS=3`、`_RETRY_BACKOFF=0.5`、`_RETRYABLE_OPERATIONS`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_leonardo_client.py （追加到文件末尾；monkeypatch 为 pytest 内置 fixture）
import core.leonardo_client as lc


def test_wait_for_completion_accepts_COMPLETE_status():
    # 真实 API 返回 "COMPLETE"（无 D）
    def fake_gql(token, payload):
        op = payload["operationName"]
        if op == "GetAIGenerationFeedStatuses":
            return {"data": {"generations": [{"id": "g", "status": "COMPLETE"}]}}
        if op == "GetAIGenerationFeed":
            return {"data": {"generations": [{"generated_images": [{"url": "https://cdn/x.jpg"}]}]}}
        return {}
    client = LeonardoClient(gql=fake_gql)
    ticks = iter([0.0, 1.0, 61.0])
    result = client.wait_for_completion("TOK", "g", timeout=60, poll_interval=1,
                                        sleep=lambda _s: None, now=lambda: next(ticks))
    assert result == {"success": True, "images": ["https://cdn/x.jpg"]}


def test_call_raises_on_graphql_errors():
    # 真实鉴权失败样本
    err = {"errors": [{"message": "Could not verify JWT: JWSError JWSInvalidSignature",
                       "extensions": {"code": "invalid-jwt"}}]}
    client = LeonardoClient(gql=lambda t, p: err)
    with pytest.raises(LeonardoError, match="Could not verify JWT"):
        client.get_credits("TOK")
    with pytest.raises(LeonardoError, match="Could not verify JWT"):
        client.poll_status("TOK", "g")


def test_http_gql_retries_transient_query_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    class _R:
        ok = True
        def json(self):
            return {"data": {"user_details": [{"apiCredit": 1}]}}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lc.requests.exceptions.ConnectionError("transient boom")
        return _R()

    monkeypatch.setattr(lc.requests, "post", fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)
    out = lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)
    assert out == {"data": {"user_details": [{"apiCredit": 1}]}}
    assert calls["n"] == 2  # 第一次失败、第二次成功
    assert sleeps == [0.5]


def test_http_gql_raises_after_exhausting_query_retries(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(lc.requests, "post", fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)
    with pytest.raises(LeonardoError, match="after 3 attempts: down"):
        lc.LeonardoClient()._http_gql("TOK", lc.TOKEN_BALANCE_QUERY)
    assert calls["n"] == 3
    assert sleeps == [0.5, 0.5]


def test_http_gql_does_not_retry_generate_mutation(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        raise lc.requests.exceptions.ConnectionError("connection lost")

    monkeypatch.setattr(lc.requests, "post", fake_post)
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)
    payload = {"operationName": "Generate", "query": "mutation Generate { generate { generationId } }"}
    with pytest.raises(LeonardoError, match="not retried to avoid duplicate side effects"):
        lc.LeonardoClient()._http_gql("TOK", payload)
    assert calls["n"] == 1
    assert sleeps == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_leonardo_client.py -k "COMPLETE or graphql_errors or http_gql" -v`
Expected: 5 FAIL，且命令会正常退出 —— 完成态只认 COMPLETED；`_call` 不检查 errors；`_http_gql` 不包装/重试传输异常。有限 `ticks` 会让 COMPLETE 用例在旧实现上返回 timeout 后断言失败，不会无限轮询。

- [ ] **Step 3: Write the implementation**

在 `core/leonardo_client.py`：

(a) 完成态。将 `wait_for_completion` 内：

```python
            if status == "COMPLETED":
```

改为：

```python
            if status in ("COMPLETE", "COMPLETED"):
```

(b) `_call` 检查 errors。将：

```python
    def _call(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        fn = self._gql_fn or self._http_gql
        return fn(token, payload)
```

改为：

```python
    def _call(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        fn = self._gql_fn or self._http_gql
        resp = fn(token, payload)
        if isinstance(resp, dict) and resp.get("errors"):
            messages = [
                str(e.get("message", "")).strip()
                for e in resp["errors"]
                if isinstance(e, dict)
            ]
            raise LeonardoError("; ".join([m for m in messages if m]) or "graphql error")
        return resp
```

(c) `_http_gql` 重试。在模块常量区（`GRAPHQL_URL` 附近）新增：

```python
_HTTP_ATTEMPTS = 3
_RETRY_BACKOFF = 0.5
_RETRYABLE_OPERATIONS = frozenset({
    "GetTokenBalance",
    "GetAIGenerationFeedStatuses",
    "GetAIGenerationFeed",
})
```

并将 `_http_gql` 改为：

```python
    def _http_gql(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = dict(_BASE_HEADERS)
        headers["authorization"] = f"Bearer {token}"
        operation = str(payload.get("operationName") or "")
        attempts = _HTTP_ATTEMPTS if operation in _RETRYABLE_OPERATIONS else 1
        for attempt in range(attempts):
            try:
                resp = requests.post(GRAPHQL_URL, headers=headers, json=payload, timeout=60)
            except requests.exceptions.RequestException as exc:
                if attempt < attempts - 1:
                    time.sleep(_RETRY_BACKOFF)
                    continue
                if attempts > 1:
                    message = f"graphql {operation} failed after {attempts} attempts: {exc}"
                else:
                    message = (
                        f"graphql {operation or 'request'} failed; request not retried "
                        f"to avoid duplicate side effects: {exc}"
                    )
                raise LeonardoError(message) from exc
            if not resp.ok:
                raise LeonardoError(f"graphql HTTP {resp.status_code}")
            return resp.json()
        raise LeonardoError("graphql request failed")  # 理论不可达
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_leonardo_client.py -v`
Expected: 32 PASS（原 27 + 5；原有 `test_wait_for_completion_polls_then_succeeds` 使用 "COMPLETED"，仍因兼容判定通过）。

- [ ] **Step 5: Run the full regression suite**

Run: `python -m pytest -q`
Expected: PASS；不得只运行两个修改文件的局部测试。

- [ ] **Step 6: Commit**

```bash
git add core/leonardo_client.py tests/test_leonardo_client.py
git commit -m "fix(leonardo): 安全处理完成态、GraphQL 错误和传输重试" \
  -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage**：4 个实测 bug 均有明确处理 —— Task 1 修「登录码」并拒绝长数字串截断（bug#4）；Task 2 修完成态 COMPLETE（bug#1，致命）、errors 吞成 PENDING（bug#2）、只读请求瞬断无重试/传输异常类型泄漏（bug#3）。`Generate` 瞬断会统一抛 `LeonardoError`，但不会在缺少幂等保证时自动重放；这是显式安全边界，不宣称自动恢复生成。
- **Placeholder scan**：无占位项；每步含可运行代码与真实样本断言。
- **不破坏既有**：`wait_for_completion` 新判定兼容旧 "COMPLETED"（旧测试 `test_wait_for_completion_polls_then_succeeds` 仍过）；`_call` 仅在 `errors` 非空时抛，现有 happy-path 测试的 fake 返回均无 errors，不受影响；`extract_canva_otp` 扩充中文模式并给既有捕获统一增加数字边界。
- **重试安全**：`_RETRYABLE_OPERATIONS` 只包含三个只读查询；测试分别锁定成功重试、3 次耗尽/2 次退避，以及 Generate 仅调用一次。
- **Type consistency**：`LeonardoError`（Task 1 依赖 core.leonardo_client 中已定义者？否——mail_otp 用自身 MailOTPError，Task 1 不涉及）；Task 2 的 `LeonardoError`、`LeonardoClient`、`build_status_query/feed` 均沿用既有名。
- **给实现者**：Task 2 的 monkeypatch 测试需 `import core.leonardo_client as lc` 于测试文件顶部导入区（并入现有导入，勿重复）；`lc.requests`/`lc.time` 已由模块导入，可被 `monkeypatch.setattr` 替换。
