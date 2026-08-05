# Leonardo 登录账号后台批量导入 + 状态告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Leonardo 登录账号从环境变量改为后台页面批量导入(`邮箱:密码` 每行一条),持久化到线程安全的原子存储,账号列表按邮箱显示,并加余额低/连续重登失败的后台+日志告警。

**Architecture:** 镜像现有 Leonardo Cookie 导入的三层结构——adobe2api 侧新增线程安全的 `LeonardoLoginStore`(原子写)+ 管理端点 + refresh-key 端点;后台 `admin.html`/`admin.js` 新增导入弹窗与账号列表;leonardo-refresher 侧 provider 拉登录账号并回报状态/余额,source/service 增加 credential_rev 触发的立即重验与 context 回收。

**Tech Stack:** Python 3 / FastAPI / Pydantic v2 / 原生 threading + json + os.replace;前端原生 JS;pytest。

## Global Constraints

- 存储必须原子写:临时文件 → `flush` + `os.fsync` → `os.replace`;新文件权限 `0o600`;损坏 JSON 时**抛错、拒绝以空覆盖**;所有读写在**单一 `threading.RLock`** 内;`report()` 状态+余额**一次事务只 save 一次**。参考 `core/token_mgr.py:108` 的 `save()`。
- 密码明文存储,但**不出现在**管理端响应、`status_view`、日志、导出;`[leo-login]` 只打邮箱前缀。refresh-key `GET /logins` 因 refresher 登录**必须**返回密码(内部信任通道)。
- `credential_rev` 单调递增(改密码 +1);`report(id, credential_rev, ...)` **仅当 rev==当前 rev 才改账号状态**,否则 `{updated:false, reason:"stale_revision"}`;**余额与 rev 无关,恒接收**。
- env `LEONARDO_LOGIN_ACCOUNTS` **仅端点拉取异常时兜底**;端点成功(含**空列表**)以存储为准、**不回退**。
- 阈值后端常量:`FAIL_ALERT_THRESHOLD=3`、`BALANCE_ALERT_THRESHOLD=1000`;由 `status_view` 下发,前端只读 `data.thresholds`(缺失才用兼容默认)。
- Pydantic 校验:report 中 `login_required` **必带** `last_error_kind`、`ok` **必空**;`balance` 有限非负 float;import `{text}` ≤ 200KB。
- 状态机固定:`ok`→`fail_count=0`+`last_error_kind=None`;`login_required`→`fail_count+=1`+记新错;`pending` 仅由导入/改密码产生。
- TDD、每步 2–5 分钟、frequent commits;沿用现有 fake 测试模式(`tests/test_admin_leonardo_cookie.py`、`tests/test_leonardo_refresher.py`)。
- 参考的既有符号:`core/token_mgr.py` 的 `CONFIG_DIR`;`api/routes/leonardo_tokens.py` 的 `_require_refresh_key(request)`;`api/routes/admin.py` 的 `require_admin_auth(request)` 与 delegate 模式;`leonardo_refresher/adapters.py` 的 `LOGIN_MARKER`、`Adobe2ApiCookieProvider`、`PlaywrightSessionSource`;`leonardo_refresher/service.py` 的 `RefresherService._known`/`_retry_after`。

---

### Task 1: `LeonardoLoginStore` — 原子持久化核心(load / _save / list / remove)

**Files:**
- Create: `api/routes/leonardo_login_store.py`
- Test: `tests/test_leonardo_login_store.py`

**Interfaces:**
- Consumes: `core.token_mgr.CONFIG_DIR`(`pathlib.Path`)。
- Produces:
  - `class LeonardoLoginStore(path: Path)`;模块级单例 `login_store = LeonardoLoginStore(CONFIG_DIR / "leonardo_logins.json")`。
  - `FAIL_ALERT_THRESHOLD = 3`、`BALANCE_ALERT_THRESHOLD = 1000`(模块常量)。
  - `.list_for_refresher() -> list[dict]`(每项 `{id,email,password,credential_rev}`)。
  - `.remove(id: str) -> {"removed": int, "count": int}`。
  - 内部:`._load() -> dict`、`._save(data: dict)`、`._lock`(RLock)。落盘结构 `{"logins":[...], "yescaptcha_balance":None, "balance_at":None}`。

- [ ] **Step 1: Write failing tests**

```python
# tests/test_leonardo_login_store.py
import json, os, threading
import pytest
from api.routes.leonardo_login_store import LeonardoLoginStore

@pytest.fixture
def store(tmp_path):
    return LeonardoLoginStore(tmp_path / "leonardo_logins.json")

def test_empty_when_missing(store):
    assert store.list_for_refresher() == []

def test_save_is_atomic_and_0600(store, tmp_path):
    store._save({"logins": [{"id": "a", "email": "x@y.z", "password": "p", "credential_rev": 1}],
                 "yescaptcha_balance": None, "balance_at": None})
    f = tmp_path / "leonardo_logins.json"
    assert f.exists()
    assert (f.stat().st_mode & 0o777) == 0o600
    # 没有残留临时文件
    assert not list(tmp_path.glob(".leonardo_logins.json.*.tmp"))
    assert json.loads(f.read_text())["logins"][0]["email"] == "x@y.z"

def test_corrupt_file_refuses_overwrite(store, tmp_path):
    (tmp_path / "leonardo_logins.json").write_text("{ not json")
    with pytest.raises(RuntimeError):
        store.list_for_refresher()   # 读到损坏 → 抛错，绝不当空

def test_remove_by_id(store):
    store._save({"logins": [{"id": "a", "email": "e", "password": "p", "credential_rev": 1}],
                 "yescaptcha_balance": None, "balance_at": None})
    assert store.remove("a") == {"removed": 1, "count": 0}
    assert store.remove("missing") == {"removed": 0, "count": 0}

def test_concurrent_saves_do_not_corrupt(store):
    def worker(n):
        for _ in range(20):
            store._with_lock_append({"id": f"id{n}-{_}", "email": "e", "password": "p", "credential_rev": 1})
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(store.list_for_refresher()) == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_leonardo_login_store.py -v`
Expected: FAIL (module `leonardo_login_store` not found).

- [ ] **Step 3: Implement the store core**

