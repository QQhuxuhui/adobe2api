"""本次修复中「删掉整段代码测试仍全绿」的接线点。

复核用变异测试找出了这些空洞：把对应生产代码删掉或改回旧行为，1188 个用例
无一变红。它们恰恰是事故修复的关键路径——没有覆盖等于随时可能被后人改回去。
"""

import time

import pytest

from core.adobe_client import (
    AuthError,
    QuotaExhaustedError,
    raise_for_access_error,
)
from core.token_mgr import TokenManager


class FakeResponse:
    def __init__(self, status_code=403, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "{}"


@pytest.fixture
def make_tm(tmp_path, monkeypatch):
    import core.token_mgr as tm_mod

    monkeypatch.setattr(tm_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tm_mod, "DATA_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(tm_mod, "LEGACY_DATA_FILE", tmp_path / "tokens_legacy.json")
    return TokenManager


def _seed(tm, rows):
    tm.tokens = [
        {
            "id": r["id"],
            "value": r["value"],
            "status": r.get("status", "active"),
            "fails": 0,
            "added_at": 0,
            "error_until": 0,
            "last_used_at": 0,
            "type": "adobe",
            "account_id": r.get("account_id", r["value"]),
            **{k: v for k, v in r.items() if k in ("auto_refresh", "refresh_profile_id")},
        }
        for r in rows
    ]
    tm.save()
    return tm


# --- 轮询阶段的配额分类：不可重试，防重复计费 ---


def test_poll_stage_quota_is_not_retryable():
    """submit 已成功、上游已计费，此时换号重跑等于再扣一次费。"""
    with pytest.raises(QuotaExhaustedError) as excinfo:
        raise_for_access_error(
            FakeResponse(403, {"x-access-error": "quota_exhausted"}),
            "image.poll",
            retryable=False,
        )
    assert excinfo.value.retryable is False


def test_submit_stage_quota_stays_retryable():
    with pytest.raises(QuotaExhaustedError) as excinfo:
        raise_for_access_error(
            FakeResponse(403, {"x-access-error": "quota_exhausted"}), "image.submit"
        )
    assert excinfo.value.retryable is True


def _poll_client(monkeypatch, poll_response):
    """把 AdobeClient 驱动到轮询阶段：submit 成功，poll 返回给定响应。

    复用 tests/test_adobe_deadline.py 那套桩（_build_payload_candidates /
    _extract_result_link），不另拼一份脆弱的 submit 响应。
    """
    from core.adobe_client import AdobeClient

    client = AdobeClient.__new__(AdobeClient)
    client.api_key = "test-key"
    client.impersonate = "chrome124"
    client.proxy = ""
    client.user_agent = "test-agent"
    client.sec_ch_ua = '"Chromium";v="124"'
    client.gpt_image_quality = "low"
    client.generate_timeout = 60

    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kw: [{}])
    monkeypatch.setattr(
        client,
        "_extract_result_link",
        lambda response, data: "https://example.test/jobs/job-id",
    )

    class _Submit:
        status_code = 200
        headers: dict = {}
        text = ""

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(client, "_post_json", lambda *a, **k: _Submit())
    monkeypatch.setattr(client, "_get", lambda *a, **k: poll_response)
    return client


def test_image_poll_classifies_quota_and_marks_non_retryable(monkeypatch):
    """改动前轮询这里根本没有 401/403 分支：403 掉进 AdobeRequestError，
    账号既不出池也不重试，死号继续留在池子里。"""
    client = _poll_client(
        monkeypatch, FakeResponse(403, {"x-access-error": "quota_exhausted"})
    )
    with pytest.raises(QuotaExhaustedError) as excinfo:
        client.generate(token="t", prompt="p")
    assert excinfo.value.retryable is False, "已提交并计费，不能换号重跑"


def test_image_poll_auth_error_without_quota_header(monkeypatch):
    client = _poll_client(monkeypatch, FakeResponse(401, {}))
    with pytest.raises(AuthError):
        client.generate(token="t", prompt="p")


@pytest.mark.parametrize(
    "context",
    ["get_json", "entity.create", "entity.upload_image",
     "entity.register_base_resources", "entity.delete", "video.poll", "image.upload"],
)
def test_all_hookups_classify_quota(context):
    """统一分类器接入的每个点都必须认得配额码，不能只有 submit 两处。"""
    with pytest.raises(QuotaExhaustedError):
        raise_for_access_error(
            FakeResponse(403, {"x-access-error": "quota_exhausted"}), context
        )


# 换号预算的行为测试见 tests/test_token_retry_deadline.py：
# 那里有现成的 retry_env / make_request harness，不另造一套。


# --- 批量余额刷新的接线（唯一的复活触发器） ---


def test_batch_refresh_targets_credit_list_not_active_list(make_tm):
    """改回 list_active_ids 的话 exhausted 账号永远查不到余额、永不复活。"""
    tm = make_tm()
    _seed(
        tm,
        [
            {"id": "a", "value": "va", "account_id": "acct-a"},
            {"id": "b", "value": "vb", "account_id": "acct-b", "status": "exhausted"},
        ],
    )
    assert "b" not in tm.list_active_ids()
    assert "b" in tm.list_credit_refresh_ids()


def test_credit_refresh_prefers_auto_refresh_row(make_tm):
    """同账号只查一行时要挑自动刷新行：手动行的 token 往往早已过期，
    拿它查余额只会得到 401，既查不到余额还会把那行标成 invalid。"""
    tm = make_tm()
    _seed(
        tm,
        [
            {"id": "manual", "value": "v-manual", "account_id": "acct-1"},
            {
                "id": "auto",
                "value": "v-auto",
                "account_id": "acct-1",
                "auto_refresh": True,
                "refresh_profile_id": "p1",
            },
        ],
    )
    assert tm.list_credit_refresh_ids() == ["auto"]


# --- refresh_mgr 的 epoch 捕获与复活写入 ---


class _RecordingTM:
    """只记录调用的替身，用来验证 refresh_mgr 真的走了 epoch-aware 路径。"""

    def __init__(self, token_info):
        self._info = token_info
        self.calls = []
        self.epoch_reads = []
        self._epoch = 7

    def get_by_id(self, tid):
        return dict(self._info)

    def account_key_for_id(self, tid, fallback_value=""):
        return "acct-1"

    def quota_epoch(self, key):
        self.epoch_reads.append(key)
        return self._epoch

    def bump_epoch(self):
        self._epoch += 1

    def set_credits_and_maybe_revive(self, tid, credits, observed_quota_epoch=None):
        self.calls.append(("revive", tid, observed_quota_epoch))
        return dict(credits)

    def set_credits(self, tid, credits):
        self.calls.append(("plain", tid, None))
        return dict(credits)


def test_adobe_credits_go_through_epoch_aware_writer(monkeypatch):
    """删掉 revive_writer 分支的话，账号出池后余额恢复也永远不会被放回池子。"""
    from core import refresh_mgr as rm

    tm = _RecordingTM({"type": "adobe", "value": "tok-1", "id": "t1"})
    monkeypatch.setattr(rm, "token_manager", tm)
    monkeypatch.setattr(
        rm.refresh_manager, "_extract_account_id", lambda v: "acct-1", raising=False
    )
    monkeypatch.setattr(
        rm.refresh_manager,
        "_fetch_credits_balance",
        lambda v, a: {"total": 10, "used": 0, "available": 10,
                      "available_until": None, "updated_at": int(time.time())},
        raising=False,
    )

    rm.refresh_manager.refresh_credits_for_token_id("t1")

    assert tm.calls and tm.calls[0][0] == "revive", "Adobe 余额必须走 epoch-aware 提交"
    assert tm.calls[0][2] == 7, "提交时要带上查询前捕获的版本号"


def test_epoch_is_captured_before_the_network_call(monkeypatch):
    """版本必须在发起查询之前读：查询期间又撞一次配额耗尽时，
    这份余额就成了过期数据，不能拿来复活账号。"""
    from core import refresh_mgr as rm

    tm = _RecordingTM({"type": "adobe", "value": "tok-1", "id": "t1"})
    monkeypatch.setattr(rm, "token_manager", tm)
    monkeypatch.setattr(
        rm.refresh_manager, "_extract_account_id", lambda v: "acct-1", raising=False
    )

    def _fetch(value, account_id):
        tm.bump_epoch()  # 查询在飞期间账号又耗尽了一次
        return {"total": 10, "used": 0, "available": 10,
                "available_until": None, "updated_at": int(time.time())}

    monkeypatch.setattr(
        rm.refresh_manager, "_fetch_credits_balance", _fetch, raising=False
    )

    rm.refresh_manager.refresh_credits_for_token_id("t1")

    submitted_epoch = tm.calls[0][2]
    assert submitted_epoch == 7, "带上的必须是查询前的版本"
    assert submitted_epoch != tm.quota_epoch("acct-1"), (
        "与当前版本失配 → set_credits_and_maybe_revive 会拒绝复活"
    )


def test_leonardo_credits_also_go_through_revive_writer(monkeypatch):
    """Leonardo 也必须走 epoch-aware 提交。

    cookie 刷新（upsert_leonardo_token）不再复活 exhausted 账号后，
    余额驱动复活就是 Leonardo 唯一的出路；这里要是只调普通 set_credits，
    Leonardo 号一旦 exhausted 就永久出不来。
    """
    from core import refresh_mgr as rm

    tm = _RecordingTM({"type": "leonardo", "value": "tok-leo", "id": "t2"})
    monkeypatch.setattr(rm, "token_manager", tm)
    monkeypatch.setattr(
        rm.refresh_manager,
        "_fetch_leonardo_credits",
        lambda info: {"total": 5, "used": 0, "available": 5,
                      "available_until": None, "updated_at": int(time.time())},
        raising=False,
    )

    rm.refresh_manager.refresh_credits_for_token_id("t2")
    assert tm.calls == [("revive", "t2", 7)], "Leonardo 余额也要带配额版本提交"


# --- Leonardo 自动余额刷新线程 ---


def _rm(monkeypatch, tokens):
    """把 refresh_manager 的 token_manager 换成给定的假仓库。"""
    from core import refresh_mgr as rm

    class TM:
        def __init__(self, rows):
            self.rows = rows

        def list_credit_refresh_ids(self):
            return [r["id"] for r in self.rows]

        def get_by_id(self, tid):
            return next((dict(r) for r in self.rows if r["id"] == tid), None)

        def list_active_ids(self):
            return [r["id"] for r in self.rows if r["status"] == "active"]

        def set_credits_error(self, tid, msg):
            pass

    monkeypatch.setattr(rm, "token_manager", TM(tokens))
    return rm


def test_leonardo_refresh_targets_skip_adobe_and_fresh_rows(monkeypatch):
    now = time.time()
    rm = _rm(
        monkeypatch,
        [
            {"id": "adobe", "type": "adobe", "status": "active", "credits_updated_at": 0},
            {"id": "leo-fresh", "type": "leonardo", "status": "active",
             "credits_updated_at": now},
            {"id": "leo-stale", "type": "leonardo", "status": "active",
             "credits_updated_at": now - 7200},
        ],
    )
    targets = rm.refresh_manager._leonardo_credit_targets()
    assert targets == ["leo-stale"], "Adobe 不归这个线程管；活跃号的余额已被每请求刷新带着走"


def test_leonardo_refresh_always_includes_exhausted(monkeypatch):
    """出池的号没有请求去顺带刷它，余额刷新是它唯一的复活触发器——不做新鲜度跳过。"""
    now = time.time()
    rm = _rm(
        monkeypatch,
        [
            {"id": "leo-dead", "type": "leonardo", "status": "exhausted",
             "credits_updated_at": now},
        ],
    )
    assert rm.refresh_manager._leonardo_credit_targets() == ["leo-dead"]


def test_leonardo_refresh_interval_is_clamped(monkeypatch):
    from core import refresh_mgr as rm
    from core.config_mgr import config_manager

    values = {}
    original = config_manager.get
    monkeypatch.setattr(
        config_manager, "get",
        lambda k, d=None: values.get(k, original(k, d)),
    )
    assert rm.RefreshManager._leonardo_credits_interval_seconds() == 600
    values["leonardo_credits_refresh_minutes"] = 1
    assert rm.RefreshManager._leonardo_credits_interval_seconds() == 60
    # 上界不得超过余额缓存 TTL（30 分钟），否则 fast-path 缓存会在两次刷新之间过期
    values["leonardo_credits_refresh_minutes"] = 999
    assert rm.RefreshManager._leonardo_credits_interval_seconds() == 1800
    for bad in ("abc", None, True):
        values["leonardo_credits_refresh_minutes"] = bad
        assert rm.RefreshManager._leonardo_credits_interval_seconds() == 600


def test_leonardo_refresh_round_survives_single_failure(monkeypatch, caplog):
    """一个账号查余额失败不能让整轮/整个线程退出（logger 未定义时这里会 NameError）。"""
    now = time.time()
    rm = _rm(
        monkeypatch,
        [
            {"id": "leo-a", "type": "leonardo", "status": "exhausted",
             "credits_updated_at": now},
            {"id": "leo-b", "type": "leonardo", "status": "exhausted",
             "credits_updated_at": now},
        ],
    )
    seen = []

    def boom(tid, handle_auth=False):
        seen.append(tid)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(rm.refresh_manager, "refresh_credits_for_token_id", boom)
    monkeypatch.setattr(
        rm.refresh_manager, "_leonardo_credits_interval_seconds", lambda: 60
    )

    stop = rm.refresh_manager._stop_event

    def wait_once(_timeout):
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", wait_once)
    stop.clear()
    try:
        with caplog.at_level("WARNING"):
            rm.refresh_manager._run_leonardo_credits()
    finally:
        stop.clear()

    assert seen == ["leo-a", "leo-b"], "一个失败不该中断后面的账号"
    assert any("leonardo credits refresh failed" in r.getMessage() for r in caplog.records)


# --- Leonardo 积分记录不得被 Adobe 的事后回填抹掉 ---


def test_measured_credits_survive_backfill():
    """请求内测出的精确成本不得被事后差分/估算覆盖。

    这是「Leonardo 使用记录没有积分数据」的直接原因：保护条件原本只认
    upstream，而 Leonardo 写的是 measured（上游 apiCreditCost 线上恒为 null），
    于是每条记录都被回填线程抹成 null，后台那一列永远显示 -。
    """
    from core.credits_tracker import CreditsTracker

    payload = {"id": "req-1", "credits_used": 292.0, "credits_source": "measured"}

    # 回填拿到 None（Leonardo 的 used 恒为 0 → delta 恒为 0 → 走估算）
    kept = CreditsTracker._merge_credits(payload, None, None)
    assert kept["credits_used"] == 292.0
    assert kept["credits_source"] == "measured"

    # 也不能被 Adobe 的估算值改写（两边积分根本不是同一种币值）
    kept2 = CreditsTracker._merge_credits(payload, 999.0, "estimated")
    assert kept2["credits_used"] == 292.0


def test_upstream_credits_still_protected():
    from core.credits_tracker import CreditsTracker

    payload = {"credits_used": 250.0, "credits_source": "upstream"}
    assert CreditsTracker._merge_credits(payload, 1.0, "measured")["credits_used"] == 250.0


def test_estimated_credits_can_be_upgraded_by_measurement():
    """估算值不是权威来源，测到真值时应当被替换。"""
    from core.credits_tracker import CreditsTracker

    payload = {"credits_used": 140.0, "credits_source": "estimated"}
    merged = CreditsTracker._merge_credits(payload, 155.0, "measured")
    assert merged["credits_used"] == 155.0
    assert merged["credits_source"] == "measured"


def test_leonardo_requests_never_enter_adobe_backfill_queue(monkeypatch):
    """Leonardo 不该被登记进 Adobe 的积分回填队列。

    它的成本在请求内部就精确测出来了；再让 Adobe 那套事后差分插一脚，
    只会用另一种币值的估算把它覆盖掉，还白白多打一次 GraphQL 余额查询。
    """
    import app as app_mod

    begins = []

    class Tracker:
        def begin(self, token_id, request_id, account_id=None):
            begins.append((token_id, request_id))

        def finish(self, *a, **k):
            pass

    class TM:
        def __init__(self, token_type):
            self.token_type = token_type

        def get_meta_by_value(self, value):
            return {
                "token_id": "tid-1",
                "token_account_id": "acct-1",
                "token_account_name": "",
                "token_account_email": "",
                "token_source": "manual",
                "refresh_profile_id": "",
                "token_type": self.token_type,
            }

    class State:
        log_id = "req-1"

    class Req:
        state = State()

    monkeypatch.setattr(app_mod, "credits_tracker", Tracker())
    monkeypatch.setattr(app_mod, "_upsert_live_request", lambda *a, **k: None)

    monkeypatch.setattr(app_mod, "token_manager", TM("leonardo"))
    meta = app_mod._set_request_token_context(Req(), "tok", 1)
    assert begins == [], "Leonardo 不登记回填任务"
    # 但账号归属等日志字段照常要写，否则日志里认不出是哪个号
    assert meta["token_account_id"] == "acct-1"

    monkeypatch.setattr(app_mod, "token_manager", TM("adobe"))
    app_mod._set_request_token_context(Req(), "tok", 1)
    assert begins == [("tid-1", "req-1")], "Adobe 仍要走原来的回填流程"


# --- post-submit 失败一律不可重试（防重复扣费） ---


@pytest.mark.parametrize(
    "poll_status,expected",
    [
        (403, QuotaExhaustedError),   # 带配额头
        (401, AuthError),
        (429, None),                  # UpstreamTemporaryError
        (500, None),
        (418, None),                  # 落到 AdobeRequestError
    ],
)
def test_all_poll_stage_failures_are_non_retryable(monkeypatch, poll_status, expected):
    """submit 成功 = 上游已受理并扣费，此后任何失败都不能换号重来。

    只把配额标成不可重试是不够的：401/429/5xx 同样会让外层换号重新 submit，
    上游再出一次图、再扣一次费，而用户只拿到一个结果。
    """
    headers = {"x-access-error": "quota_exhausted"} if poll_status == 403 else {}
    client = _poll_client(monkeypatch, FakeResponse(poll_status, headers))
    with pytest.raises(Exception) as excinfo:
        client.generate(token="t", prompt="p")
    if expected is not None:
        assert isinstance(excinfo.value, expected)
    assert getattr(excinfo.value, "retryable", True) is False, (
        f"poll {poll_status} 仍可重试 → 会重复提交并重复扣费"
    )


def test_submit_stage_failures_stay_retryable(monkeypatch):
    """对照组：submit 阶段还没受理，换号重试是对的。"""
    from core.adobe_client import AdobeClient

    client = AdobeClient.__new__(AdobeClient)
    for attr, value in (
        ("api_key", "k"), ("impersonate", "chrome124"), ("proxy", ""),
        ("user_agent", "ua"), ("sec_ch_ua", "x"), ("gpt_image_quality", "low"),
        ("generate_timeout", 60),
    ):
        setattr(client, attr, value)
    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kw: [{}])
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda *a, **k: FakeResponse(403, {"x-access-error": "quota_exhausted"}),
    )
    with pytest.raises(QuotaExhaustedError) as excinfo:
        client.generate(token="t", prompt="p")
    assert excinfo.value.retryable is True


