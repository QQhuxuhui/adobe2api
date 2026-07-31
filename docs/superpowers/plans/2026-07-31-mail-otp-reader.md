# MailOTPReader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用微软邮箱 refresh token 读取收件箱、抠出 Canva 的 6 位验证码，并在兑换 token 轮换时把新 token 交还调用方持久化。

**Architecture:** 单模块 `core/mail_otp.py`，四个纯/半纯函数按依赖递进：`extract_canva_otp`（纯正则）→ `redeem_graph_token`（换 Graph token，处理轮换）→ `fetch_latest_canva_otp`（读收件箱选最新 Canva 验证码）→ `get_otp`（编排：兑换→轮询→回调）。所有网络调用通过可注入的 `http_post`/`http_get`/`sleep`/`now` 接缝暴露，单元测试全程零网络。

**Tech Stack:** Python 3.10、`requests`（项目已依赖，见 `core/refresh_mgr.py` 的 `requests.post`）、`pytest`（项目现有测试框架，见 `tests/`）。本计划**不引入任何新依赖**。

## Global Constraints

- **Python 3.10**（`Dockerfile` 基础镜像 `python:3.10-slim-bullseye`）。
- **不新增依赖**：仅用 `requests` + 标准库。playwright 属于后续 LeonardoBootstrapper 计划，本计划不涉及。
- **token 轮换必须持久化**：MSA refresh token 兑换成功即轮换，旧的立即失效。任何成功兑换后返回的新 refresh token 必须交还调用方（通过返回值或 `on_rotate` 回调），由调用方写回存储。
- **多语言 OTP**：Canva 验证码邮件按邮箱 locale 可能是中文/英文/印尼文，正则必须同时覆盖三者。样本主题为中文「…验证码是 <6位>」。
- **绝不外泄凭据**：不得把 access_token / refresh_token / OTP 值写入日志或任何外部请求；本模块只与 `login.microsoftonline.com` 和 `graph.microsoft.com` 通信。
- **端点固定**：
  - token：`https://login.microsoftonline.com/consumers/oauth2/v2.0/token`
  - 收信：`https://graph.microsoft.com/v1.0/me/messages`
  - Graph token scope：`https://graph.microsoft.com/.default`

---

### Task 1: OTP 提取（纯函数，多语言）

**Files:**
- Create: `core/mail_otp.py`
- Test: `tests/test_mail_otp.py`

**Interfaces:**
- Consumes: 无（纯函数，标准库 `re`）。
- Produces: `extract_canva_otp(text: str) -> Optional[str]` —— 从邮件主题+正文文本中返回 6 位验证码字符串，找不到返回 `None`。供 Task 3、Task 4 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_otp.py
from core.mail_otp import extract_canva_otp


def test_extract_otp_chinese_subject():
    assert extract_canva_otp("你的Canva可画验证码是100581") == "100581"


def test_extract_otp_english():
    assert extract_canva_otp("Your Canva verification code is 482913") == "482913"


def test_extract_otp_indonesian():
    assert extract_canva_otp("Kode Canva anda adalah 123456") == "123456"


def test_extract_otp_generic_code_colon():
    assert extract_canva_otp("Canva\nYour code: 654321") == "654321"


def test_extract_otp_none_when_no_code():
    assert extract_canva_otp("Welcome to Canva, let's get started") is None


def test_extract_otp_ignores_unrelated_numbers():
    # 无 canva/code/验证码 语境的 6 位数字不应误命中
    assert extract_canva_otp("Order #123456 has shipped") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mail_otp.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'core.mail_otp'`（或 `ImportError: extract_canva_otp`）。

- [ ] **Step 3: Write minimal implementation**

```python
# core/mail_otp.py
import re
from typing import Optional

_OTP_PATTERNS = [
    r"验证码[是为:：]?\s*(\d{6})",                                       # zh: 验证码是100581
    r"(?:verification|security|login|one[-\s]?time)\s+code\s*(?:is|:)?\s*(\d{6})",  # en
    r"kode\s+canva(?:\s+anda)?\s*(?:adalah|:)?\s*(\d{6})",              # id
    r"\bcode\b[^0-9]{0,10}(\d{6})",                                     # generic: code ... 6 digits
    r"\bcanva\b[^0-9]{0,20}?(\d{6})",                                   # generic: canva ... 6 digits
]