```python
# api/routes/leonardo_login_store.py
import json, os, threading, uuid
from pathlib import Path

FAIL_ALERT_THRESHOLD = 3
BALANCE_ALERT_THRESHOLD = 1000
_EMPTY = {"logins": [], "yescaptcha_balance": None, "balance_at": None}


class LeonardoLoginStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._corrupt = False

    def _load(self) -> dict:
        if not self._path.exists():
            return json.loads(json.dumps(_EMPTY))
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._corrupt = True
            raise RuntimeError(
                "refusing to use corrupt leonardo_logins.json; repair or move it first"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("logins"), list):
            self._corrupt = True
            raise RuntimeError("leonardo_logins.json has unexpected shape")
        data.setdefault("yescaptcha_balance", None)
        data.setdefault("balance_at", None)
        return data

    def _save(self, data: dict) -> None:
        if self._corrupt:
            raise RuntimeError("refusing to overwrite corrupt leonardo_logins.json")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as h:
                os.chmod(tmp, 0o600)
                h.write(payload)
                h.flush()
                os.fsync(h.fileno())
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def list_for_refresher(self) -> list:
        with self._lock:
            data = self._load()
            return [
                {"id": x["id"], "email": x["email"], "password": x["password"],
                 "credential_rev": int(x.get("credential_rev") or 1)}
                for x in data["logins"]
            ]

    def remove(self, id: str) -> dict:
        with self._lock:
            data = self._load()
            before = len(data["logins"])
            data["logins"] = [x for x in data["logins"] if x.get("id") != id]
            self._save(data)
            return {"removed": before - len(data["logins"]), "count": len(data["logins"])}

    def _with_lock_append(self, entry: dict) -> None:  # 仅测试并发用
        with self._lock:
            data = self._load()
            data["logins"].append(entry)
            self._save(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_leonardo_login_store.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add api/routes/leonardo_login_store.py tests/test_leonardo_login_store.py
git commit -m "feat(leonardo): LeonardoLoginStore 原子持久化核心(RLock+os.replace+0600+损坏拒覆盖)"
```

---

### Task 2: `LeonardoLoginStore.import_lines`(批量解析 / 去重 / credential_rev)

**Files:**
- Modify: `api/routes/leonardo_login_store.py`
- Test: `tests/test_leonardo_login_store.py`

**Interfaces:**
- Produces: `.import_lines(raw: str) -> {"added": int, "updated": int, "skipped": int, "count": int}`。
  - 按行,`email, sep, password = line.partition(":")`;`email = email.strip().lower()`;`password = password` 去掉行尾 `\r\n`、**不 strip**;`sep` 空 / email 空 / password 空 → skipped。
  - email 去重:存在则——密码不同才 `credential_rev += 1` 且 `status="pending"`、`fail_count=0`、`updated_at` 刷新(计 updated);密码相同则跳过(不计 added/updated,计入 skipped? 见测试:密码相同视为 no-op,不计 updated)。
  - 新 email:`id=uuid4().hex[:12]`、`credential_rev=1`、`status="pending"`、`fail_count=0`(计 added)。

- [ ] **Step 1: Write failing tests**

```python
def test_import_parses_first_colon_and_no_strip(store):
    out = store.import_lines("a@b.co:pa:ss word \nC@D.co : pw2\n")
    assert (out["added"], out["skipped"]) == (2, 0)
    rows = {r["email"]: r for r in store.list_for_refresher()}
    assert rows["a@b.co"]["password"] == "pa:ss word "   # 首冒号切分、密码不 strip、保留内部冒号与空格
    assert "c@d.co" in rows                               # email 规范化小写+strip

def test_import_skips_invalid_lines(store):
    out = store.import_lines("noColonHere\n:emptyEmail\nx@y.z:\n\n  \n")
    assert out["added"] == 0 and out["skipped"] == 5

def test_reimport_same_password_is_noop(store):
    store.import_lines("a@b.co:pw")
    out = store.import_lines("a@b.co:pw")
    assert out["added"] == 0 and out["updated"] == 0
    assert store.list_for_refresher()[0]["credential_rev"] == 1

def test_reimport_new_password_bumps_rev_and_resets(store):
    store.import_lines("a@b.co:pw1")
    store.report(store.list_for_refresher()[0]["id"],
                 store.list_for_refresher()[0]["credential_rev"], "ok")  # 先变 ok(依赖 Task 3)
    out = store.import_lines("a@b.co:pw2")
    assert out["updated"] == 1
    row = store.list_for_refresher()[0]
    assert row["password"] == "pw2" and row["credential_rev"] == 2
```

> 注:`test_reimport_new_password_bumps_rev_and_resets` 依赖 Task 3 的 `report`;若按顺序实现,可在 Task 3 完成后再解注该断言,或此处仅断言 rev/password、status 断言留到 Task 3。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_leonardo_login_store.py -k import -v`
Expected: FAIL (`import_lines` not defined)。

- [ ] **Step 3: Implement `import_lines`**

```python
    import time  # 顶部已 import 的话省略

    def import_lines(self, raw: str) -> dict:
        added = updated = skipped = 0
        with self._lock:
            data = self._load()
            by_email = {x["email"]: x for x in data["logins"]}
            for line in str(raw or "").splitlines():
                email, sep, password = line.partition(":")
                email = email.strip().lower()
                password = password.rstrip("\r\n")
                if not sep or not email or not password:
                    skipped += 1
                    continue
                now = int(time.time())
                cur = by_email.get(email)
                if cur is None:
                    data["logins"].append({
                        "id": uuid.uuid4().hex[:12], "email": email, "password": password,
                        "credential_rev": 1, "status": "pending", "fail_count": 0,
                        "last_error_kind": None, "updated_at": now, "last_attempt_at": None,
                    })
                    added += 1
                elif cur["password"] != password:
                    cur["password"] = password
                    cur["credential_rev"] = int(cur.get("credential_rev") or 1) + 1
                    cur["status"] = "pending"
                    cur["fail_count"] = 0
                    cur["last_error_kind"] = None
                    cur["updated_at"] = now
                    updated += 1
                else:
                    skipped += 1
            self._save(data)
            return {"added": added, "updated": updated, "skipped": skipped,
                    "count": len(data["logins"])}
