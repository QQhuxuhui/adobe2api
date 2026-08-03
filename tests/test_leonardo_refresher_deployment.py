import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _compose_config(overrides=None) -> dict:
    if not shutil.which("docker"):
        pytest.skip("docker is unavailable")
    env = os.environ.copy()
    env.update(
        {
            "LEONARDO_REFRESH_KEY": "test-refresh-key",
            "LEONARDO_PROXY": "http://proxy:10809",
            "NOVNC_PASSWORD": "test-vnc-password",
            "LEONARDO_ACCOUNT_LABEL": "Primary",
        }
    )
    env.update(overrides or {})
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "leonardo",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_declares_optional_isolated_refresher_service():
    config = _compose_config()
    adobe = config["services"]["adobe2api"]
    refresher = config["services"]["leonardo-refresher"]

    assert refresher["profiles"] == ["leonardo"]
    assert refresher["environment"]["ADOBE2API_BASE_URL"] == "http://adobe2api:6001"
    assert refresher["environment"]["LEONARDO_REFRESH_KEY"] == "test-refresh-key"
    assert refresher["environment"]["LEONARDO_PROXY"] == "http://proxy:10809"
    assert adobe["environment"]["LEONARDO_REFRESH_KEY"] == "test-refresh-key"
    assert adobe["environment"]["LEONARDO_PROXY"] == "http://proxy:10809"
    assert "HTTP_PROXY" not in adobe["environment"]
    assert "HTTPS_PROXY" not in adobe["environment"]
    assert "HTTP_PROXY" not in refresher["environment"]
    assert "HTTPS_PROXY" not in refresher["environment"]
    assert refresher["shm_size"] == "1073741824"
    assert "http://127.0.0.1:8080/healthz" in refresher["healthcheck"]["test"][-1]
    assert "host.docker.internal=host-gateway" in adobe["extra_hosts"]
    assert "host.docker.internal=host-gateway" in refresher["extra_hosts"]

    assert any(
        port["host_ip"] == "127.0.0.1"
        and port["published"] == "6080"
        and port["target"] == 6080
        for port in refresher["ports"]
    )
    assert any(
        volume["type"] == "volume"
        and volume["source"] == "leo-profile"
        and volume["target"] == "/profile"
        for volume in refresher["volumes"]
    )
    assert "leo-profile" in config["volumes"]


def test_sidecar_image_and_vnc_auth_are_pinned_and_explicit():
    dockerfile = (ROOT / "leonardo_refresher" / "Dockerfile").read_text()
    requirements = (ROOT / "leonardo_refresher" / "requirements.txt").read_text()
    entrypoint = (ROOT / "leonardo_refresher" / "entrypoint.sh").read_text()
    adapters = (ROOT / "leonardo_refresher" / "adapters.py").read_text()
    seccomp = ROOT / "leonardo_refresher" / "seccomp_profile.json"

    assert "mcr.microsoft.com/playwright/python:v1.61.0-noble" in dockerfile
    assert "playwright==1.61.0" in requirements
    assert "requests==2.31.0" in requirements
    assert "x11vnc -storepasswd" in entrypoint
    assert "-rfbauth" in entrypoint
    assert "-nopw" not in entrypoint
    assert "NOVNC_PASSWORD" in entrypoint
    assert "/tmp/.X11-unix/X${display_number}" in entrypoint
    assert 'kill -0 "${xvfb_pid}"' in entrypoint
    assert "gosu" in dockerfile
    assert "groupadd --system pwuser" not in dockerfile
    assert "useradd --system" not in dockerfile
    assert 'exec gosu pwuser "$0" "$@"' in entrypoint
    assert 'chown -R pwuser:pwuser "${PROFILE_DIR}"' in entrypoint
    assert "--no-sandbox" not in adapters
    assert seccomp.exists()
    profile = json.loads(seccomp.read_text())
    namespace_rule = next(
        rule for rule in profile["syscalls"] if "clone" in rule.get("names", [])
    )
    assert namespace_rule["action"] == "SCMP_ACT_ALLOW"


def test_compose_uses_one_ttl_setting_and_enables_chromium_sandbox():
    config = _compose_config({"LEONARDO_TOKEN_MIN_TTL_SECONDS": "777"})
    adobe = config["services"]["adobe2api"]
    refresher = config["services"]["leonardo-refresher"]

    assert adobe["environment"]["LEONARDO_TOKEN_MIN_TTL_SECONDS"] == "777"
    assert refresher["environment"]["SAFETY_MARGIN_SECONDS"] == "777"
    assert any(
        option.endswith("leonardo_refresher/seccomp_profile.json")
        for option in refresher["security_opt"]
    )