def extract_canva_otp(text: str) -> Optional[str]:
    """从邮件主题+正文中提取 Canva 6 位验证码，覆盖中/英/印尼文，找不到返回 None。"""
    if not text:
        return None
    for pattern in _OTP_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mail_otp.py -v`
Expected: PASS（6 个用例全绿）。

- [ ] **Step 5: Commit**

```bash
git add core/mail_otp.py tests/test_mail_otp.py
git commit -m "feat(mail-otp): multi-locale Canva OTP extraction"
```

---

### Task 2: 兑换 Graph token（处理轮换）

**Files:**
- Modify: `core/mail_otp.py`
- Test: `tests/test_mail_otp.py`

**Interfaces:**
- Consumes: 无（HTTP 通过注入的 `http_post` 接缝）。
- Produces:
  - `MailOTPError(Exception)` —— 本模块统一异常基类。
  - `redeem_graph_token(client_id: str, refresh_token: str, *, http_post=None) -> Tuple[str, str]` —— 返回 `(access_token, current_refresh_token)`；响应含新 refresh token 则返回新的，否则回退旧的。失败抛 `MailOTPError`。供 Task 4 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_otp.py  （追加）
import pytest
from core.mail_otp import redeem_graph_token, MailOTPError


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_redeem_returns_access_and_rotated_refresh():
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "NEW_RT"})

    access, refresh = redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
    assert access == "AT"
    assert refresh == "NEW_RT"
    assert calls["url"] == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    assert calls["data"]["grant_type"] == "refresh_token"
    assert calls["data"]["scope"] == "https://graph.microsoft.com/.default"


def test_redeem_falls_back_to_old_refresh_when_not_rotated():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT"})  # 无 refresh_token

    access, refresh = redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
    assert access == "AT"
    assert refresh == "OLD_RT"


def test_redeem_raises_on_error_status():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(400, {"error": "invalid_grant"}, text='{"error":"invalid_grant"}')

    with pytest.raises(MailOTPError):
        redeem_graph_token("CID", "OLD_RT", http_post=fake_post)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mail_otp.py -k redeem -v`
Expected: FAIL —— `ImportError: cannot import name 'redeem_graph_token'`。

- [ ] **Step 3: Write minimal implementation**

```python
# core/mail_otp.py  （在文件顶部 import 区追加）
from typing import Optional, Tuple

import requests

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class MailOTPError(Exception):
    """MailOTPReader 统一异常。"""


def redeem_graph_token(
    client_id: str,
    refresh_token: str,
    *,
    http_post=None,
) -> Tuple[str, str]:
    """用 refresh token 兑换 Graph access token。返回 (access_token, current_refresh_token)。
    MSA refresh token 兑换即轮换：若响应含新 refresh token 返回新的，否则回退旧的。"""
    http_post = http_post or requests.post
    resp = http_post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": GRAPH_SCOPE,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise MailOTPError(f"token redeem failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    access = data.get("access_token")
    if not access:
        raise MailOTPError("token redeem response missing access_token")
    new_refresh = data.get("refresh_token") or refresh_token
    return access, new_refresh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mail_otp.py -k redeem -v`
Expected: PASS（3 个用例全绿）。

- [ ] **Step 5: Commit**

```bash
git add core/mail_otp.py tests/test_mail_otp.py
git commit -m "feat(mail-otp): redeem Graph token with refresh rotation handling"
```

---

### Task 3: 读收件箱选最新 Canva 验证码

**Files:**
- Modify: `core/mail_otp.py`
- Test: `tests/test_mail_otp.py`

**Interfaces:**
- Consumes: `extract_canva_otp`（Task 1）、`MailOTPError`（Task 2）。
- Produces: `fetch_latest_canva_otp(access_token: str, *, since_ts: Optional[float] = None, http_get=None) -> Optional[Tuple[str, float]]` —— 从收件箱找发件人含 `canva`、`receivedDateTime` 晚于 `since_ts`、最新的一封，返回 `(otp, received_epoch)`；无则 `None`。供 Task 4 使用。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_otp.py  （追加）
from core.mail_otp import fetch_latest_canva_otp


def _msg(addr, subject, received, body=""):
    return {
        "from": {"emailAddress": {"address": addr}},
        "subject": subject,
        "bodyPreview": body,
        "receivedDateTime": received,
    }


