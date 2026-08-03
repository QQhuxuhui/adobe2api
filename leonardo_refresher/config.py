import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


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


@dataclass(frozen=True)
class RefresherConfig:
    adobe2api_base_url: str
    refresh_key: str = field(repr=False)
    proxy: str
    novnc_password: str = field(repr=False)
    account_label: str = "Leonardo"
    refresh_interval_seconds: int = 3000
    safety_margin_seconds: int = 600
    min_interval_seconds: int = 60
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    profile_dir: str = "/profile"

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
            "NOVNC_PASSWORD": str(
                source.get("NOVNC_PASSWORD", "") or ""
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
        health_port = _read_int(source, "HEALTH_PORT", 8080)
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
            novnc_password=required["NOVNC_PASSWORD"],
            account_label=str(
                source.get("LEONARDO_ACCOUNT_LABEL", "Leonardo") or "Leonardo"
            ).strip()
            or "Leonardo",
            refresh_interval_seconds=refresh_interval,
            safety_margin_seconds=safety_margin,
            min_interval_seconds=min_interval,
            health_host=str(source.get("HEALTH_HOST", "0.0.0.0") or "0.0.0.0").strip(),
            health_port=health_port,
            profile_dir=str(source.get("PROFILE_DIR", "/profile") or "/profile").strip(),
        )
