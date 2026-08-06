import json
import base64
import copy
import logging
import math
import os
import threading
import time
import uuid
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict

from core.leonardo_client import is_likely_leonardo_token

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
DATA_FILE = CONFIG_DIR / "tokens.json"
LEGACY_DATA_FILE = DATA_DIR / "tokens.json"

logger = logging.getLogger(__name__)


class TokenLease:
    """一次账号占用。持有期间该账号被计入 in-flight，用完必须 release。

    leased=False 表示闸门关闭时的「空租约」——没有真正占用计数，release 是空操作，
    只是让调用方的代码路径统一。
    """

    __slots__ = ("token", "account_key", "token_id", "leased")

    def __init__(self, token: str, account_key: str, token_id: str, leased: bool = True):
        self.token = token
        self.account_key = account_key
        self.token_id = token_id
        self.leased = leased


def retire_account_for_quota(
    manager,
    *,
    account_key: str = "",
    token: str = "",
    token_id: str = "",
    operation_name: str = "",
) -> bool:
    """配额耗尽 → 整个账号出池。三条生成链路共用。

    配额是账号级的，而一个账号可能有多行 token（手动导入一行、自动刷新一行）；
    只标一行的话另一行照样会被选中，死号就一直留在调度池里被反复撞。

    账号键优先用调用方手里稳定的那个：app 侧是租约的 account_key，后台循环是
    请求前保存的 token_id。都拿不到才回落到 token 值反查——请求在飞期间 cookie
    刷新可能已经换掉 token 值，反查会落空，所以它只是兜底而不是主路径。

    manager 由调用方传入（测试会注入 Fake），不直接用模块级单例。
    """
    key = str(account_key or "").strip()
    if not key:
        resolver = getattr(manager, "account_key_for_id", None)
        if callable(resolver):
            try:
                key = str(resolver(token_id, fallback_value=token) or "").strip()
            except TypeError:
                # 老签名/测试桩没有 fallback_value 形参
                key = str(resolver(token_id) or "").strip()
    retire = getattr(manager, "report_account_exhausted", None)
    if key and callable(retire) and retire(key):
        return True

    # 拿着旧 key 没命中任何行：请求在飞期间自动刷新可能给该行补上了 account_id，
    # 而 _account_key 是 account_id 优先 —— 键变了，租约里那个已经是旧的。
    # 用稳定的 token_id（刷新只改值不换 id）重新解析一次再试。
    if callable(retire):
        resolver = getattr(manager, "account_key_for_id", None)
        if callable(resolver):
            try:
                fresh = str(resolver(token_id, fallback_value=token) or "").strip()
            except TypeError:
                fresh = str(resolver(token_id) or "").strip()
            if fresh and fresh != key and retire(fresh):
                return True
    manager.report_exhausted(token)
    if not key:
        logger.warning(
            "quota exhausted but account key unresolved (operation=%s); "
            "fell back to token-value retirement",
            operation_name or "-",
        )
    return False


