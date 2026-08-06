"""/v1/images/edits 的端到端时限与换号上限。

事故里单个 edits 能跑 4~8 分钟：这条路径此前完全没有 deadline，
输入图下载、上传、submit、轮询各用各的固定超时，换一次号就全部重来一遍，
最后被下游 480s 掐断成 504——而这边还在跑并照常计费。

时限从进入 endpoint 就开始算：最多 6 张远程输入图的下载发生在生成之前，
只约束上游那一段的话它们完全不受限制。
"""

import time

import pytest

from core.adobe_client import UpstreamTemporaryError


# --- 配置读取 ---


@pytest.fixture
def cfg(monkeypatch):
    from core.config_mgr import config_manager

    values = {}
    original = config_manager.get
    monkeypatch.setattr(
        config_manager,
        "get",
        lambda key, default=None: values.get(key, original(key, default)),
    )
    return values


def test_edits_deadline_defaults_to_300s(cfg):
    from api.routes.generation import _edits_deadline

    before = time.monotonic()
    deadline = _edits_deadline()
    assert 299 <= deadline - before <= 301


def test_edits_deadline_reads_config(cfg):
    from api.routes.generation import _edits_deadline

    cfg["images_edits_deadline_seconds"] = 42
    before = time.monotonic()
    assert 41 <= _edits_deadline() - before <= 43


@pytest.mark.parametrize("raw", [0, -1, "0"])
def test_non_positive_deadline_means_unlimited(cfg, raw):
    from api.routes.generation import _edits_deadline

    cfg["images_edits_deadline_seconds"] = raw
    assert _edits_deadline() is None


@pytest.mark.parametrize("raw", ["abc", None, True])
def test_dirty_deadline_config_falls_back_to_default(cfg, raw):
    from api.routes.generation import _edits_deadline

    cfg["images_edits_deadline_seconds"] = raw
    before = time.monotonic()
    deadline = _edits_deadline()
    assert deadline is not None and 299 <= deadline - before <= 301


# --- 剩余时间收敛 ---


def test_remaining_shrinks_and_raises_after_expiry():
    from api.routes.generation import _remaining_before_deadline

    deadline = time.monotonic() + 5
    remaining = _remaining_before_deadline(deadline, "x")
    assert 4 < remaining <= 5

    with pytest.raises(UpstreamTemporaryError) as excinfo:
        _remaining_before_deadline(time.monotonic() - 1, "Images edits request")
    assert excinfo.value.status_code == 503
    assert excinfo.value.error_type == "timeout"
    assert "Images edits request" in str(excinfo.value)


def test_no_deadline_means_no_limit():
    from api.routes.generation import _remaining_before_deadline

    assert _remaining_before_deadline(None, "x") is None


# --- 输入图加载：共享同一个绝对截止时间 ---


def _messages(*urls):
    return [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": u}} for u in urls],
        }
    ]


def test_input_images_share_one_budget(monkeypatch):
    """6 张远程图不能各拿 30 秒：后一张只获得剩余时间。"""
    import app as app_mod

    timeouts = []

    class Resp:
        status_code = 200
        content = b"\x89PNG\r\n\x1a\n"
        headers = {"content-type": "image/png"}

    def fake_get(url, timeout=None):
        timeouts.append(timeout)
        return Resp()

    monkeypatch.setattr(app_mod.requests, "get", fake_get)
    monkeypatch.setattr(
        app_mod, "normalize_input_image", lambda b, m: (b, "image/png", 8, 8)
    )

    deadline = time.monotonic() + 10
    app_mod._load_input_images(
        _messages("http://x/1.png", "http://x/2.png"), deadline=deadline
    )

    assert len(timeouts) == 2
    assert all(t <= 10 for t in timeouts), "单次超时不得超过总预算"
    assert timeouts[1] <= timeouts[0], "后一张只拿剩余时间，不是新的 30 秒"