```

(在文件顶部加 `import time`。)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_leonardo_login_store.py -k import -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add api/routes/leonardo_login_store.py tests/test_leonardo_login_store.py
git commit -m "feat(leonardo): 登录账号批量导入解析(首冒号/不strip/邮箱去重/改密码bump rev)"
```

---

### Task 3: `report` / `status_view` / 阈值告警日志

**Files:**
- Modify: `api/routes/leonardo_login_store.py`
- Test: `tests/test_leonardo_login_store.py`

**Interfaces:**
- Produces:
  - `.report(id, credential_rev, status, last_error_kind=None, balance=None) -> {"updated": bool, "reason": str|None}`。
    - 余额:`balance` 非空则更新 `yescaptcha_balance`/`balance_at`(**与 rev 无关**)。
    - 账号:找不到 id → `{updated:false, reason:"unknown_id"}`;`credential_rev != 当前` → `{updated:false, reason:"stale_revision"}`。
    - rev 匹配:`ok`→`status=ok, fail_count=0, last_error_kind=None`;`login_required`→`fail_count+=1, last_error_kind=<入参>`;`last_attempt_at=now`。
    - 阈值日志(见 Global Constraints;用 `logging.getLogger("leonardo_login").warning`,`flush` 由 logging 保证):`fail_count` 跨到 `FAIL_ALERT_THRESHOLD`、旧`fail_count>=阈值`后恢复 `ok`、余额跌破 / 恢复。
  - `.status_view() -> {"logins":[{id,email,status,fail_count,last_error_kind,updated_at,last_attempt_at}], "count":int, "yescaptcha_balance":float|None, "balance_at":int|None, "thresholds":{"fail_count":FAIL_ALERT_THRESHOLD, "yescaptcha_balance":BALANCE_ALERT_THRESHOLD}}`(**无 password**)。

- [ ] **Step 1: Write failing tests**

```python
def _id_rev(store):
    r = store.list_for_refresher()[0]
    return r["id"], r["credential_rev"]

def test_report_ok_clears_error_and_fail(store):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    store.report(i, rev, "login_required", last_error_kind="password")
    store.report(i, rev, "ok")
    v = store.status_view()["logins"][0]
    assert v["status"] == "ok" and v["fail_count"] == 0 and v["last_error_kind"] is None

def test_report_login_required_increments(store):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    store.report(i, rev, "login_required", last_error_kind="captcha")
    store.report(i, rev, "login_required", last_error_kind="captcha")
    v = store.status_view()["logins"][0]
    assert v["fail_count"] == 2 and v["last_error_kind"] == "captcha"

def test_stale_revision_rejected_but_balance_accepted(store):
    store.import_lines("a@b.co:pw1"); i, _ = _id_rev(store)
    store.import_lines("a@b.co:pw2")  # rev -> 2, pending
    out = store.report(i, 1, "ok", balance=42.0)  # 旧 rev
    assert out == {"updated": False, "reason": "stale_revision"}
    v = store.status_view()
    assert v["logins"][0]["status"] == "pending"        # 账号状态没被旧回报改
    assert v["yescaptcha_balance"] == 42.0              # 但余额收下了

def test_status_view_has_thresholds_and_no_password(store):
    store.import_lines("a@b.co:pw")
    v = store.status_view()
    assert v["thresholds"] == {"fail_count": 3, "yescaptcha_balance": 1000}
    assert "password" not in v["logins"][0]

def test_low_balance_logged_only_on_crossing(store, caplog):
    store.import_lines("a@b.co:pw"); i, rev = _id_rev(store)
    import logging
    with caplog.at_level(logging.WARNING, logger="leonardo_login"):
        store.report(i, rev, "ok", balance=2000.0)   # 高，不告警
        store.report(i, rev, "ok", balance=500.0)    # 跌破 -> 告警一次
        store.report(i, rev, "ok", balance=400.0)    # 仍低 -> 不重复
    assert sum("余额" in r.message for r in caplog.records) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_leonardo_login_store.py -k "report or status or balance" -v`
Expected: FAIL。

- [ ] **Step 3: Implement `report` + `status_view`**

```python
import logging
_logger = logging.getLogger("leonardo_login")

    def status_view(self) -> dict:
        with self._lock:
            data = self._load()
            return {
                "logins": [{k: x.get(k) for k in
                            ("id", "email", "status", "fail_count",
                             "last_error_kind", "updated_at", "last_attempt_at")}
                           for x in data["logins"]],
                "count": len(data["logins"]),
                "yescaptcha_balance": data.get("yescaptcha_balance"),
                "balance_at": data.get("balance_at"),
                "thresholds": {"fail_count": FAIL_ALERT_THRESHOLD,
                               "yescaptcha_balance": BALANCE_ALERT_THRESHOLD},
            }

    def report(self, id, credential_rev, status, last_error_kind=None, balance=None) -> dict:
        with self._lock:
            data = self._load()
            # 余额与 rev 无关，先处理
            if balance is not None:
                old = data.get("yescaptcha_balance")
                if (old is None or old >= BALANCE_ALERT_THRESHOLD) and balance < BALANCE_ALERT_THRESHOLD:
                    _logger.warning("YesCaptcha 余额跌破阈值：%s < %s", balance, BALANCE_ALERT_THRESHOLD)
                elif old is not None and old < BALANCE_ALERT_THRESHOLD and balance >= BALANCE_ALERT_THRESHOLD:
                    _logger.warning("YesCaptcha 余额已恢复：%s", balance)
                data["yescaptcha_balance"] = float(balance)
                data["balance_at"] = int(time.time())
            row = next((x for x in data["logins"] if x.get("id") == id), None)
            if row is None:
                self._save(data)
                return {"updated": False, "reason": "unknown_id"}
            if int(row.get("credential_rev") or 1) != int(credential_rev):
                self._save(data)
                return {"updated": False, "reason": "stale_revision"}
            row["last_attempt_at"] = int(time.time())
            if status == "ok":
                if row.get("fail_count", 0) >= FAIL_ALERT_THRESHOLD:
                    _logger.warning("登录账号 %s 已恢复正常", row["email"])
                row["status"] = "ok"; row["fail_count"] = 0; row["last_error_kind"] = None
            else:  # login_required
                row["fail_count"] = int(row.get("fail_count") or 0) + 1
                row["status"] = "login_required"; row["last_error_kind"] = last_error_kind
                if row["fail_count"] == FAIL_ALERT_THRESHOLD:
                    _logger.warning("登录账号 %s 连续登录失败 %s 次(%s)",
                                    row["email"], row["fail_count"], last_error_kind)
            self._save(data)
            return {"updated": True, "reason": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_leonardo_login_store.py -v`