# acquire_lease 的结果原因
LEASE_OK = "ok"
LEASE_NO_TOKEN = "no_token"      # 该请求可用的账号已试完/池里没有该类型
LEASE_QUEUE_FULL = "queue_full"  # 排队人数已达上限，快速失败
LEASE_TIMEOUT = "timeout"        # 排队等待超时（队列超时或请求 deadline）


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
        # 账号在飞占用计数：account_key -> 当前正在执行的请求数。仅存内存，
        # 进程重启即清零（重启后本来就没有在飞请求）。用同一把锁 + 条件变量
        # 实现「账号满载→排队等待→有账号释放立刻唤醒」。
        self._inflight: Dict[str, int] = {}
        self._gate_cond = threading.Condition(self._lock)
        self._waiters = 0
        # 账号级配额事件版本号：account_key -> 单调递增整数，每次「配额耗尽」+1。
        # 余额刷新用它定序——余额请求发起前读一次，落盘时版本没变才允许复活账号。
        # 不能用时间戳代替：余额的 updated_at 是秒级的 int(time.time())，
        # 与耗尽事件同秒时无法比较先后。
        self._quota_epochs: Dict[str, int] = {}
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
                    self._rebuild_quota_epochs_locked()
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
                # exhausted 是账号配额耗尽，与 token 新旧无关：cookie 刷新不得复活它，
                # 否则每个刷新周期都会把死号重新送回池子里挨撞。
                # 复活只走余额驱动那条路（set_credits_and_maybe_revive）。
                if target.get("status") != "exhausted":
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

            # 同账号已经因为配额耗尽出池的话，新建的行必须继承这个终态：
            # 否则 profile 第一次推 token 就凭空造出一行 active，
            # 把整个死账号重新放回调度池。
            account_status = "active"
            if account_id:
                for t in self.tokens:
                    if (
                        str(t.get("account_id") or "").strip() == account_id
                        and t.get("status") == "exhausted"
                    ):
                        account_status = "exhausted"
                        break

            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": account_status,
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
            # 和 upsert_auto_refresh_token 同一条规矩：刷新只换 token 值，
            # 不复活配额耗尽的账号——否则每个刷新周期都把死号送回池子里挨撞。
            # 复活只走余额驱动那条路（set_credits_and_maybe_revive）。
            keeps_exhausted = target is not None and target.get("status") == "exhausted"
            desired = {
                "value": token_value,
                "status": "exhausted" if keeps_exhausted else "active",
                "fails": target.get("fails", 0) if keeps_exhausted else 0,
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
                    # 供调用方按后端分流：Leonardo 的积分是另一套币值，
                    # 不能和 Adobe 混在同一套事后差分/估算里
                    "token_type": str(t.get("type") or ""),
                }
        return {
            "token_id": "",
            "token_account_id": "",
            "token_account_name": "",
            "token_account_email": "",
            "token_source": "manual",
            "token_type": "",
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

    @staticmethod
    def _write_credits_locked(target: Dict, credits: Dict) -> None:
        """把余额写进 token 行。字段集必须完整——credits_total / credits_used /
        credits_error 是后台余额展示和错误态依赖的，漏一个后台就显示不出来。"""
        target["credits_total"] = credits.get("total")
        target["credits_used"] = credits.get("used")
        target["credits_available"] = credits.get("available")
        target["credits_available_until"] = credits.get("available_until")
        target["credits_updated_at"] = credits.get("updated_at") or int(time.time())
        target["credits_error"] = ""

    def set_credits(self, tid: str, credits: Dict):
        with self._lock:
            for t in self.tokens:
                if t.get("id") != tid:
                    continue
                self._write_credits_locked(t, credits)
                self.save()
                return dict(t)
        return None

    def set_credits_and_maybe_revive(
        self,
        tid: str,
        credits: Dict,
        observed_quota_epoch: Optional[int] = None,
    ):
        """写余额，并在同一把锁内决定要不要把 exhausted 账号放回池子。

        复活条件（缺一不可）：账号确实处于 exhausted、余额 > 0、且本次余额快照
        属于当前配额版本（observed_quota_epoch == 当前 quota_epoch）。

        第三条挡的是乱序：余额请求可能在耗尽事件之前就发出、之后才返回，
        那份「余额还有」的旧快照不能把刚耗尽的账号复活。用版本号而不是时间戳，
        是因为余额的 updated_at 只有秒级精度，同秒发生时分不出先后。

        版本失配的余额仍然落盘（后台要看），但打上 credits_quota_epoch 标记，
        调度侧的余额 fast-path 会据此忽略它。
        """
        with self._lock:
            target = next((t for t in self.tokens if t.get("id") == tid), None)
            if target is None:
                return None

            key = self._account_key(target)
            current_epoch = self._quota_epochs.get(key, 0)
            snapshot_epoch = self._quota_epoch_value(observed_quota_epoch)

            self._write_credits_locked(target, credits)
            # 注意不能用 `or` 兜底：0 是合法版本（账号从未耗尽过）。
            target["credits_quota_epoch"] = snapshot_epoch

            available = self._credits_available_value(target)
            if (
                available is not None
                and available > 0
                and snapshot_epoch is not None
                and snapshot_epoch == current_epoch
            ):
                for t in self.tokens:
                    if self._account_key(t) == key and t.get("status") == "exhausted":
                        t["status"] = "active"
                        t["fails"] = 0
            self.save()
            return dict(target)

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

    # 余额刷新要覆盖的状态：
    #   active/error —— 常规刷新，error 号还指望余额查询触发 auth 复活
    #   exhausted    —— 唯一的复活触发器。cookie 刷新不再复活配额耗尽的号，
    #                   只有「余额回来了」才放回池子，所以必须持续查它们的余额。
    # 注意：这个集合只用于余额刷新，绝不能拿去放宽 list_active_ids——
    # 那会把 exhausted 账号重新送进生成调度池。
    CREDIT_REFRESH_STATUSES = frozenset({"active", "error", "exhausted"})

    def list_credit_refresh_ids(self) -> List[str]:
        """批量余额刷新的目标 token id，按账号去重（同账号查一行就够）。"""
        with self._lock:
            by_account: Dict[str, List[Dict]] = {}
            loose: List[str] = []
            for t in self.tokens:
                if t.get("status") not in self.CREDIT_REFRESH_STATUSES:
                    continue
                tid = str(t.get("id") or "")
                if not tid:
                    continue
                key = self._account_key(t)
                if not key:
                    loose.append(tid)
                    continue
                by_account.setdefault(key, []).append(t)

            ids: List[str] = []
            for _key, rows in by_account.items():
                # 同账号只查一行，优先自动刷新行：手动导入行的 token 往往早已过期，
                # 拿它查余额只会得到 401——既查不到余额，还会把那行标成 invalid。
                chosen = next((r for r in rows if r.get("auto_refresh")), rows[0])
                ids.append(str(chosen.get("id") or ""))
            ids.extend(loose)
            return [i for i in ids if i]

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

    # 下面三个解析器只在选号路径上被调用，而选号是持锁的热路径：
    # 任何一次 float()/int() 抛异常都会把整个选号打挂，所以脏数据一律降级为
    # 「没有可用缓存」而不是异常。NaN/Infinity 同样按无效处理。
    @staticmethod
    def _quota_epoch_value(value) -> Optional[int]:
        """配额版本号；缺失或脏数据返回 None（注意 0 是合法版本，不能当空）。"""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _credits_available_value(row: Dict) -> Optional[float]:
        """可用余额；缺失或脏数据（含 NaN/Inf）返回 None = 无从判断。"""
        raw = row.get("credits_available")
        if raw is None or raw == "":
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _credits_updated_at(row: Dict) -> float:
        """余额快照时间；脏数据一律当作 0（= 无缓存）。"""
        try:
            parsed = float(row.get("credits_updated_at") or 0)
        except (TypeError, ValueError):
            return 0.0
        return parsed if math.isfinite(parsed) else 0.0

    @staticmethod
    def _parse_credits_until(value) -> Optional[float]:
        """availableUntil → epoch 秒。

        这个字段是 Adobe 的 availableUntil 原样透传，通常是 ISO 日期字符串
        （前端直接拿去 new Date()），直接 float() 会抛 ValueError。
        解析不出来返回 None，由调用方当作「无法判断周期」放行。
        """
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            pass
        try:
            parsed_date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            timestamp = parsed_date.timestamp()
            return timestamp if math.isfinite(timestamp) else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _cooldown_seconds() -> float:
        from core.config_mgr import config_manager

        raw = config_manager.get("rate_limit_cooldown_seconds", 60)
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return 60.0
        return seconds if seconds >= 0 else 60.0

    @staticmethod
    def _gate_config():
        """读并发闸门配置。返回 (enabled, max_inflight_per_account, queue_size, queue_timeout)。"""
        from core.config_mgr import config_manager

        def _int(key, default):
            try:
                return int(float(str(config_manager.get(key, default)).strip()))
            except (TypeError, ValueError):
                return int(default)

        enabled = bool(config_manager.get("concurrency_gate_enabled", True))
        max_inflight = _int("max_inflight_per_account", 1)
        if max_inflight < 1:
            max_inflight = 1
        queue_size = _int("account_queue_size", 100)
        if queue_size < 0:
            queue_size = 0
        timeout = _int("account_queue_timeout_seconds", 25)
        if timeout < 0:
            timeout = 0
        return enabled, max_inflight, queue_size, timeout

    def _max_inflight_per_account(self) -> int:
        _enabled, max_inflight, _q, _t = self._gate_config()
        # 闸门关时不限并发：用一个很大的值让 in-flight 过滤形同虚设
        return max_inflight if _enabled else 1_000_000

    def _is_busy(self, t: Dict, max_inflight: int) -> bool:
        return self._inflight.get(self._account_key(t), 0) >= max_inflight

    # 余额缓存超过这个岁数就不再用于跳过账号——宁可多打一次 403，
    # 也不能让一份陈旧的「余额为 0」把账号永久挡在池外。
    CREDITS_FASTPATH_TTL = 30 * 60

    def _account_known_zero_credits(self, rows: List[Dict]) -> bool:
        """这个账号是否「确定没额度」，可以在选号阶段直接跳过。

        只是 fast-path：省掉对已知空号的整次多图上传。任何不确定（没缓存、
        缓存过期、额度周期已翻转、快照属于旧配额版本、数据脏）一律返回 False 放行，
        由真实的 403 分类做最终裁决。

        必须无锁：本方法在 acquire_lease 的 _gate_cond 持锁段内被调用，
        而 _gate_cond 和 self._lock 是同一把不可重入锁。
        """
        now = time.time()
        # 只采信属于当前配额版本的快照：耗尽事件之前发出、之后才返回的余额
        # 反映的是旧状态，既不能复活账号，也不能拿来裁决它。
        current_epoch = max(
            (self._quota_epoch_value(row.get("quota_epoch")) or 0 for row in rows),
            default=0,
        )
        current = [
            row
            for row in rows
            if self._quota_epoch_value(row.get("credits_quota_epoch")) == current_epoch
        ]
        if not current:
            return False

        freshest = max(current, key=self._credits_updated_at)
        # 上一次余额刷新失败时 set_credits_error 只写错误、保留旧余额，
        # 却把 credits_updated_at 刷新了——那份陈旧的「零余额」会重新显得新鲜，
        # 一直挡住可能早就恢复额度的账号。带错误标记的快照一律不采信。
        if str(freshest.get("credits_error") or "").strip():
            return False
        updated_at = self._credits_updated_at(freshest)
        if updated_at <= 0 or now - updated_at > self.CREDITS_FASTPATH_TTL:
            return False
        until = self._parse_credits_until(freshest.get("credits_available_until"))
        if until is not None and until <= now:
            return False
        available = self._credits_available_value(freshest)
        return available is not None and available <= 0

    def _filter_zero_credit_accounts(self, active: List[Dict]) -> List[Dict]:
        """按账号整组过滤掉已知零余额的号。

        按账号而不是按行：同账号两行 token 的余额缓存不一定同步，
        按行过滤时「一行是 0、另一行没缓存」的账号会从后一行漏过去。
        全被滤空时返回原列表——fast-path 不该把池子清空。
        """
        if not active:
            return active
        by_account: Dict[str, List[Dict]] = {}
        for t in active:
            by_account.setdefault(self._account_key(t), []).append(t)
        kept = [
            t
            for _key, rows in by_account.items()
            if not self._account_known_zero_credits(rows)
            for t in rows
        ]
        return kept or active

    def _ready_pool_locked(self, active: List[Dict]) -> List[Dict]:
        """滤掉还在限流冷却、以及正在被占满的账号。

        全部都在冷却/占满时不能直接返回空——那会让整个池子暂时不可用；此时退而求其次，
        交出最早解冻的那个，由上层的重试/报错去处理。
        （这是给不排队的 get_available 用的兜底路径；排队逻辑在 acquire_lease 里。）
        """
        now = time.time()
        max_inflight = self._max_inflight_per_account()
        ready = [
            t
            for t in active
            if float(t.get("error_until") or 0) <= now and not self._is_busy(t, max_inflight)
        ]
        if ready:
            return ready
        return [min(active, key=lambda t: float(t.get("error_until") or 0))]

    def _stamp_used_by_key_locked(self, key: str) -> None:
        now = time.time()
        for t in self.tokens:
            if self._account_key(t) == key:
                t["last_used_at"] = now
        self.save()

    def _mark_used_locked(self, chosen: Dict) -> None:
        """记账号的最近使用时间，同账号的所有行一起更新。

        在「选中」时就打点而不是等请求结束，是为了让正在飞行中的账号立刻显得「刚用过」，
        并发请求才不会同时落到同一个账号上。
        """
        self._stamp_used_by_key_locked(self._account_key(chosen))

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
        # 在 _ready_pool_locked 之前过滤：它在全池不可用时会兜底交回一行，
        # 放到之后过滤的话零余额账号会被那条兜底重新塞回来。
        active = self._filter_zero_credit_accounts(active)

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
            active = self._filter_zero_credit_accounts(active)
            mode = str(strategy or "round_robin").strip().lower()
            chosen = self._choose_locked(
                self._ready_pool_locked(active), mode, cursor_key=aid
            )
            self._mark_used_locked(chosen)
            return chosen["value"]

    def _universe_locked(
        self, token_type: Optional[str], account_id: str, exclude_accounts
    ) -> List[Dict]:
        """该请求「理论上可用」的账号（状态可用、类型匹配、未被本请求试过）。
        不看在飞/冷却——那是「暂时不可用、将来会恢复」，用于区分「排队等」还是「彻底没号」。
        """
        active = [t for t in self.tokens if t.get("status") in {"active", "error"}]
        if account_id:
            active = [t for t in active if self._account_key(t) == account_id]
        elif token_type == "leonardo":
            active = [t for t in active if t.get("type") == "leonardo"]
        elif token_type == "adobe":
            active = [t for t in active if t.get("type") != "leonardo"]
        if exclude_accounts:
            active = [t for t in active if self._account_key(t) not in exclude_accounts]
        return self._filter_zero_credit_accounts(active)

    def acquire_lease(
        self,
        *,
        strategy: str = "round_robin",
        token_type: Optional[str] = "adobe",
        account_id: Optional[str] = None,
        exclude_accounts=frozenset(),
        deadline: Optional[float] = None,
    ):
        """占用一个【空闲(非在飞)且非冷却】的账号并返回 (TokenLease, LEASE_OK)。

        没有空闲账号时按队列排队等待，直到有账号释放/解冻、或队列超时/请求 deadline 到、
        或排队人数已满。deadline 用 time.monotonic() 计时（与调用方一致）。

        返回 (TokenLease, LEASE_OK) 或 (None, 原因)。用完必须调用 release_lease。
        """
        enabled, max_inflight, queue_size, q_timeout = self._gate_config()
        aid = str(account_id or "").strip()
        mode = str(strategy or "round_robin").strip().lower()
        started = time.monotonic()

        with self._gate_cond:
            while True:
                now = time.time()
                universe = self._universe_locked(token_type, aid, exclude_accounts)
                if not universe:
                    return None, LEASE_NO_TOKEN

                free = [
                    t
                    for t in universe
                    if float(t.get("error_until") or 0) <= now
                    and self._inflight.get(self._account_key(t), 0) < max_inflight
                ]
                if free:
                    chosen = self._choose_locked(free, mode, cursor_key=aid)
                    key = self._account_key(chosen)
                    # 先落盘打点，成功后再记占用：save() 万一抛（磁盘满/文件不可写），
                    # 占用计数不会被永久加上去而漏还，把账号从池里悄悄抹掉。
                    self._stamp_used_by_key_locked(key)
                    if enabled:
                        self._inflight[key] = self._inflight.get(key, 0) + 1
                    return (
                        TokenLease(chosen.get("value"), key, chosen.get("id"), leased=enabled),
                        LEASE_OK,
                    )

                # 没有空闲账号了
                if not enabled:
                    # 闸门关：不排队，退回旧兜底（返回最早解冻的，即便在飞/冷却）
                    chosen = self._choose_locked(
                        self._ready_pool_locked(universe), mode, cursor_key=aid
                    )
                    key = self._account_key(chosen)
                    self._stamp_used_by_key_locked(key)
                    return TokenLease(chosen.get("value"), key, chosen.get("id"), leased=False), LEASE_OK

                # 闸门开：排队等待。先算各种「等待上限」
                q_left = (q_timeout - (time.monotonic() - started)) if q_timeout > 0 else None
                d_left = (deadline - time.monotonic()) if deadline is not None else None
                if (q_left is not None and q_left <= 0) or (d_left is not None and d_left <= 0):
                    return None, LEASE_TIMEOUT
                if self._waiters >= queue_size:
                    return None, LEASE_QUEUE_FULL

                # 冷却中但没被占满的账号：等到最早解冻时再看一眼
                thaws = [
                    float(t.get("error_until") or 0) - now
                    for t in universe
                    if float(t.get("error_until") or 0) > now
                    and self._inflight.get(self._account_key(t), 0) < max_inflight
                ]
                next_thaw = min(thaws) if thaws else None
                # 在飞账号会通过 release 的 notify 唤醒；没有任何定时信号时用 1s 轮询兜底
                candidates = [w for w in (q_left, d_left, next_thaw) if w is not None and w > 0]
                wait_for = min(candidates) if candidates else 1.0
                wait_for = max(0.01, min(wait_for, 5.0))

                self._waiters += 1
                try:
                    self._gate_cond.wait(timeout=wait_for)
                finally:
                    self._waiters -= 1
                # 醒来后重新走一圈判断

    def release_lease(self, lease) -> None:
        """归还账号占用，唤醒一个排队者。对空租约/None 是安全的空操作。"""
        if lease is None or not getattr(lease, "leased", False):
            return
        key = getattr(lease, "account_key", "") or ""
        with self._gate_cond:
            cur = self._inflight.get(key, 0)
            if cur <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = cur - 1
            self._gate_cond.notify_all()

    def inflight_snapshot(self) -> Dict[str, int]:
        """当前各账号在飞数（调试/监控用）。"""
        with self._lock:
            return dict(self._inflight)

    def gate_status(self) -> Dict:
        """并发闸门 / 排队 的实时快照，供后台页面展示。"""
        enabled, max_inflight, queue_size, q_timeout = self._gate_config()
        now = time.time()
        with self._lock:

            def summarize(is_leo: bool) -> Dict:
                rows = [
                    t
                    for t in self.tokens
                    if t.get("status") in {"active", "error"}
                    and ((t.get("type") == "leonardo") == is_leo)
                ]
                # 按账号聚合（同账号多行只算一个）
                by_acct: Dict[str, Dict] = {}
                for t in rows:
                    by_acct.setdefault(self._account_key(t), t)
                keys = list(by_acct.keys())
                cooling = sum(
                    1 for k in keys if float(by_acct[k].get("error_until") or 0) > now
                )
                inflight_total = sum(self._inflight.get(k, 0) for k in keys)
                busy = sum(
                    1 for k in keys if self._inflight.get(k, 0) >= max_inflight
                )
                ready = sum(
                    1
                    for k in keys
                    if float(by_acct[k].get("error_until") or 0) <= now
                    and self._inflight.get(k, 0) < max_inflight
                )
                busy_accounts = sorted(
                    (
                        {
                            "email": by_acct[k].get("refresh_profile_email") or "",
                            "inflight": self._inflight.get(k, 0),
                        }
                        for k in keys
                        if self._inflight.get(k, 0) > 0
                    ),
                    key=lambda x: -x["inflight"],
                )
                return {
                    "accounts": len(keys),
                    "ready": ready,
                    "cooling": cooling,
                    "busy": busy,
                    "inflight_total": inflight_total,
                    "capacity": len(keys) * max_inflight,
                    "busy_accounts": busy_accounts,
                }

            return {
                "gate_enabled": enabled,
                "max_inflight_per_account": max_inflight,
                "queue_size": queue_size,
                "queue_timeout_seconds": q_timeout,
                "waiters": self._waiters,
                "adobe": summarize(False),
                "leonardo": summarize(True),
            }

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

    def _rebuild_quota_epochs_locked(self) -> None:
        """从落盘的 quota_epoch 重建内存版本号。进程重启后继续单调递增。"""
        epochs: Dict[str, int] = {}
        for t in self.tokens:
            if not isinstance(t, dict):
                continue
            epoch = self._quota_epoch_value(t.get("quota_epoch"))
            if epoch is None:
                continue
            key = self._account_key(t)
            if epoch > epochs.get(key, 0):
                epochs[key] = epoch
        self._quota_epochs = epochs

    def _find_account_key_locked(self, value: str) -> str:
        target = self._find_by_value_locked(value)
        return self._account_key(target) if target is not None else ""

    def _find_by_value_locked(self, value: str) -> Optional[Dict]:
        wanted = str(value or "").strip()
        if not wanted:
            return None
        for t in self.tokens:
            if str(t.get("value") or "").strip() == wanted:
                return t
        return None

    def account_key_for_id(self, tid: str, fallback_value: str = "") -> str:
        """按稳定 token id 读账号键；id 查不到时回落到 token 值反查。

        调用方拿到空串意味着「认不出这是哪个账号」，必须当作出池失败记日志，
        不能静默跳过——那正是死号留在池里被反复撞的老问题。
        """
        key = str(tid or "").strip()
        with self._lock:
            target = None
            if key:
                target = next((t for t in self.tokens if str(t.get("id") or "") == key), None)
            if target is None:
                target = self._find_by_value_locked(fallback_value)
            return self._account_key(target) if target is not None else ""

    def quota_epoch(self, account_key: str) -> int:
        """读账号当前的配额事件版本号。余额请求发起前调用，落盘时用它比对。"""
        with self._lock:
            return self._quota_epochs.get(str(account_key or "").strip(), 0)

    def report_account_exhausted(self, account_key: str) -> bool:
        """整个账号按配额出池。

        必须按账号而不是按 token 值：同一账号可能有多行 token（手动导入一行、
        自动刷新一行），只标一行的话另一行照样会被选中。调用方传 lease.account_key
        或 account_key_for_id() 的结果，不要用 token 值反查——请求在飞期间
        cookie 刷新可能已经把 token 值换掉，反查会落空。
        """
        key = str(account_key or "").strip()
        if not key:
            return False
        with self._lock:
            epoch = self._quota_epochs.get(key, 0) + 1
            self._quota_epochs[key] = epoch
            touched = False
            for t in self.tokens:
                if self._account_key(t) == key:
                    t["status"] = "exhausted"
                    t["error_until"] = 0
                    t["quota_epoch"] = epoch
                    touched = True
            if touched:
                self.save()
            return touched

    def report_account_invalid(self, account_key: str) -> bool:
        """整个账号标记为凭证失效。

        仅供「确认整个账号都不可用」的场景（如账号被封）调用；日常的 401
        请走 token 值级的 report_invalid，见那里的说明。

        已经 exhausted 的行保持原状：配额耗尽是更强的终态，被降级成 invalid
        会让 cookie 刷新的守卫失效，把死号洗回池子。
        """
        key = str(account_key or "").strip()
        if not key:
            return False
        with self._lock:
            touched = False
            for t in self.tokens:
                if self._account_key(t) != key:
                    continue
                if t.get("status") == "exhausted":
                    continue
                t["status"] = "invalid"
                t["error_until"] = 0
                touched = True
            if touched:
                self.save()
            return touched

    def report_exhausted(self, value: str):
        """兼容包装：按 token 值反查账号后转调账号级接口。

        锁不可重入，所以先在锁内解析账号键、退出锁再调用。
        新代码应直接用 report_account_exhausted()。
        """
        with self._lock:
            key = self._find_account_key_locked(value)
        if not key:
            logger.warning("report_exhausted: token value not found, account not retired")
            return
        self.report_account_exhausted(key)

    def report_invalid(self, value: str):
        """凭证失效：只标这一行，不牵连同账号的其他行。

        粒度和配额出池是**故意不同**的：配额是账号级的额度用完了，
        而 401 只说明「这个 access token 不能用了」——同账号的手动导入行和
        自动刷新行持有的是两个不同的 token，一个过期不代表另一个也过期。
        按账号连坐的话，一行陈旧 token 会把持有新 token 的兄弟行一起打死，
        整个账号要等到下一轮 cookie 刷新（默认 15h）才可能恢复。

        另外不覆盖 exhausted：配额耗尽是更强的终态，被降级成 invalid 后
        cookie 刷新的守卫（只认 exhausted）就会失效，死号会被洗回调度池。
        """
        wanted = str(value or "").strip()
        if not wanted:
            return
        with self._lock:
            touched = False
            for t in self.tokens:
                if str(t.get("value") or "").strip() != wanted:
                    continue
                if t.get("status") == "exhausted":
                    continue
                t["status"] = "invalid"
                t["error_until"] = 0
                touched = True
            if touched:
                self.save()
            else:
                logger.warning(
                    "report_invalid: no eligible row for this token value"
                )

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