def test_input_image_loading_stops_when_expired(monkeypatch):
    import app as app_mod

    called = []
    monkeypatch.setattr(
        app_mod.requests, "get", lambda *a, **k: called.append(1)
    )

    with pytest.raises(UpstreamTemporaryError) as excinfo:
        app_mod._load_input_images(
            _messages("http://x/1.png"), deadline=time.monotonic() - 1
        )
    assert excinfo.value.status_code == 503
    assert not called, "已经超时就不该再发起下载"


def test_input_images_without_deadline_keep_old_timeout(monkeypatch):
    """不传 deadline 时行为不变（其他 endpoint 仍在共用这个函数）。"""
    import app as app_mod

    timeouts = []

    class Resp:
        status_code = 200
        content = b"\x89PNG\r\n\x1a\n"
        headers = {"content-type": "image/png"}

    monkeypatch.setattr(
        app_mod.requests,
        "get",
        lambda url, timeout=None: (timeouts.append(timeout), Resp())[1],
    )
    monkeypatch.setattr(
        app_mod, "normalize_input_image", lambda b, m: (b, "image/png", 8, 8)
    )

    app_mod._load_input_images(_messages("http://x/1.png"))
    assert timeouts == [30]


def test_loader_shim_tolerates_legacy_signature():
    """注入的 loader 可能是不认识 deadline 的旧桩，不能因此报错。"""
    from api.routes.generation import _load_input_images_with_deadline

    def legacy(messages):
        return ["ok"]

    assert _load_input_images_with_deadline(legacy, [], time.monotonic() + 5) == ["ok"]
    assert _load_input_images_with_deadline(legacy, [], None) == ["ok"]

    def modern(messages, deadline=None):
        return [deadline]

    got = _load_input_images_with_deadline(modern, [], 123.0)
    assert got == [123.0]


# --- 换号上限 ---


def test_rotation_key_normalizes_dots_and_case():
    from app import _rotation_config_key

    assert _rotation_config_key("images.edits") == "rotation_max_accounts_images_edits"
    # gemini 的 operation_name 是驼峰动态拼的，压平后才是登记用的键名
    assert (
        _rotation_config_key("gemini.generateContent")
        == "rotation_max_accounts_gemini_generatecontent"
    )
    assert _rotation_config_key("") == ""


def test_rotation_cap_defaults(cfg):
    from app import _rotation_max_accounts

    # images.edits 默认设了 5；其他 operation 默认不限
    assert _rotation_max_accounts("images.edits") == 5
    assert _rotation_max_accounts("chat.completions") == 0
    assert _rotation_max_accounts("gemini.generateContent") == 0


def test_rotation_cap_per_operation_override(cfg):
    from app import _rotation_max_accounts

    cfg["rotation_max_accounts_default"] = 3
    cfg["rotation_max_accounts_gemini_generatecontent"] = 7
    assert _rotation_max_accounts("gemini.generateContent") == 7
    assert _rotation_max_accounts("video.generate") == 3, "未单独配置的走 default"


@pytest.mark.parametrize("raw", ["abc", None, True, -5])
def test_dirty_rotation_config_means_unlimited(cfg, raw):
    from app import _rotation_max_accounts

    cfg["rotation_max_accounts_images_edits"] = raw
    assert _rotation_max_accounts("images.edits") == 0


# --- 配置项已在后端四处登记（缺一处就会被静默丢弃） ---


@pytest.mark.parametrize(
    "key",
    [
        "images_edits_deadline_seconds",
        "rotation_max_accounts_default",
        "rotation_max_accounts_images_edits",
    ],
)
def test_new_config_keys_are_registered(key):
    import json
    from pathlib import Path

    from api.schemas import ConfigUpdateRequest
    from core.config_mgr import config_manager

    # 1) 默认字典是唯一的键注册表：没登记的键 load()/update_all() 都会静默丢弃
    assert key in config_manager.config
    # 2) schema
    assert key in ConfigUpdateRequest.model_fields
    # 3) 示例配置
    example = json.loads(
        (Path(__file__).parent.parent / "config" / "config.example.json").read_text()
    )
    assert key in example
    # 4) 管理接口白名单
    admin_src = (
        Path(__file__).parent.parent / "api" / "routes" / "admin.py"
    ).read_text()
    assert f'"{key}"' in admin_src