# --- 账号键迁移 / 新行绕过 ---


def _jwt(sub):
    import base64 as _b64
    import json as _json

    body = _b64.urlsafe_b64encode(
        _json.dumps({"sub": sub, "exp": int(time.time()) + 3600}).encode()
    ).decode().rstrip("=")
    return f"x.{body}.sig"


def test_retire_survives_account_key_migration(make_tm):
    """自动刷新会给老行补上 account_id，而 _account_key 是 account_id 优先——
    键变了，租约里握的那个就成了旧的，按它出池会一行都命中不到。"""
    from core.token_mgr import retire_account_for_quota

    tm = make_tm()
    tm.tokens = [{
        "id": "auto", "value": "old-token", "status": "active", "fails": 0,
        "added_at": 0, "error_until": 0, "last_used_at": 0,
        "auto_refresh": True, "refresh_profile_id": "prof-1",
    }]
    tm.save()
    lease_key = tm._account_key(tm.tokens[0])          # 请求开始时握到的键
    tm.upsert_auto_refresh_token(_jwt("real-acct"), "prof-1")  # 在飞期间刷新
    assert tm._account_key(tm.tokens[0]) != lease_key, "前提：键确实变了"

    assert retire_account_for_quota(
        tm, account_key=lease_key, token="old-token", token_id="auto"
    ) is True
    assert tm.tokens[0]["status"] == "exhausted"