Expected: PASS(含 Task 2 里依赖 report 的用例)。

- [ ] **Step 5: Commit**

```bash
git add api/routes/leonardo_login_store.py tests/test_leonardo_login_store.py
git commit -m "feat(leonardo): report(rev 校验+状态机+余额) 与 status_view(下发阈值) + 阈值跨越告警日志"
```

---

### Task 4: refresh-key 端点(`GET /logins`、`POST /login/report`)+ Pydantic 模型

**Files:**
- Modify: `api/schemas.py`
- Modify: `api/routes/leonardo_tokens.py`（`build_leonardo_token_router` 内加两个路由）
- Test: `tests/test_leonardo_login_endpoints.py`

**Interfaces:**
- Consumes: `login_store`(Task 1)、`_require_refresh_key(request)`(现有)。
- Produces:
  - `GET /api/v1/tokens/leonardo/logins` → `{"logins": login_store.list_for_refresher()}`。
  - `POST /api/v1/tokens/leonardo/login/report`,body 模型 `LeonardoLoginReportRequest`,→ `login_store.report(...)`。
  - `class LeonardoLoginReportRequest`(schemas):`id:str; credential_rev:int(ge=0); status:Literal["ok","login_required"]; last_error_kind:Optional[Literal["password","captcha","proxy","upstream"]]=None; balance:Optional[float]=None`;model_validator:`login_required`必带 `last_error_kind`、`ok` 必空;`balance` 若给必 `>=0` 且有限。

- [ ] **Step 1: Write failing tests**

```python
# tests/test_leonardo_login_endpoints.py
import pytest
from pydantic import ValidationError
from api.schemas import LeonardoLoginReportRequest

def test_report_model_requires_error_on_failure():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="login_required")

def test_report_model_forbids_error_on_ok():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="ok", last_error_kind="password")

def test_report_model_rejects_negative_balance():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="ok", balance=-1)

def test_report_model_ok():
    m = LeonardoLoginReportRequest(id="a", credential_rev=2, status="login_required",
                                   last_error_kind="captcha", balance=10.5)
    assert m.balance == 10.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_leonardo_login_endpoints.py -v`
Expected: FAIL（`LeonardoLoginReportRequest` 未定义）。

- [ ] **Step 3: Add the Pydantic model + endpoints**

```python
# api/schemas.py
from typing import Literal, Optional
import math
from pydantic import BaseModel, model_validator, field_validator

class LeonardoLoginReportRequest(BaseModel):
    id: str
    credential_rev: int
    status: Literal["ok", "login_required"]
    last_error_kind: Optional[Literal["password", "captcha", "proxy", "upstream"]] = None
    balance: Optional[float] = None

    @field_validator("credential_rev")
    @classmethod
    def _rev_nonneg(cls, v):
        if v < 0:
            raise ValueError("credential_rev must be >= 0")
        return v

    @field_validator("balance")
    @classmethod
    def _bal(cls, v):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise ValueError("balance must be finite and >= 0")
        return v

    @model_validator(mode="after")
    def _err_matches_status(self):
        if self.status == "login_required" and not self.last_error_kind:
            raise ValueError("login_required requires last_error_kind")
        if self.status == "ok" and self.last_error_kind:
            raise ValueError("ok must not carry last_error_kind")
        return self
```

```python
# api/routes/leonardo_tokens.py — build_leonardo_token_router 内追加
    from api.routes.leonardo_login_store import login_store
    from api.schemas import LeonardoLoginReportRequest

    @router.get("/api/v1/tokens/leonardo/logins")
    def get_leonardo_logins(request: Request):
        _require_refresh_key(request)
        return {"logins": login_store.list_for_refresher()}

    @router.post("/api/v1/tokens/leonardo/login/report")
    def report_leonardo_login(req: LeonardoLoginReportRequest, request: Request):
        _require_refresh_key(request)
        return login_store.report(req.id, req.credential_rev, req.status,
                                  last_error_kind=req.last_error_kind, balance=req.balance)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_leonardo_login_endpoints.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add api/schemas.py api/routes/leonardo_tokens.py tests/test_leonardo_login_endpoints.py
git commit -m "feat(leonardo): refresh-key 登录端点(/logins,/login/report)+ 条件校验模型"
```

---

### Task 5: admin 端点(导入 / 状态 / 删除)

**Files:**
- Modify: `api/schemas.py`（`LeonardoLoginImportRequest`）
- Modify: `api/routes/admin.py`（`require_admin_auth` + delegate,仿 cookie 三件套 `admin.py:842`）
- Test: `tests/test_admin_leonardo_login.py`

**Interfaces:**
- Consumes: `login_store`、`require_admin_auth`。
- Produces:
  - `class LeonardoLoginImportRequest(BaseModel): text: constr(max_length=200_000)`。
  - `POST /api/v1/leonardo/login` → `{"status":"ok", **login_store.import_lines(req.text)}`。
  - `GET /api/v1/leonardo/login/status` → `login_store.status_view()`。
  - `DELETE /api/v1/leonardo/login/{id}` → 404 if `removed==0`,否则 `{"status":"ok", **result}`。

- [ ] **Step 1: Write failing tests**（用现有 admin 测试的 client/鉴权 fixture 模式,参考 `tests/test_admin_leonardo_cookie.py`)

```python
# tests/test_admin_leonardo_login.py（示意，沿用现有 admin 测试的 authed client fixture）
def test_import_status_delete_roundtrip(admin_client, cookie_dir):
    r = admin_client.post("/api/v1/leonardo/login", json={"text": "a@b.co:pw\nbad line\n"})
    assert r.status_code == 200 and r.json()["added"] == 1 and r.json()["skipped"] == 1
    st = admin_client.get("/api/v1/leonardo/login/status").json()
    assert st["count"] == 1 and st["logins"][0]["email"] == "a@b.co"
    assert "password" not in st["logins"][0] and st["thresholds"]["fail_count"] == 3
    lid = st["logins"][0]["id"]
    assert admin_client.delete(f"/api/v1/leonardo/login/{lid}").status_code == 200
    assert admin_client.delete(f"/api/v1/leonardo/login/{lid}").status_code == 404

def test_import_rejects_oversize(admin_client):
    r = admin_client.post("/api/v1/leonardo/login", json={"text": "x" * 200_001})
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_leonardo_login.py -v`
Expected: FAIL。