def test_fetch_returns_newest_canva_otp():
    payload = {"value": [
        _msg("noreply@canva.com", "你的Canva可画验证码是100581", "2026-07-30T14:47:00Z"),
        _msg("news@e.adobe.com", "Welcome", "2026-07-30T13:00:00Z"),
    ]}

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer AT"
        return _FakeResp(200, payload)

    otp, ts = fetch_latest_canva_otp("AT", http_get=fake_get)
    assert otp == "100581"
    assert ts > 0


def test_fetch_ignores_non_canva_and_returns_none():
    payload = {"value": [_msg("news@e.adobe.com", "Verification code is 999999", "2026-07-30T14:47:00Z")]}

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, payload)

    assert fetch_latest_canva_otp("AT", http_get=fake_get) is None


def test_fetch_skips_messages_at_or_before_since_ts():
    # since_ts 设为该邮件时间之后，应过滤掉它
    payload = {"value": [_msg("noreply@canva.com", "验证码是100581", "2026-07-30T14:47:00Z")]}

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, payload)

    future = 4102444800.0  # 2100-01-01
    assert fetch_latest_canva_otp("AT", since_ts=future, http_get=fake_get) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mail_otp.py -k fetch -v`
Expected: FAIL —— `ImportError: cannot import name 'fetch_latest_canva_otp'`。

- [ ] **Step 3: Write minimal implementation**

```python
# core/mail_otp.py  （在 import 区追加 datetime；在文件内追加函数）
from datetime import datetime, timezone

GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"
_MESSAGES_QUERY = (
    "?$top=15&$select=from,subject,bodyPreview,receivedDateTime"
    "&$orderby=receivedDateTime%20desc"
)


