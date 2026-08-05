import json
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import urlparse


def _read_int(
    env: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = env.get(name, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def playwright_proxy(proxy: str):
    """把 `http://user:pass@host:port` 解析成 Playwright 的 proxy dict。

    Playwright 要求 server 不含凭据、username/password 单列;住宅代理带认证时必须拆开。
    留空返回 None（直连）。解析失败退化为 {"server": 原串}。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    try:
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        if not u.hostname:
            return {"server": proxy}
        scheme = u.scheme or "http"
        server = f"{scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        cfg = {"server": server}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg
    except Exception:  # noqa: BLE001
        return {"server": proxy}


def _parse_login_accounts(raw: str):
    """LEONARDO_LOGIN_ACCOUNTS：JSON 数组 [{"email","password"}, ...] → ((email,password),...)。"""
    raw = str(raw or "").strip()
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    accs = []
    for it in data if isinstance(data, list) else []:
        email = str((it or {}).get("email") or "").strip()
        password = str((it or {}).get("password") or "")
        if email and password:
            accs.append((email, password))
    return tuple(accs)


@dataclass(frozen=True)
class RefresherConfig:
    adobe2api_base_url: str
    refresh_key: str = field(repr=False)
    proxy: str = ""
    novnc_password: str = field(default="", repr=False)  # 已弃用(headless,无 noVNC)
    account_label: str = "Leonardo"
    refresh_interval_seconds: int = 3000
    safety_margin_seconds: int = 600
    min_interval_seconds: int = 60
    # 多账号：每隔这么久瞄一眼 cookie 列表，发现新导入的账号就立刻刷（秒级生效）。
    # 只做一次轻量 HTTP 拉取；不到期的账号不会真的跑浏览器。
    poll_interval_seconds: int = 15
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    profile_dir: str = "/profile"
    # 自动登录（复制会话 ~2.5h 被上游作废的真解）：email/password 账号，refresher 用
    # 住宅代理+YesCaptcha 解 Turnstile 直接铸新会话，掉线自动重登。留空则只走 cookie 上传模式。
    login_accounts: tuple = ()
    yescaptcha_key: str = field(default="", repr=False)
    turnstile_sitekey: str = "0x4AAAAAAAjpS3rLKnsHyb79"
    login_url: str = "https://app.leonardo.ai/auth/login"

    def playwright_proxy(self):
        return playwright_proxy(self.proxy)

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
    ) -> "RefresherConfig":
        source = os.environ if env is None else env
        base_url = str(
            source.get("ADOBE2API_BASE_URL", "http://adobe2api:6001") or ""
        ).strip().rstrip("/")
        required = {
            "ADOBE2API_BASE_URL": base_url,
            "LEONARDO_REFRESH_KEY": str(
                source.get("LEONARDO_REFRESH_KEY", "") or ""
            ).strip(),
        }
        for name, value in required.items():
            if not value:
                raise ValueError(f"{name} is required")

        # 代理可选：留空＝直连（如出口在非受限地区）。空时 Chromium 不传 proxy。
        proxy = str(source.get("LEONARDO_PROXY", "") or "").strip()

        min_interval = _read_int(source, "MIN_INTERVAL_SECONDS", 60)
        safety_margin = _read_int(source, "SAFETY_MARGIN_SECONDS", 600)
        refresh_interval = _read_int(source, "REFRESH_INTERVAL_SECONDS", 3000)
        poll_interval = _read_int(source, "POLL_INTERVAL_SECONDS", 15)
        health_port = _read_int(source, "HEALTH_PORT", 8080)
        if poll_interval <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS must be greater than zero")
        if min_interval <= 0:
            raise ValueError("MIN_INTERVAL_SECONDS must be greater than zero")
        if safety_margin <= min_interval:
            raise ValueError(
                "SAFETY_MARGIN_SECONDS must be greater than MIN_INTERVAL_SECONDS"
            )
        if refresh_interval <= min_interval:
            raise ValueError(
                "REFRESH_INTERVAL_SECONDS must be greater than MIN_INTERVAL_SECONDS"
            )
        if not 1 <= health_port <= 65535:
            raise ValueError("HEALTH_PORT must be between 1 and 65535")

        return cls(
            adobe2api_base_url=base_url,
            refresh_key=required["LEONARDO_REFRESH_KEY"],
            proxy=proxy,
            novnc_password=str(source.get("NOVNC_PASSWORD", "") or "").strip(),
            account_label=str(
                source.get("LEONARDO_ACCOUNT_LABEL", "Leonardo") or "Leonardo"
            ).strip()
            or "Leonardo",
            refresh_interval_seconds=refresh_interval,
            safety_margin_seconds=safety_margin,
            min_interval_seconds=min_interval,
            poll_interval_seconds=poll_interval,
            health_host=str(source.get("HEALTH_HOST", "0.0.0.0") or "0.0.0.0").strip(),
            health_port=health_port,
            profile_dir=str(source.get("PROFILE_DIR", "/profile") or "/profile").strip(),
            login_accounts=_parse_login_accounts(source.get("LEONARDO_LOGIN_ACCOUNTS", "")),
            yescaptcha_key=str(source.get("YESCAPTCHA_CLIENT_KEY", "") or "").strip(),
            turnstile_sitekey=str(
                source.get("LEONARDO_TURNSTILE_SITEKEY", "0x4AAAAAAAjpS3rLKnsHyb79")
                or "0x4AAAAAAAjpS3rLKnsHyb79"
            ).strip(),
            login_url=str(
                source.get("LEONARDO_LOGIN_URL", "https://app.leonardo.ai/auth/login")
                or "https://app.leonardo.ai/auth/login"
            ).strip(),
        )