- [ ] **Step 3: Implement schema + endpoints**

```python
# api/schemas.py
from pydantic import constr
class LeonardoLoginImportRequest(BaseModel):
    text: constr(max_length=200_000)
```

```python
# api/routes/admin.py — 紧挨 leonardo_cookie_* 三个端点后追加
    @router.post("/api/v1/leonardo/login")
    def leonardo_login_import(req: LeonardoLoginImportRequest, request: Request):
        require_admin_auth(request)
        from api.routes.leonardo_login_store import login_store
        return {"status": "ok", **login_store.import_lines(req.text)}

    @router.get("/api/v1/leonardo/login/status")
    def leonardo_login_status(request: Request):
        require_admin_auth(request)
        from api.routes.leonardo_login_store import login_store
        return login_store.status_view()

    @router.delete("/api/v1/leonardo/login/{login_id}")
    def leonardo_login_remove(login_id: str, request: Request):
        require_admin_auth(request)
        from api.routes.leonardo_login_store import login_store
        result = login_store.remove(login_id)
        if not result.get("removed"):
            raise HTTPException(status_code=404, detail="未找到该登录账号")
        return {"status": "ok", **result}
```

（记得在 admin.py 顶部 import `LeonardoLoginImportRequest`;`HTTPException` 已在用。)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_admin_leonardo_login.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add api/schemas.py api/routes/admin.py tests/test_admin_leonardo_login.py
git commit -m "feat(leonardo): 后台登录账号导入/状态/删除端点(admin 鉴权)"
```

---

### Task 6: 前端(导入弹窗 + 账号列表 + 余额告警)

**Files:**
- Modify: `static/admin.html`（新增「导入 Leonardo 账号」按钮 + 弹窗,紧邻现有 `leoCookieModal`)
- Modify: `static/admin.js`（导入提交 + `refreshLeoLoginStatus` + `removeLeoLogin`,参考 `admin.js:582-700` cookie 版)

**Interfaces:**
- Consumes: `POST /api/v1/leonardo/login`、`GET /api/v1/leonardo/login/status`、`DELETE /api/v1/leonardo/login/{id}`。
- Produces:（无被测导出;这是 UI。手动验证 + 端点已在 Task 5 测过。)

- [ ] **Step 1: 加 HTML 弹窗**(仿 `leoCookieModal`)

```html
<!-- static/admin.html：在 leoCookieModal 之后 -->
<button id="openLeoLoginBtn" type="button">导入 Leonardo 账号</button>
<div id="leoLoginModal" class="modal" style="display:none">
  <div class="modal-body">
    <h3>导入 Leonardo 登录账号</h3>
    <p style="font-size:12px;color:#888">每行一条：<code>邮箱:密码</code>（按首个冒号分隔，密码可含冒号/空格）</p>
    <div id="leoLoginBalance" style="font-size:12px;margin:4px 0"></div>
    <textarea id="leoLoginInput" rows="8" style="width:100%" placeholder="a@b.co:password&#10;c@d.co:password"></textarea>
    <div id="leoLoginMsg" style="font-size:12px;margin:4px 0"></div>
    <div id="leoLoginStatus" style="font-size:12px"></div>
    <div style="margin-top:8px">
      <button id="leoLoginSubmitBtn" type="button">导入</button>
      <button id="leoLoginCloseBtn" type="button">关闭</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: 加 JS**（导入 + 列表渲染 + 余额,阈值只读后端)

```javascript
// static/admin.js
const openLeoLoginBtn = document.getElementById("openLeoLoginBtn");
const leoLoginModal = document.getElementById("leoLoginModal");
const leoLoginInput = document.getElementById("leoLoginInput");
const leoLoginSubmitBtn = document.getElementById("leoLoginSubmitBtn");
const leoLoginCloseBtn = document.getElementById("leoLoginCloseBtn");
const leoLoginMsg = document.getElementById("leoLoginMsg");
const leoLoginStatus = document.getElementById("leoLoginStatus");
const leoLoginBalance = document.getElementById("leoLoginBalance");

async function refreshLeoLoginStatus() {
  if (!leoLoginStatus) return;
  try {
    const res = await fetch("/api/v1/leonardo/login/status");
    if (!res.ok) return;
    const data = await res.json();
    const th = data.thresholds || { fail_count: 3, yescaptcha_balance: 1000 };
    const bal = data.yescaptcha_balance;
    if (leoLoginBalance) {
      const low = bal != null && bal < th.yescaptcha_balance;
      leoLoginBalance.textContent = `YesCaptcha 余额：${bal == null ? "未知" : bal}${low ? "（偏低，请充值）" : ""}`;
      leoLoginBalance.style.color = low ? "#c0392b" : "#888";
    }
    const list = Array.isArray(data.logins) ? data.logins : [];
    leoLoginStatus.innerHTML = "";
    if (!list.length) { leoLoginStatus.textContent = "当前未导入登录账号。"; return; }
    list.forEach((a) => {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:8px;margin:2px 0";
      const failing = a.status === "login_required" || (a.fail_count || 0) >= th.fail_count;
      const span = document.createElement("span");
      span.textContent = `${a.email} · ${a.status}${a.fail_count ? `(失败${a.fail_count})` : ""}${a.last_error_kind ? " " + a.last_error_kind : ""}`;
      span.style.color = failing ? "#c0392b" : (a.status === "ok" ? "#27ae60" : "#888");
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "danger"; btn.textContent = "删除";
      btn.style.padding = "1px 8px";
      btn.addEventListener("click", () => removeLeoLogin(a.id, btn));
      row.appendChild(span); row.appendChild(btn); leoLoginStatus.appendChild(row);
    });
  } catch (err) { /* 忽略 */ }
}

async function removeLeoLogin(id, btn) {
  if (!id || !window.confirm("确定删除这个登录账号？")) return;
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/v1/leonardo/login/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(d.detail || "删除失败"); if (btn) btn.disabled = false; return; }
    refreshLeoLoginStatus();
  } catch (err) { window.alert("删除失败"); if (btn) btn.disabled = false; }
}

if (openLeoLoginBtn) openLeoLoginBtn.addEventListener("click", () => { leoLoginModal.style.display = "flex"; if (leoLoginMsg) leoLoginMsg.textContent = ""; refreshLeoLoginStatus(); });
if (leoLoginCloseBtn) leoLoginCloseBtn.addEventListener("click", () => { leoLoginModal.style.display = "none"; });
if (leoLoginSubmitBtn) leoLoginSubmitBtn.addEventListener("click", async () => {
  const raw = (leoLoginInput?.value || "").trim();
  if (!raw) { if (leoLoginMsg) leoLoginMsg.textContent = "请粘贴账号"; return; }
  leoLoginSubmitBtn.disabled = true;
  try {
    const res = await fetch("/api/v1/leonardo/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: raw }) });
    const d = await res.json().catch(() => ({}));
    if (leoLoginMsg) leoLoginMsg.textContent = res.ok ? `新增 ${d.added}，更新 ${d.updated}，跳过 ${d.skipped}` : (d.detail || "导入失败");
    if (res.ok) { leoLoginInput.value = ""; refreshLeoLoginStatus(); }
  } finally { leoLoginSubmitBtn.disabled = false; }
});
```