def _parse_iso_epoch(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def fetch_latest_canva_otp(
    access_token: str,
    *,
    since_ts: Optional[float] = None,
    http_get=None,
) -> Optional[Tuple[str, float]]:
    """读收件箱，返回最新一封 Canva 验证码邮件的 (otp, received_epoch)；无则 None。"""
    http_get = http_get or requests.get
    resp = http_get(
        GRAPH_MESSAGES + _MESSAGES_QUERY,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise MailOTPError(f"graph messages failed: HTTP {resp.status_code}")
    for msg in resp.json().get("value", []):
        addr = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        if "canva" not in addr:
            continue
        received = _parse_iso_epoch(msg.get("receivedDateTime") or "")
        if since_ts is not None and received is not None and received <= since_ts:
            continue
        text = (msg.get("subject") or "") + "\n" + (msg.get("bodyPreview") or "")
        otp = extract_canva_otp(text)
        if otp:
            return otp, (received or 0.0)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mail_otp.py -k fetch -v`
Expected: PASS（3 个用例全绿）。

- [ ] **Step 5: Commit**

```bash
git add core/mail_otp.py tests/test_mail_otp.py
git commit -m "feat(mail-otp): read inbox and pick newest Canva OTP"
```

---

### Task 4: 编排 `get_otp`（兑换 → 轮询 → 轮换回调）

**Files:**
- Modify: `core/mail_otp.py`
- Test: `tests/test_mail_otp.py`

**Interfaces:**
- Consumes: `redeem_graph_token`（Task 2）、`fetch_latest_canva_otp`（Task 3）、`MailOTPError`（Task 2）。
- Produces: `get_otp(client_id, refresh_token, *, since_ts=None, on_rotate=None, poll_interval=5, timeout=120, http_post=None, http_get=None, sleep=time.sleep, now=time.time) -> Tuple[str, str]` —— 返回 `(otp, current_refresh_token)`；token 轮换时调用 `on_rotate(new_refresh)`；超时抛 `MailOTPError`。这是 LeonardoBootstrapper 计划将消费的唯一入口。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_otp.py  （追加）
from core.mail_otp import get_otp


def test_get_otp_rotates_token_and_polls_until_found():
    rotated = []
    fetch_calls = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "NEW_RT"})

    def fake_get(url, headers=None, timeout=None):
        fetch_calls["n"] += 1
        if fetch_calls["n"] < 2:
            return _FakeResp(200, {"value": []})           # 第一次：还没到验证码
        return _FakeResp(200, {"value": [
            {"from": {"emailAddress": {"address": "noreply@canva.com"}},
             "subject": "验证码是100581", "bodyPreview": "",
             "receivedDateTime": "2026-07-30T14:47:00Z"},
        ]})

    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    otp, refresh = get_otp(
        "CID", "OLD_RT",
        on_rotate=rotated.append,
        poll_interval=1, timeout=60,
        http_post=fake_post, http_get=fake_get,
        sleep=lambda _s: None,
        now=lambda: next(ticks),
    )
    assert otp == "100581"
    assert refresh == "NEW_RT"
    assert rotated == ["NEW_RT"]        # 轮换回调被调用
    assert fetch_calls["n"] == 2        # 轮询了两次


def test_get_otp_raises_on_timeout():
    def fake_post(url, data=None, timeout=None):
        return _FakeResp(200, {"access_token": "AT", "refresh_token": "OLD_RT"})

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(200, {"value": []})

    ticks = iter([0.0, 100.0, 200.0])

    with pytest.raises(MailOTPError):
        get_otp(
            "CID", "OLD_RT",
            poll_interval=1, timeout=30,
            http_post=fake_post, http_get=fake_get,
            sleep=lambda _s: None,
            now=lambda: next(ticks),
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mail_otp.py -k get_otp -v`
Expected: FAIL —— `ImportError: cannot import name 'get_otp'`。

- [ ] **Step 3: Write minimal implementation**

```python
# core/mail_otp.py  （在 import 区追加 time；在文件末尾追加函数）
import time


def get_otp(
    client_id: str,
    refresh_token: str,
    *,
    since_ts: Optional[float] = None,
    on_rotate=None,
    poll_interval: float = 5,
    timeout: float = 120,
    http_post=None,
    http_get=None,
    sleep=time.sleep,
    now=time.time,
) -> Tuple[str, str]:
    """端到端取 OTP：兑换 Graph token（轮换则回调 on_rotate）→ 轮询收件箱直到取到验证码。
    返回 (otp, current_refresh_token)。超时抛 MailOTPError。"""
    access, current_refresh = redeem_graph_token(client_id, refresh_token, http_post=http_post)
    if on_rotate is not None and current_refresh != refresh_token:
        on_rotate(current_refresh)

    deadline = now() + timeout
    while True:
        hit = fetch_latest_canva_otp(access, since_ts=since_ts, http_get=http_get)
        if hit:
            return hit[0], current_refresh
        if now() >= deadline:
            raise MailOTPError("no Canva OTP received within timeout")
        sleep(poll_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mail_otp.py -v`
Expected: PASS（本文件全部用例全绿）。

- [ ] **Step 5: Commit**

```bash
git add core/mail_otp.py tests/test_mail_otp.py
git commit -m "feat(mail-otp): orchestrate get_otp with rotation callback and polling"
```

---

## 集成验收（可选，需真实账号 + 网络，非 TDD）

单元测试完成后，可用样本账号做一次**手工**冒烟验证（不纳入 CI）：读账号文件的 `(client_id, refresh_token)`，调 `get_otp(..., on_rotate=<写回账号文件>)`，先在 Canva 触发一次邮箱验证码，确认能取到 6 位码且轮换 token 已写回。⚠️ 每次成功兑换都会轮换 token，务必让 `on_rotate` 真正持久化，否则烧号。

---

## Self-Review

- **Spec coverage**：本计划对应 spec §7.1 的 `MailOTPReader` 一行。覆盖了 §1.3（Graph 兑换 scope）、§1.4（轮换写回）、§5 step 5（多语言 OTP，含修正后的中文正则）。spec 其余模块（Bootstrapper/leonardo_client/refresh_mgr 集成）属独立计划，不在本计划范围——见文首 Scope Check。
- **Placeholder scan**：无 TODO/TBD；每个代码步骤均为可运行的真实代码。
- **Type consistency**：`extract_canva_otp`（T1）→ `fetch_latest_canva_otp`（T3）消费一致；`redeem_graph_token` 返回 `(access, refresh)`（T2）被 `get_otp`（T4）按同名解构；`MailOTPError`（T2）在 T3/T4 复用；`_FakeResp` 测试替身在 T2 定义后 T3/T4 复用（执行时按 Task 顺序，替身已在同一测试文件中）。
