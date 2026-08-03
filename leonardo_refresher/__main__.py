import logging
import signal
import sys
import threading
import time
from typing import Callable, Optional

from leonardo_refresher.adapters import (
    Adobe2ApiTokenSink,
    PlaywrightSessionSource,
)
from leonardo_refresher.config import RefresherConfig
from leonardo_refresher.health import start_health_server
from leonardo_refresher.service import RefresherService, RuntimeState


logger = logging.getLogger("leonardo_refresher")


def run(
    *,
    config: Optional[RefresherConfig] = None,
    source_factory: Callable = lambda cfg: PlaywrightSessionSource(config=cfg),
    sink_factory: Callable = lambda base_url, refresh_key: Adobe2ApiTokenSink(
        base_url=base_url,
        refresh_key=refresh_key,
    ),
    health_factory: Callable = lambda state, host, port: start_health_server(
        state,
        host=host,
        port=port,
    ),
    stop_event=None,
    now: Callable[[], float] = time.time,
) -> int:
    runtime_config = config or RefresherConfig.from_env()
    runtime_state = RuntimeState()
    event = stop_event or threading.Event()
    source = None
    sink = None
    health = None

    previous_handlers = {}
    if stop_event is None:
        def request_stop(signum, frame):
            event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)

    try:
        source = source_factory(runtime_config)
        sink = sink_factory(
            runtime_config.adobe2api_base_url,
            runtime_config.refresh_key,
        )
        health = health_factory(
            runtime_state,
            runtime_config.health_host,
            runtime_config.health_port,
        )
        # 不 eager-open：让首个 run_once 经 fetch_token 懒加载驱动浏览器。
        # eager open 若失败会残留半开状态并绕过 fetch_token 的 _reset_browser
        # 优雅降级，导致容器崩溃循环（见 review）。懒加载失败会归为
        # browser_control → browser_unavailable → healthz 存活 + 按 MIN 重试。
        service = RefresherService(
            source=source,
            sink=sink,
            state=runtime_state,
            config=runtime_config,
            now=now,
        )
        service.run_forever(event)
        return 0
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_error = None
        for resource_name, resource in (
            ("source", source),
            ("sink", sink),
            ("health", health),
        ):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:
                logger.error(
                    "failed to close %s after %s",
                    resource_name,
                    type(exc).__name__,
                )
                if cleanup_error is None:
                    cleanup_error = exc
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if cleanup_error is not None and not active_error:
            raise cleanup_error


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return run()
    except ValueError as exc:
        logger.error("invalid refresher configuration: %s", exc)
        return 2
    except Exception as exc:
        logger.error("refresher stopped after %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