- [ ] **Step 3: 页面加载 + Token 列表刷新时同步拉状态**

在 admin.js 现有"页面初始化"与"刷新 Token 列表"处调用 `refreshLeoLoginStatus()`(找到现有 `refreshLeoCookieStatus()` 的调用点,并列加一行 `refreshLeoLoginStatus();`)。

- [ ] **Step 4: 手动验证**

Run: 本地起 adobe2api,打开后台 → 导入 `a@b.co:pw` → 列表出现 `a@b.co · pending`,余额行显示;删除生效。
Expected: 三个动作都 OK,密码不在任何响应里(浏览器 Network 检查 `login/status` 无 password 字段)。

- [ ] **Step 5: Commit**

```bash
git add static/admin.html static/admin.js
git commit -m "feat(leonardo): 后台登录账号导入弹窗 + 账号列表(邮箱/状态红标)+ 余额告警(阈值读后端)"
```

---

### Task 7: refresher provider — `fetch_logins` / `report_login`

**Files:**
- Modify: `leonardo_refresher/adapters.py`（`Adobe2ApiCookieProvider`)
- Test: `tests/test_leonardo_refresher.py`

**Interfaces:**
- Consumes: 现有 `Adobe2ApiCookieProvider(base_url, refresh_key, session_factory)`、`RefreshFetchError`。
- Produces:
  - `.fetch_logins() -> list[dict]`（每项 `{id,email,password,credential_rev}`;404/空 → `[]`;网络/HTTP≥400 → `RefreshFetchError`)。
  - `.report_login(id, credential_rev, status, last_error_kind=None, balance=None) -> None`（POST;失败吞掉不抛,不打断刷新)。

- [ ] **Step 1: Write failing tests**（沿用现有 `_GetSession`/`_PushSession` fake）

```python
def test_fetch_logins_returns_list():
    session = _GetSession(response=_GetResponse(payload={"logins": [
        {"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 2}]}))
    p = Adobe2ApiCookieProvider(base_url="http://x", refresh_key="k", session_factory=lambda: session)
    assert p.fetch_logins() == [{"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 2}]
    assert session.calls[0]["url"].endswith("/api/v1/tokens/leonardo/logins")

def test_report_login_posts_and_swallows_errors():
    session = _PushSession(error=requests.ConnectionError("down"))
    p = Adobe2ApiCookieProvider(base_url="http://x", refresh_key="k", session_factory=lambda: session)
    p.report_login("i1", 2, "ok", balance=5.0)  # 不抛
    assert session.calls[0]["json"] == {"id": "i1", "credential_rev": 2, "status": "ok",
                                        "last_error_kind": None, "balance": 5.0}
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_leonardo_refresher.py -k "fetch_logins or report_login" -v` → FAIL。

- [ ] **Step 3: Implement**

```python
# leonardo_refresher/adapters.py — Adobe2ApiCookieProvider 内
    def fetch_logins(self):
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v1/tokens/leonardo/logins",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key}, timeout=15)
        except requests.RequestException as exc:
            raise RefreshFetchError("network") from exc
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise RefreshFetchError(f"logins_http_{resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise RefreshFetchError("invalid_response") from exc
        out = []
        for x in (data or {}).get("logins") or []:
            if x.get("id") and x.get("email") and x.get("password"):
                out.append({"id": str(x["id"]), "email": str(x["email"]),
                            "password": str(x["password"]),
                            "credential_rev": int(x.get("credential_rev") or 1)})
        return out

    def report_login(self, id, credential_rev, status, last_error_kind=None, balance=None):
        try:
            self._session.post(
                f"{self._base_url}/api/v1/tokens/leonardo/login/report",
                headers={"X-Leonardo-Refresh-Key": self._refresh_key},
                json={"id": id, "credential_rev": int(credential_rev), "status": status,
                      "last_error_kind": last_error_kind, "balance": balance},
                timeout=15)
        except Exception:  # noqa: BLE001 - 回报失败不打断刷新
            pass
```

- [ ] **Step 4: Run to verify pass** → `pytest tests/test_leonardo_refresher.py -k "fetch_logins or report_login" -v` PASS。

- [ ] **Step 5: Commit**

```bash
git add leonardo_refresher/adapters.py tests/test_leonardo_refresher.py
git commit -m "feat(leonardo): refresher provider fetch_logins/report_login"
```

---

### Task 8: refresher source — 登录账号来源(端点+env兜底) + rev fingerprint + `drop_context`

**Files:**
- Modify: `leonardo_refresher/adapters.py`（`PlaywrightSessionSource.list_cookies`、加 `drop_context`、`_solve_turnstile` 后查余额、`_fetch_token_login` 带 rev 回报)
- Test: `tests/test_leonardo_refresher.py`