def test_new_auto_refresh_row_inherits_exhausted(make_tm):
    """同账号已因配额出池时，profile 第一次推 token 不得凭空造出一行 active。"""
    tm = make_tm()
    tm.tokens = [{
        "id": "manual", "value": "tok-m", "status": "exhausted", "fails": 0,
        "added_at": 0, "error_until": 0, "last_used_at": 0, "account_id": "acct-X",
    }]
    tm.save()
    tm.upsert_auto_refresh_token(_jwt("acct-X"), "prof-new")

    assert all(t["status"] == "exhausted" for t in tm.tokens)
    assert tm.get_available(strategy="least_recently_used") is None


def test_new_row_for_healthy_account_is_active(make_tm):
    """反向：账号没出池时照常建 active 行。"""
    tm = make_tm()
    tm.tokens = []
    tm.save()
    tm.upsert_auto_refresh_token(_jwt("acct-Y"), "prof-y")
    assert tm.tokens[0]["status"] == "active"


# --- 余额刷新失败不得让陈旧快照重新"新鲜" ---


def test_errored_credit_snapshot_is_not_trusted(make_tm):
    """set_credits_error 保留旧余额却更新了 credits_updated_at，
    那份陈旧的零余额会重新显得新鲜，一直挡住可能早已恢复额度的账号。"""
    tm = make_tm()
    now = time.time()
    rows = [{
        "id": "a", "value": "va", "status": "active", "fails": 0, "added_at": 0,
        "error_until": 0, "last_used_at": 0, "type": "adobe", "account_id": "acct-1",
        "credits_available": 0, "credits_updated_at": now,
        "credits_quota_epoch": 0, "credits_error": "",
    }]
    tm.tokens = rows
    tm.save()
    assert tm._account_known_zero_credits(rows) is True, "前提：正常的零余额会被跳过"

    tm.set_credits_error("a", "network unreachable")
    assert tm._account_known_zero_credits(tm.tokens) is False, (
        "上次刷新失败的快照不可采信，必须放行"
    )


