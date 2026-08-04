import json
import base64
import copy
import logging
import os
import threading
import time
import uuid
import random
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from core.leonardo_client import is_likely_leonardo_token

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
DATA_FILE = CONFIG_DIR / "tokens.json"
LEGACY_DATA_FILE = DATA_DIR / "tokens.json"

logger = logging.getLogger(__name__)


class TokenManager:
    ERROR_COOLDOWN_SECONDS = 180
    # 429 冷却的封顶：无论 Retry-After 说多久，最多冷却这么久（秒）
    MAX_COOLDOWN_SECONDS = 3600

    def __init__(self):
        self._lock = threading.Lock()
        self.tokens: List[Dict] = []
        self._rr_index = 0
        # 账号内轮换（get_available_for_account）的独立游标，按账号 key 分开，
        # 不碰全局 _rr_index——否则「只有一行的账号池」会把全局游标夹回 0。
        self._rr_cursors: Dict[str, int] = {}
        self._unreadable_data_file: Optional[Path] = None
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        with self._lock:
            source = DATA_FILE if DATA_FILE.exists() else LEGACY_DATA_FILE
            if source.exists():
                try:
                    self.tokens = json.loads(source.read_text(encoding="utf-8"))
                    now_ts = time.time()
                    for t in self.tokens:
                        if not isinstance(t, dict):
                            continue
                        t.setdefault("id", uuid.uuid4().hex[:8])
                        t.setdefault("value", "")
                        t.setdefault("status", "active")
                        t.setdefault("fails", 0)
                        t.setdefault("added_at", now_ts)
                        t.setdefault("error_until", 0)
                        # 老库没有这个字段：视为「从未用过」，重启后先轮到它们
                        t.setdefault("last_used_at", 0)
                    if source == LEGACY_DATA_FILE and not DATA_FILE.exists():
                        self.save()
                    self._unreadable_data_file = None
                except Exception as exc:
                    self.tokens = []
                    if source == DATA_FILE:
                        self._unreadable_data_file = source
                        logger.error(
                            "failed to load token file %s after %s; "
                            "refusing to overwrite the unreadable file",
                            source,
                            type(exc).__name__,
                        )
                    else:
                        logger.error(
                            "failed to load legacy token file %s after %s",
                            source,
                            type(exc).__name__,
                        )

    def save(self):
        if (
            self._unreadable_data_file == DATA_FILE
            and self._unreadable_data_file.exists()
        ):
            raise RuntimeError(
                "refusing to overwrite unreadable token file; repair or move it first"
            )

        payload = json.dumps(self.tokens, indent=2)
        existing_metadata = DATA_FILE.stat() if DATA_FILE.exists() else None
        file_mode = (
            existing_metadata.st_mode & 0o777
            if existing_metadata is not None
            else 0o600
        )
        temp_file = DATA_FILE.with_name(
            f".{DATA_FILE.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp_file.open("w", encoding="utf-8") as handle:
                os.chmod(temp_file, file_mode)
                if existing_metadata is not None:
                    try:
                        os.chown(
                            temp_file,
                            existing_metadata.st_uid,
                            existing_metadata.st_gid,
                        )
                    except PermissionError:
                        pass
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, DATA_FILE)
            directory_fd = os.open(DATA_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp_file.unlink(missing_ok=True)

    def add(self, value: str, meta: Optional[Dict] = None):
        with self._lock:
            value = value.strip()
            if value.startswith("Bearer "):
                value = value[7:].strip()
            meta = dict(meta or {})
            account_id = self.account_id_from_token(value)
            if account_id and not meta.get("account_id"):
                meta["account_id"] = account_id

            for t in self.tokens:
                if t["value"] == value:
                    if meta:
                        t.update(meta)
                        self.save()
                    return t

            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": time.time(),
                "error_until": 0,
                "last_used_at": 0,
            }
            # 未显式指定 type 时按 token 形态自动判定；meta 含 type 则以其为准
            if not meta or "type" not in meta:
                if is_likely_leonardo_token(value):
                    new_token["type"] = "leonardo"
            if meta:
                new_token.update(meta)
            self.tokens.append(new_token)
            self.save()
            return new_token

    def upsert_auto_refresh_token(
        self,
        value: str,
        profile_id: str,
        profile_name: Optional[str] = None,
        profile_email: Optional[str] = None,
    ):
        with self._lock:
            value = value.strip()
            if value.startswith("Bearer "):
                value = value[7:].strip()

            now_ts = time.time()
            pid = str(profile_id or "").strip()
            account_id = self.account_id_from_token(value)
            if not pid:
                raise ValueError("profile_id is required")

            target = None
            for t in self.tokens:
                if (
                    t.get("auto_refresh") is True
                    and str(t.get("refresh_profile_id") or "").strip() == pid
                ):
                    target = t
                    break

            if target is not None:
                target["value"] = value
                target["status"] = "active"
                target["fails"] = 0
                # 刷新只换了 token 值，账号没变；限流是账号级的，正在冷却中就别清零，
                # 否则一次自动刷新会把 429 冷却窗口悄悄抹掉。已过期的照常归零。
                if float(target.get("error_until") or 0) <= now_ts:
                    target["error_until"] = 0
                target["updated_at"] = now_ts
                target["source"] = "auto_refresh"
                target["auto_refresh"] = True
                target["refresh_profile_id"] = pid
                target["refresh_profile_name"] = str(profile_name or "").strip() or pid
                target["refresh_profile_email"] = str(profile_email or "").strip()
                if account_id:
                    target["account_id"] = account_id
                self.save()
                return dict(target)

            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": now_ts,
                "updated_at": now_ts,
                "error_until": 0,
                "last_used_at": 0,
                "source": "auto_refresh",
                "auto_refresh": True,
                "refresh_profile_id": pid,
                "refresh_profile_name": str(profile_name or "").strip() or pid,
                "refresh_profile_email": str(profile_email or "").strip(),
                "account_id": account_id,
            }
            self.tokens.append(new_token)
            self.save()
            return dict(new_token)

    def upsert_leonardo_token(
        self,
        value: str,
        account_id: str,
        label: Optional[str] = None,
    ) -> Dict:
        with self._lock:
            token_value = str(value or "").strip()
            if token_value.startswith("Bearer "):
                token_value = token_value[7:].strip()

            aid = str(account_id or "").strip()
            token_account_id = self.account_id_from_token(token_value)
            incoming_exp = self._decode_jwt_exp(token_value) or 0
            if not aid or token_account_id != aid:
                raise ValueError("account_id does not match token")
            if incoming_exp <= 0:
                raise ValueError("token exp is required")

            original_tokens = copy.deepcopy(self.tokens)

            def persist_or_rollback() -> None:
                try:
                    self.save()
                except Exception:
                    self.tokens = original_tokens
                    raise

            matches = [
                item
                for item in self.tokens
                if item.get("type") == "leonardo"
                and str(
                    item.get("account_id")
                    or self.account_id_from_token(item.get("value") or "")
                ).strip()
                == aid
            ]
            target = max(
                matches,
                key=lambda item: self._decode_jwt_exp(item.get("value") or "") or 0,
                default=None,
            )
            changed = False

            if target is not None and len(matches) > 1:
                duplicate_object_ids = {
                    id(item) for item in matches if item is not target
                }
                self.tokens = [
                    item for item in self.tokens if id(item) not in duplicate_object_ids
                ]
                changed = True

            if target is not None:
                target_exp = self._decode_jwt_exp(target.get("value") or "") or 0
                if incoming_exp < target_exp:
                    if changed:
                        persist_or_rollback()
                    return {"status": "noop", "token": dict(target)}

            now_ts = time.time()
            profile_name = str(label or "").strip() or aid
            desired = {
                "value": token_value,
                "status": "active",
                "fails": 0,
                "error_until": 0,
                "type": "leonardo",
                "source": "leonardo_refresher",
                "account_id": aid,
                "refresh_profile_name": profile_name,
                "refresh_profile_email": "",
            }

            if target is None:
                target = {
                    "id": uuid.uuid4().hex[:8],
                    "added_at": now_ts,
                    "updated_at": now_ts,
                    **desired,
                }
                self.tokens.append(target)
                persist_or_rollback()
                return {"status": "created", "token": dict(target)}

            if all(target.get(key) == expected for key, expected in desired.items()):
                if changed:
                    persist_or_rollback()
                return {"status": "noop", "token": dict(target)}

            target.update(desired)
            target["updated_at"] = now_ts
            persist_or_rollback()
            return {"status": "updated", "token": dict(target)}

    def remove(self, tid: str):
        with self._lock:
            self.tokens = [t for t in self.tokens if t["id"] != tid]
            self.save()

    def remove_auto_refresh_by_profile(self, profile_id: str):
        pid = str(profile_id or "").strip()
        if not pid:
            return
        with self._lock:
            self.tokens = [
                t
                for t in self.tokens
                if not (
                    t.get("auto_refresh") is True
                    and str(t.get("refresh_profile_id") or "").strip() == pid
                )
            ]
            self.save()

    def get_by_id(self, tid: str) -> Optional[Dict]:
        with self._lock:
            for t in self.tokens:
                if t.get("id") == tid:
                    return dict(t)
        return None

    def get_meta_by_value(self, value: str) -> Dict:
        token_value = str(value or "").strip()
        with self._lock:
            for t in self.tokens:
                if str(t.get("value") or "").strip() != token_value:
                    continue
                return {
                    "token_id": t.get("id"),
                    "token_account_id": t.get("account_id") or self.account_id_from_token(token_value),
                    "token_account_name": t.get("refresh_profile_name") or "",
                    "token_account_email": t.get("refresh_profile_email") or "",
                    "token_source": t.get("source") or "manual",
                    "refresh_profile_id": t.get("refresh_profile_id") or "",
                }
        return {
            "token_id": "",
            "token_account_id": "",
            "token_account_name": "",
            "token_account_email": "",
            "token_source": "manual",
            "refresh_profile_id": "",
        }

    def set_status(self, tid: str, status: str):
        with self._lock:
            for t in self.tokens:
                if t["id"] == tid:
                    t["status"] = status
                    t["fails"] = 0 if status == "active" else t["fails"]
                    if status == "active":
                        t["error_until"] = 0
            self.save()

    def set_credits(self, tid: str, credits: Dict):
        with self._lock:
            for t in self.tokens:
                if t.get("id") != tid:
                    continue
                t["credits_total"] = credits.get("total")
                t["credits_used"] = credits.get("used")
                t["credits_available"] = credits.get("available")
                t["credits_available_until"] = credits.get("available_until")
                t["credits_updated_at"] = credits.get("updated_at") or int(time.time())
                t["credits_error"] = ""
                self.save()
                return dict(t)
        return None

    def set_credits_error(self, tid: str, error_message: str):
        with self._lock:
            for t in self.tokens:
                if t.get("id") != tid:
                    continue
                t["credits_error"] = str(error_message or "")[:300]
                t["credits_updated_at"] = int(time.time())
                self.save()
                return dict(t)
        return None

    def list_active_ids(self) -> List[str]:
        with self._lock:
            return [
                str(t.get("id") or "")
                for t in self.tokens
                if t.get("status") == "active"
            ]

    def has_active_token(self, token_type: Optional[str] = None) -> bool:
        """池中是否存在指定类型的可用 token（非消费、不推进轮询）。

        token_type: "leonardo" 只看 Leonardo；"adobe" 看非 Leonardo；None 看全部。
        用于按 token 类型自动选择出图后端（Adobe / Leonardo）。
        """
        with self._lock:
            active = [t for t in self.tokens if t.get("status") in {"active", "error"}]
            if token_type == "leonardo":
                return any(t.get("type") == "leonardo" for t in active)
            if token_type == "adobe":
                return any(t.get("type") != "leonardo" for t in active)
            return bool(active)

    @staticmethod
    def _account_key(t: Dict) -> str:
        """账号身份。

        同一个账号可能占多行 token（手动导入一行、自动刷新又一行）。上游限流是按
        账号算的，所以调度也必须按账号来，否则「轮换」会在同一个账号的两行之间空转。
        """
        return (
            str(t.get("account_id") or "").strip()
            or str(t.get("refresh_profile_id") or "").strip()
            or str(t.get("value") or "").strip()
        )

    @staticmethod
    def _cooldown_seconds() -> float:
        from core.config_mgr import config_manager

        raw = config_manager.get("rate_limit_cooldown_seconds", 60)
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return 60.0
        return seconds if seconds >= 0 else 60.0

    def _ready_pool_locked(self, active: List[Dict]) -> List[Dict]:
        """滤掉还在限流冷却里的 token。

        全部都在冷却时不能直接返回空——那会让整个池子暂时不可用；此时退而求其次，
        交出最早解冻的那个，由上层的重试/报错去处理。
        """
        now = time.time()
        ready = [t for t in active if float(t.get("error_until") or 0) <= now]
        if ready:
            return ready
        return [min(active, key=lambda t: float(t.get("error_until") or 0))]

    def _mark_used_locked(self, chosen: Dict) -> None:
        """记账号的最近使用时间，同账号的所有行一起更新。

        在「选中」时就打点而不是等请求结束，是为了让正在飞行中的账号立刻显得「刚用过」，
        并发请求才不会同时落到同一个账号上。
        """
        now = time.time()
        key = self._account_key(chosen)
        for t in self.tokens:
            if self._account_key(t) == key:
                t["last_used_at"] = now
        self.save()

    def _choose_locked(
        self, pool: List[Dict], mode: str, cursor_key: str = ""
    ) -> Dict:
        if mode in {"least_recently_used", "lru"}:
            by_account: Dict[str, List[Dict]] = {}
            for t in pool:
                by_account.setdefault(self._account_key(t), []).append(t)

            def account_last_used(rows: List[Dict]) -> float:
                return max(float(r.get("last_used_at") or 0) for r in rows)

            # 账号按「最近一次被用」排序取最旧的；账号内部再取最旧的那一行。
            # 并列时用 key/id 兜底，保证顺序稳定、可测试。
            _, rows = min(
                by_account.items(), key=lambda kv: (account_last_used(kv[1]), kv[0])
            )
            return min(
                rows,
                key=lambda r: (float(r.get("last_used_at") or 0), str(r.get("id") or "")),
            )
        if mode == "random":
            return random.choice(pool)
        # round_robin：只在「读」时对当前池子大小取模；写回时**不能**再 % len(pool)，
        # 否则 get_available_for_account 传进来的单行账号池会把游标夹成 0，
        # 让默认策略永远塌缩到第一个账号。账号内轮换用各自的游标，不动全局位置。
        if cursor_key:
            idx = self._rr_cursors.get(cursor_key, 0) % len(pool)
            self._rr_cursors[cursor_key] = idx + 1
        else:
            idx = self._rr_index % len(pool)
            self._rr_index = idx + 1
        return pool[idx]

    def _pick_active_token_locked(
        self, strategy: str = "round_robin", token_type: Optional[str] = None
    ) -> Optional[Dict]:
        active = [t for t in self.tokens if t.get("status") in {"active", "error"}]
        if token_type == "leonardo":
            active = [t for t in active if t.get("type") == "leonardo"]
        elif token_type == "adobe":
            active = [t for t in active if t.get("type") != "leonardo"]
        if not active:
            return None

        mode = str(strategy or "round_robin").strip().lower()
        chosen = self._choose_locked(self._ready_pool_locked(active), mode)
        self._mark_used_locked(chosen)
        return chosen

    def get_available(
        self,
        strategy: str = "round_robin",
        token_type: Optional[str] = "adobe",
    ) -> Optional[str]:
        with self._lock:
            chosen = self._pick_active_token_locked(strategy=strategy, token_type=token_type)
            return chosen["value"] if chosen is not None else None

    @classmethod
    def account_id_from_token(cls, value: str) -> str:
        data = cls._decode_jwt_payload(value)
        if not data:
            return ""
        return str(
            data.get("user_id") or data.get("aa_id") or data.get("sub") or ""
        ).strip()

    def get_available_for_account(
        self, account_id: str, strategy: str = "round_robin"
    ) -> Optional[str]:
        aid = str(account_id or "").strip()
        if not aid:
            return None
        with self._lock:
            active = [
                t
                for t in self.tokens
                if t.get("status") in {"active", "error"}
                and str(t.get("account_id") or self.account_id_from_token(t.get("value") or ""))
                == aid
            ]
            if not active:
                return None
            mode = str(strategy or "round_robin").strip().lower()
            chosen = self._choose_locked(
                self._ready_pool_locked(active), mode, cursor_key=aid
            )
            self._mark_used_locked(chosen)
            return chosen["value"]

    def list_active_account_tokens(self) -> List[Dict]:
        with self._lock:
            items = []
            seen = set()
            for t in self.tokens:
                if t.get("status") != "active":
                    continue
                value = str(t.get("value") or "").strip()
                aid = str(t.get("account_id") or self.account_id_from_token(value)).strip()
                if not value or not aid or aid in seen:
                    continue
                seen.add(aid)
                items.append(
                    {
                        "token": value,
                        "account_id": aid,
                        "type": str(t.get("type") or ""),
                        "account_name": str(t.get("refresh_profile_name") or ""),
                        "account_email": str(t.get("refresh_profile_email") or ""),
                    }
                )
            return items

    def report_exhausted(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["status"] = "exhausted"
                    t["error_until"] = 0
            self.save()

    def report_invalid(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["status"] = "invalid"
                    t["error_until"] = 0
            self.save()

    def report_rate_limited(
        self, value: str, retry_after: Optional[float] = None
    ) -> float:
        """上游 429：让这个账号冷却一段时间，别让下一个请求立刻又打上去。

        限流是账号级的，所以同账号的每一行 token 一起冷却。上游给了 Retry-After 就照它，
        否则用配置里的 rate_limit_cooldown_seconds。返回实际冷却秒数（0 表示没生效）。

        Retry-After 是上游可控的值，会被写进 tokens.json 持久化，所以要封顶——
        一个畸形/恶意的头不能把账号封禁几个小时。
        """
        token_value = str(value or "").strip()
        if not token_value:
            return 0.0
        try:
            cooldown = float(retry_after or 0)
        except (TypeError, ValueError):
            cooldown = 0.0
        if cooldown <= 0:
            cooldown = self._cooldown_seconds()
        if cooldown <= 0:
            return 0.0
        cooldown = min(cooldown, self.MAX_COOLDOWN_SECONDS)

        until = time.time() + cooldown
        with self._lock:
            target = None
            for t in self.tokens:
                if str(t.get("value") or "").strip() == token_value:
                    target = t
                    break
            if target is None:
                return 0.0
            key = self._account_key(target)
            for t in self.tokens:
                # 已有更晚的解冻时间就别缩短它
                if self._account_key(t) == key and float(t.get("error_until") or 0) < until:
                    t["error_until"] = until
            self.save()
        return cooldown

    def handle_auth_failure(
        self, value: str, *, refresh_credits: bool = True
    ) -> Dict:
        token_value = str(value or "").strip()
        linked_profile_id = ""
        linked_auto_refresh = False

        with self._lock:
            for t in self.tokens:
                if str(t.get("value") or "").strip() != token_value:
                    continue
                linked_profile_id = str(t.get("refresh_profile_id") or "").strip()
                linked_auto_refresh = bool(t.get("auto_refresh"))
                break

        if not linked_auto_refresh or not linked_profile_id:
            self.report_invalid(token_value)
            return {
                "status": "invalid",
                "message": "token invalid or expired",
                "http_status": 401,
                "profile_id": linked_profile_id,
            }

        try:
            from core.refresh_mgr import refresh_manager

            refresh_result = refresh_manager.refresh_once(
                linked_profile_id,
                refresh_credits=refresh_credits,
            )
        except Exception as exc:
            fails_now = self.report_error(token_value)
            threshold = self._auth_failure_threshold()
            if fails_now >= threshold:
                self.report_invalid(token_value)
                return {
                    "status": "invalid",
                    "message": (
                        f"auto refresh failed {fails_now} times "
                        f"(threshold {threshold}): {exc}"
                    ),
                    "http_status": 401,
                    "profile_id": linked_profile_id,
                }
            return {
                "status": "retry",
                "message": f"auto refresh failed ({fails_now}/{threshold}): {exc}",
                "http_status": None,
                "profile_id": linked_profile_id,
            }

        return {
            "status": "refreshed",
            "message": "token refreshed via cookie",
            "http_status": 200,
            "profile_id": linked_profile_id,
            "result": refresh_result,
        }

    @staticmethod
    def _auth_failure_threshold() -> int:
        from core.config_mgr import config_manager

        raw = config_manager.get("auth_failure_threshold", 3)
        try:
            threshold = int(str(raw).strip())
        except Exception:
            return 3
        if threshold < 1:
            return 1
        return threshold

    def report_error(self, value: str) -> int:
        fails_now = 0
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] += 1
                    t["updated_at"] = time.time()
                    fails_now = int(t["fails"])
            self.save()
        return fails_now

    def report_success(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] = 0
                    if t["status"] == "error":
                        t["status"] = "active"
                        t["error_until"] = 0
            self.save()

    @staticmethod
    def _decode_jwt_payload(value: str) -> Optional[dict]:
        token = str(value or "").strip()
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    @classmethod
    def _decode_jwt_exp(cls, value: str) -> Optional[int]:
        data = cls._decode_jwt_payload(value)
        if not data:
            return None

        exp = data.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp)

        # Adobe tokens often expose created_at + expires_in in payload instead of exp.
        created_at = data.get("created_at")
        expires_in = data.get("expires_in")
        try:
            created_at_val = int(str(created_at).strip())
            expires_in_val = int(str(expires_in).strip())
        except Exception:
            return None

        if created_at_val <= 0 or expires_in_val <= 0:
            return None

        # Some fields are milliseconds (e.g. 1771862511913 / 86400000)
        if created_at_val > 10_000_000_000:
            created_at_val = int(created_at_val / 1000)
        if expires_in_val > 86400 * 2:
            expires_in_val = int(expires_in_val / 1000)

        return created_at_val + expires_in_val

    def list_all(self):
        with self._lock:
            res = []
            now_ts = int(time.time())
            for t in self.tokens:
                # mask value
                val = t["value"]
                masked = val[:15] + "..." + val[-10:] if len(val) > 30 else "***"
                exp_ts = self._decode_jwt_exp(val)
                remaining_seconds = None
                exp_readable = None
                if exp_ts is not None:
                    remaining_seconds = exp_ts - now_ts
                    try:
                        exp_readable = datetime.fromtimestamp(exp_ts).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    except Exception:
                        exp_readable = str(exp_ts)
                res.append(
                    {
                        "id": t["id"],
                        "value": masked,
                        "status": t["status"],
                        "fails": t["fails"],
                        "added_at": t["added_at"],
                        "error_until": t.get("error_until", 0),
                        "cooling_down": bool(
                            float(t.get("error_until") or 0) > now_ts
                        ),
                        "last_used_at": t.get("last_used_at", 0),
                        "source": t.get("source", "manual"),
                        "auto_refresh": bool(t.get("auto_refresh", False)),
                        "refresh_profile_id": t.get("refresh_profile_id"),
                        "refresh_profile_name": t.get("refresh_profile_name"),
                        "refresh_profile_email": t.get("refresh_profile_email"),
                        "credits_total": t.get("credits_total"),
                        "credits_used": t.get("credits_used"),
                        "credits_available": t.get("credits_available"),
                        "credits_available_until": t.get("credits_available_until"),
                        "credits_updated_at": t.get("credits_updated_at"),
                        "credits_error": t.get("credits_error", ""),
                        "expires_at": exp_ts,
                        "expires_at_text": exp_readable,
                        "remaining_seconds": remaining_seconds,
                        "is_expired": bool(
                            exp_ts is not None
                            and remaining_seconds is not None
                            and remaining_seconds <= 0
                        ),
                    }
                )
            return res

    def export_tokens(self, ids: Optional[List[str]] = None) -> List[Dict]:
        selected_ids = None
        if isinstance(ids, list):
            normalized = [str(x or "").strip() for x in ids]
            selected_ids = {x for x in normalized if x}
        with self._lock:
            out: List[Dict] = []
            for t in self.tokens:
                tid = str(t.get("id") or "").strip()
                if selected_ids is not None and tid not in selected_ids:
                    continue
                out.append(
                    {
                        "id": tid,
                        "token": str(t.get("value") or "").strip(),
                        "status": str(t.get("status") or "active"),
                        "source": str(t.get("source") or "manual"),
                        "auto_refresh": bool(t.get("auto_refresh", False)),
                        "refresh_profile_id": t.get("refresh_profile_id"),
                        "refresh_profile_name": t.get("refresh_profile_name"),
                        "refresh_profile_email": t.get("refresh_profile_email"),
                        "added_at": t.get("added_at"),
                    }
                )
            return out


token_manager = TokenManager()