**Interfaces:**
- Consumes: `fetch_logins`(Task 7)、`report_login`、`_get_balance`(新,见下)、现有 `_fetch_token_login`。
- Produces:
  - `list_cookies()`:cookie 条目 + 登录条目,登录条目 `fingerprint = f"{LOGIN_MARKER}:{credential_rev}"`,cookie 位 `email\npassword`;登录来源:`fetch_logins()` 成功(含空)以其为准,抛 `RefreshFetchError` 时回退 env。
  - `drop_context(cid)`:关闭并移除 `self._accounts[cid]`。
  - `_get_balance() -> float|None`:调 YesCaptcha getBalance,失败 None。

- [ ] **Step 1: Write failing tests**

```python
def test_list_cookies_uses_endpoint_login_source():
    class Prov:
        def fetch_all(self): return []
        def fetch_logins(self): return [{"id": "i1", "email": "a@b.co", "password": "pw", "credential_rev": 3}]
    src = PlaywrightSessionSource(config=_login_config(login_accounts=(("env@x.co","envpw"),)),
                                  cookie_provider=Prov(), playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()))
    entries = src.list_cookies()
    login = [e for e in entries if e[2].startswith(LOGIN_MARKER)][0]
    assert login[0] == "i1" and login[2] == f"{LOGIN_MARKER}:3" and login[1] == "a@b.co\npw"
    # 端点成功(即使只返回一条) → 不并入 env 账号
    assert not any(e[1].startswith("env@x.co") for e in entries)

def test_list_cookies_falls_back_to_env_on_fetch_error():
    from leonardo_refresher.service import RefreshFetchError
    class Prov:
        def fetch_all(self): return []
        def fetch_logins(self): raise RefreshFetchError("network")
    src = PlaywrightSessionSource(config=_login_config(login_accounts=(("env@x.co","envpw"),)),
                                  cookie_provider=Prov(), playwright_factory=lambda: _LoginPlaywright(_LoginBrowser()))
    login = [e for e in src.list_cookies() if e[2].startswith(LOGIN_MARKER)][0]
    assert login[1] == "env@x.co\nenvpw"

def test_drop_context_closes_and_removes():
    src, pw, browser = _login_source()
    src.open()
    src._accounts["cid"] = {"context": browser.new_context(), "fp": LOGIN_MARKER}
    ctx = src._accounts["cid"]["context"]
    src.drop_context("cid")
    assert "cid" not in src._accounts and ctx.closed is True
```

（`_login_config` 扩展以接受 `login_accounts` kwarg;`_LoginCtx` 已有 `closed`。)

- [ ] **Step 2: Run to verify fail** → FAIL。

- [ ] **Step 3: Implement**

```python
# list_cookies —— 替换现有实现
    def list_cookies(self):
        fetch_all = getattr(self._cookie_provider, "fetch_all", None)
        entries = list(fetch_all() if callable(fetch_all) else [])
        logins = None
        fetch_logins = getattr(self._cookie_provider, "fetch_logins", None)
        if callable(fetch_logins):
            try:
                logins = fetch_logins()  # 成功(含空)以此为准
            except RefreshFetchError:
                logins = None             # 拉取异常 → 回退 env
        if logins is not None:
            for it in logins:
                entries.append((it["id"], it["email"] + "\n" + it["password"],
                                f'{LOGIN_MARKER}:{it["credential_rev"]}'))
        else:
            for email, password in getattr(self._config, "login_accounts", ()) or ():
                cid = "login:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
                entries.append((cid, email + "\n" + password, LOGIN_MARKER))
        return entries

    def drop_context(self, cid):
        acct = self._accounts.pop(cid, None)
        if acct:
            try:
                acct["context"].close()
            except Exception:  # noqa: BLE001
                pass

    def _get_balance(self):
        key = str(self._config.yescaptcha_key or "").strip()
        if not key:
            return None
        try:
            r = self._yc_post("https://api.yescaptcha.com/getBalance", {"clientKey": key})
            b = r.get("balance")
            return float(b) if b is not None else None
        except Exception:  # noqa: BLE001
            return None
```

`fetch_token_for` 的登录分流改为解析 rev 并透传:

```python
        if fingerprint.startswith(LOGIN_MARKER):
            email, _, password = str(cookie_str or "").partition("\n")
            rev = int(fingerprint.split(":", 1)[1]) if ":" in fingerprint else 1
            return self._fetch_token_login(cookie_id, email, password, rev)
```

`_fetch_token_login(cookie_id, email, password, credential_rev)`:每次尝试先 `bal = self._get_balance()`;成功 `self._cookie_provider.report_login(cookie_id, credential_rev, "ok", balance=bal)`;失败按异常映射 `kind`(LoginRequired→`password`/内部据 body、RefreshFetchError.kind 含 `proxy`→`proxy` 否则 `upstream`、captcha 解不出→`captcha`)后 `report_login(cookie_id, credential_rev, "login_required", kind, bal)` 再抛。

- [ ] **Step 4: Run to verify pass** → PASS（含现有登录测试,注意其 fingerprint 现在是 `LOGIN_MARKER` 或 `LOGIN_MARKER:rev`;把现有 `test_login_account_*` 里构造的 fingerprint 同步为 `f"{LOGIN_MARKER}:1"`,`_fetch_token_login` 调用加 rev 参数)。

- [ ] **Step 5: Commit**

```bash
git add leonardo_refresher/adapters.py tests/test_leonardo_refresher.py
git commit -m "feat(leonardo): 登录源改走端点(env兜底)+rev fingerprint+drop_context+余额上报"
```

---

### Task 9: refresher service — credential_rev 触发立即重验 + 缺席回收 context

**Files:**
- Modify: `leonardo_refresher/service.py`（`RefresherService`)
- Test: `tests/test_leonardo_refresher.py`

**Interfaces:**
- Consumes: `source.drop_context(cid)`(Task 8)、`self._known`/`self._retry_after`、`list_cookies` 的 `(cid, cookie_str, fingerprint)`。
- Produces:
  - `run_once` 中:仅对**登录**cid(`fingerprint.startswith(LOGIN_MARKER)`)维护 `self._login_fp`;fp 变化(=rev 变)→ `pop _known/_retry_after` + `source.drop_context(cid)`。
  - prune 阶段:对缺席 cid 额外 `source.drop_context(cid)`(cookie 与 login 都回收)。

- [ ] **Step 1: Write failing tests**