# --- 视频重试的账号去重口径 ---


def test_video_identity_uses_account_id_first():
    """和 TokenManager._account_key 同口径，否则同账号两行会被当成两个账号重复试。"""
    import inspect

    from core import video_tasks

    source = inspect.getsource(video_tasks)
    idx = source.index("def token_identity(")
    body = source[idx : idx + 600]
    assert "token_account_id" in body, "去重键必须优先用 account_id"


def test_refresh_manager_stop_ends_background_threads():
    """热重载/重复实例化时必须能停掉后台线程，否则旧实例会继续打上游。

    两个线程都以 _stop_event 为退出条件，Leonardo 那条还用它做 sleep。
    """
    from core.refresh_mgr import RefreshManager, refresh_manager

    assert callable(getattr(RefreshManager, "stop", None)), "必须提供 stop()"

    was_set = refresh_manager._stop_event.is_set()
    started_before = refresh_manager._runner_started
    try:
        refresh_manager._stop_event.clear()
        refresh_manager._runner_started = True
        refresh_manager.stop()
        assert refresh_manager._stop_event.is_set(), "stop() 必须置位退出条件"
        assert refresh_manager._runner_started is False, "允许之后重新 start()"
    finally:
        refresh_manager._runner_started = started_before
        if was_set:
            refresh_manager._stop_event.set()
        else:
            refresh_manager._stop_event.clear()


def test_app_shutdown_stops_refresh_manager():
    """shutdown 钩子要真的调到 stop()——只加方法不接线等于没加。"""
    import inspect

    import app as app_mod

    source = inspect.getsource(app_mod._shutdown_video_services)
    assert "refresh_manager" in source and "stop" in source


def test_poll_451_image_unsafe_stays_retryable(monkeypatch):
    """451 是内容安全拦截：上游**没有产出**，用户什么也拿不到。

    判定「能不能重试」的标准不是「有没有扣费」，而是「用户能不能拿到结果」。
    现网每天数百次 451 靠换号/换种子重试救回来，一刀切成不可重试会直接
    把这些请求判死。
    """
    client = _poll_client(monkeypatch, FakeResponse(451, {}))
    with pytest.raises(Exception) as excinfo:
        client.generate(token="t", prompt="p")
    assert getattr(excinfo.value, "retryable", False) is True, (
        "451 必须保持可重试，否则线上大量本可成功的请求会直接失败"
    )
    assert getattr(excinfo.value, "status_code", None) == 451
