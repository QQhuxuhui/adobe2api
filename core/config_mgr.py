import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = DATA_DIR / "config.json"


class ConfigManager:
    def __init__(self):
        self._lock = threading.Lock()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Default config
        self.config = {
            "api_key": "projectx_webapp",
            "admin_username": "admin",
            "admin_password": "admin",
            "admin_session_secret": "adobe2api-change-this-session-secret",
            "public_base_url": "http://127.0.0.1:6001/",
            "proxy": "",
            "use_proxy": False,
            "generate_timeout": 300,
            "gemini_native_deadline_seconds": 500,
            # /v1/images/edits 的端到端时限（秒），0=不限。必须小于下游网关的
            # upstream 超时，让最内层先放弃，避免「上游已断开、这边还在跑并计费」。
            "images_edits_deadline_seconds": 300,
            # 单个请求最多试几个账号（换号上限），0=不限。deadline 是主保险，
            # 这是第二道：号池很大时挨个试过去也能把预算烧光。
            # 按 operation 覆盖：rotation_max_accounts_<operation 小写且点转下划线>，
            # 例如 images.edits → rotation_max_accounts_images_edits。
            "rotation_max_accounts_default": 0,
            "rotation_max_accounts_images_edits": 5,
            # Leonardo 余额自动刷新间隔（分钟）。Leonardo 的 refresher 是独立进程、
            # 推完 token 就走，不像 Adobe 那样顺带查余额；而配额出池的账号没有请求
            # 去顺带刷它，余额刷新就是它唯一的复活触发器。
            "leonardo_credits_refresh_minutes": 10,
            "refresh_interval_hours": 15,
            "retry_enabled": True,
            "retry_max_attempts": 3,
            "auth_failure_threshold": 3,
            "retry_backoff_seconds": 1.0,
            "retry_on_status_codes": [429, 451, 500, 502, 503, 504],
            "retry_on_error_types": ["timeout", "connection", "proxy"],
            "token_rotation_strategy": "round_robin",
            # 429 后该账号冷却多久（秒）。上游带 Retry-After 时以 Retry-After 为准。
            "rate_limit_cooldown_seconds": 60,
            # 并发闸门 + 账号在飞占用锁 + 排队机制
            "concurrency_gate_enabled": True,
            "max_inflight_per_account": 1,       # 每个账号最多同时在飞几个请求
            "account_queue_size": 100,           # 账号满载后最多多少个请求排队等待
            "account_queue_timeout_seconds": 25, # 排队最长等多久（应 < 反代超时）
            "batch_concurrency": 5,
            "generated_max_size_mb": 1024,
            "generated_prune_size_mb": 200,
            "gpt_image_quality": "low",
        }
        self.load()

    def load(self):
        with self._lock:
            source = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
            if source.exists():
                try:
                    data = json.loads(source.read_text(encoding="utf-8"))
                    for k, v in data.items():
                        if k in self.config:
                            self.config[k] = v
                    if source == LEGACY_CONFIG_FILE and not CONFIG_FILE.exists():
                        CONFIG_FILE.write_text(
                            json.dumps(self.config, indent=2), encoding="utf-8"
                        )
                except Exception:
                    pass

    def save(self):
        with self._lock:
            CONFIG_FILE.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def get(self, key, default=None):
        with self._lock:
            return self.config.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.config[key] = value
        self.save()

    def get_all(self):
        with self._lock:
            return dict(self.config)

    def update_all(self, data: dict):
        with self._lock:
            for k, v in data.items():
                if k in self.config:
                    self.config[k] = v
        self.save()


config_manager = ConfigManager()