```python
def test_credential_rev_change_forces_relogin(monkeypatch):
    # 首轮 rev=1 变 healthy；rev 升到 2 → 清 _known + drop_context → 下轮重登
    dropped = []
    class Src:
        def __init__(self): self.rev = 1
        def list_cookies(self): return [("i1", "a@b.co\npw", f"__login__:{self.rev}")]
        def fetch_token_for(self, cid, cookie, fp):
            from leonardo_refresher.service import _JWT_OK  # 见下 helper 或直接构造
            return _JWT_OK
        def drop_context(self, cid): dropped.append(cid)
    # ... 构造 service，run_once 一次(健康)，改 src.rev=2，再 run_once
    # 断言 dropped == ["i1"] 且 _known 被清、当轮重新 fetch。
```

（用现有 `_jwt(...)` 造 token;`_SessionSource` 风格 fake 加 `drop_context`。断言 `service._known` 在 rev 变化后被清空、`drop_context` 被调用一次。)

```python
def test_absent_login_cid_drops_context():
    dropped = []
    class Src:
        def __init__(self): self.items = [("i1", "a@b.co\npw", "__login__:1")]
        def list_cookies(self): return self.items
        def fetch_token_for(self, cid, cookie, fp): return _jwt({"token_use":"id","sub":"s","exp":13600})
        def drop_context(self, cid): dropped.append(cid)
    # run_once 一次；将 items 置空；再 run_once → 断言 dropped 含 "i1"
```

- [ ] **Step 2: Run to verify fail** → FAIL。

- [ ] **Step 3: Implement**（`__init__` 加 `self._login_fp = {}`;`run_once` 内）

```python
# __init__
        self._login_fp = {}

# run_once —— prune 段追加(在 _known/_retry_after prune 后)
        drop = getattr(self.source, "drop_context", None)
        for cid in list(self._login_fp):
            if cid not in present:
                self._login_fp.pop(cid, None)
        if callable(drop):
            # 缺席的登录/ cookie cid 回收浏览器 context
            for cid in list(self._known.keys() | self._retry_after.keys() | set(self._login_fp)):
                pass  # 见下：真正的缺席回收在遍历里做

# run_once —— 进入逐账号循环前/内，对登录账号做 rev 变化检测：
        for cid, cookie_str, fingerprint in cookies:
            if fingerprint.startswith(LOGIN_MARKER):
                if self._login_fp.get(cid) != fingerprint:
                    # 凭据 rev 变(或首见)→ 清缓存 + 丢旧 context，立即重验
                    self._known.pop(cid, None)
                    self._retry_after.pop(cid, None)
                    if callable(drop):
                        drop(cid)
                    self._login_fp[cid] = fingerprint
            # ...（后续沿用现有 gate：known_exp / retry_after / _refresh_one）
```

缺席 context 回收(prune 段,简洁版):在现有"`for cid in list(self._known): if cid not in present: pop`"旁,加

```python
        if callable(getattr(self.source, "drop_context", None)):
            known_or_seen = set(self._known) | set(self._retry_after) | set(self._login_fp)
            for cid in list(known_or_seen):
                if cid not in present:
                    self.source.drop_context(cid)
```

（`LOGIN_MARKER` 从 `leonardo_refresher.adapters` import;注意避免循环 import——service 目前被 adapters import,故在 service 内用字符串常量 `"__login__"` 或延迟 import。用局部常量 `_LOGIN_MARKER = "__login__"` 定义在 service 顶部,与 adapters 保持一致并加注释。)

- [ ] **Step 4: Run to verify pass** → `pytest tests/test_leonardo_refresher.py -v` 全绿。

- [ ] **Step 5: Run full suite + commit**

```bash
pytest -q
git add leonardo_refresher/service.py tests/test_leonardo_refresher.py
git commit -m "feat(leonardo): 凭据 rev 变化立即重验 + 缺席账号回收 Playwright context"
```

---

## Deployment Runbook（非 TDD 任务,按序执行)

1. **构建 adobe2api v52**：`docker build -f Dockerfile -t <ACR>/adobe2api:v52 -t :latest .` → push；`echo 52 > .docker-version`。
2. **部署 adobe2api**：搬瓦工 `docker compose -f docker-compose.deploy.yml pull adobe2api && up -d --no-deps adobe2api`。**此时 refresher 仍是 v9**(还在读 env 账号,池子不空)。
3. **后台导入并确认**：管理页导入 `arif95750@qw2.biz.id:Qwerty123`(及其它),`login/status` 显示 pending/ok。
4. **构建并部署 refresher v10**：build/push `leonardo-refresher:v10`；`echo 10 > .docker-version-leonardo`；`compose --profile leonardo pull leonardo-refresher && up -d --no-deps leonardo-refresher`。日志应出现 `[leo-login]` 用**存储**账号登录、`login/status` 转 ok。
5. **清 env**：确认端点账号可用后,从 `/opt/adobe2api/.env` 删 `LEONARDO_LOGIN_ACCOUNTS`(留空即不再兜底);无需重建(env 只在端点异常时兜底)。
6. `config/` 已由 Compose 持久化,`leonardo_logins.json` 自动落在其中,无需新 volume。

---

## Self-Review

**Spec coverage**:①存储→Task1-3;②端点→Task4(refresh-key)+Task5(admin);③refresher→Task7(provider)+Task8(source)+Task9(service);④UI+日志告警→Task6(UI)+Task3(日志);⑤Pydantic→Task4/5;数据流/错误处理/env迁移/部署→分散于各 Task + Runbook;测试计划→各 Task 的 Step1 + Task1 并发用例 + Task3 stale/告警 + Task8 空列表不回退。全覆盖。

**Placeholder scan**:无 TBD/TODO;每步含真实代码。Task9 的 fake 测试给了骨架 + 断言意图(构造用现有 `_jwt`/`_SessionSource` 风格),实现者据现有 fake 补全——可接受(现有测试已有等价 fake)。

**Type consistency**:`report(id, credential_rev, status, last_error_kind, balance)` 在 store(Task3)/model(Task4)/provider(Task7)/service→provider(Task8)一致;`fingerprint = f"{LOGIN_MARKER}:{rev}"` 在 Task8 产、Task9 消一致;`status_view().thresholds` 在 Task3 产、Task6 消一致;`drop_context(cid)` Task8 产、Task9 消一致。
