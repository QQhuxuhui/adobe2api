from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request


def test_generated_route_rejects_symlink_outside_root(tmp_path: Path, monkeypatch):
    try:
        import app as app_module
    except Exception as exc:  # pragma: no cover - environment import guard
        pytest.skip(str(exc))

    outside = tmp_path.parent / "outside-generated.mp4"
    outside.write_bytes(b"private")
    link = tmp_path / "served.mp4"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    monkeypatch.setattr(app_module, "GENERATED_DIR", tmp_path)
    response = TestClient(app_module.app).get("/generated/served.mp4")
    assert response.status_code == 404


def test_public_base_url_environment_overrides_default_config(monkeypatch):
    try:
        import app as app_module
    except Exception as exc:  # pragma: no cover - environment import guard
        pytest.skip(str(exc))

    original_get = app_module.config_manager.get

    def config_get(key, default=None):
        if key == "public_base_url":
            return "http://127.0.0.1:6001/"
        return original_get(key, default)

    monkeypatch.setattr(app_module.config_manager, "get", config_get)
    monkeypatch.setenv("ADOBE_PUBLIC_BASE_URL", "https://videos.example/base")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )

    assert (
        app_module._public_generated_url(request, "result.mp4")
        == "https://videos.example/base/generated/result.mp4"
    )
