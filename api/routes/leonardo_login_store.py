import json, logging, os, threading, time, uuid
from pathlib import Path

from core.token_mgr import CONFIG_DIR

FAIL_ALERT_THRESHOLD = 3
BALANCE_ALERT_THRESHOLD = 1000
_EMPTY = {"logins": [], "yescaptcha_balance": None, "balance_at": None}

_logger = logging.getLogger("leonardo_login")


class LeonardoLoginStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._corrupt = False

    def _load(self) -> dict:
        if not self._path.exists():
            return json.loads(json.dumps(_EMPTY))
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._corrupt = True
            raise RuntimeError(
                "refusing to use corrupt leonardo_logins.json; repair or move it first"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("logins"), list):
            self._corrupt = True
            raise RuntimeError("leonardo_logins.json has unexpected shape")
        data.setdefault("yescaptcha_balance", None)
        data.setdefault("balance_at", None)
        return data

    def _save(self, data: dict) -> None:
        if self._corrupt:
            raise RuntimeError("refusing to overwrite corrupt leonardo_logins.json")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as h:
                os.chmod(tmp, 0o600)
                h.write(payload)
                h.flush()
                os.fsync(h.fileno())
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def list_for_refresher(self) -> list:
        with self._lock:
            data = self._load()
            return [
                {"id": x["id"], "email": x["email"], "password": x["password"],
                 "credential_rev": int(x.get("credential_rev") or 1)}
                for x in data["logins"]
            ]

    def import_lines(self, raw: str) -> dict:
        added = updated = skipped = 0
        with self._lock:
            data = self._load()
            by_email = {x["email"]: x for x in data["logins"]}
            for line in str(raw or "").splitlines():
                email, sep, password = line.partition(":")
                email = email.strip().lower()
                password = password.rstrip("\r\n")
                if not sep or not email or not password:
                    skipped += 1
                    continue
                now = int(time.time())
                cur = by_email.get(email)
                if cur is None:
                    entry = {
                        "id": uuid.uuid4().hex[:12], "email": email, "password": password,
                        "credential_rev": 1, "status": "pending", "fail_count": 0,
                        "last_error_kind": None, "updated_at": now, "last_attempt_at": None,
                    }
                    data["logins"].append(entry)
                    by_email[email] = entry
                    added += 1
                elif cur["password"] != password:
                    cur["password"] = password
                    cur["credential_rev"] = int(cur.get("credential_rev") or 1) + 1
                    cur["status"] = "pending"
                    cur["fail_count"] = 0
                    cur["last_error_kind"] = None
                    cur["updated_at"] = now
                    updated += 1
                else:
                    skipped += 1
            self._save(data)
            return {"added": added, "updated": updated, "skipped": skipped,
                    "count": len(data["logins"])}

    def remove(self, id: str) -> dict:
        with self._lock:
            data = self._load()
            before = len(data["logins"])
            data["logins"] = [x for x in data["logins"] if x.get("id") != id]
            self._save(data)
            return {"removed": before - len(data["logins"]), "count": len(data["logins"])}

    def status_view(self) -> dict:
        with self._lock:
            data = self._load()
            return {
                "logins": [{k: x.get(k) for k in
                            ("id", "email", "status", "fail_count",
                             "last_error_kind", "updated_at", "last_attempt_at")}
                           for x in data["logins"]],
                "count": len(data["logins"]),
                "yescaptcha_balance": data.get("yescaptcha_balance"),
                "balance_at": data.get("balance_at"),
                "thresholds": {"fail_count": FAIL_ALERT_THRESHOLD,
                               "yescaptcha_balance": BALANCE_ALERT_THRESHOLD},
            }

    def report(self, id, credential_rev, status, last_error_kind=None, balance=None) -> dict:
        with self._lock:
            data = self._load()
            # 余额与 rev 无关，先处理
            if balance is not None:
                old = data.get("yescaptcha_balance")
                if (old is None or old >= BALANCE_ALERT_THRESHOLD) and balance < BALANCE_ALERT_THRESHOLD:
                    _logger.warning("YesCaptcha 余额跌破阈值：%s < %s", balance, BALANCE_ALERT_THRESHOLD)
                elif old is not None and old < BALANCE_ALERT_THRESHOLD and balance >= BALANCE_ALERT_THRESHOLD:
                    _logger.warning("YesCaptcha 余额已恢复：%s", balance)
                data["yescaptcha_balance"] = float(balance)
                data["balance_at"] = int(time.time())
            row = next((x for x in data["logins"] if x.get("id") == id), None)
            if row is None:
                self._save(data)
                return {"updated": False, "reason": "unknown_id"}
            if int(row.get("credential_rev") or 1) != int(credential_rev):
                self._save(data)
                return {"updated": False, "reason": "stale_revision"}
            row["last_attempt_at"] = int(time.time())
            if status == "ok":
                if int(row.get("fail_count") or 0) >= FAIL_ALERT_THRESHOLD:
                    _logger.warning("登录账号 %s 已恢复正常", row["email"])
                row["status"] = "ok"; row["fail_count"] = 0; row["last_error_kind"] = None
            else:  # login_required
                row["fail_count"] = int(row.get("fail_count") or 0) + 1
                row["status"] = "login_required"; row["last_error_kind"] = last_error_kind
                if row["fail_count"] == FAIL_ALERT_THRESHOLD:
                    _logger.warning("登录账号 %s 连续登录失败 %s 次(%s)",
                                    row["email"], row["fail_count"], last_error_kind)
            self._save(data)
            return {"updated": True, "reason": None}

    def _with_lock_append(self, entry: dict) -> None:  # 仅测试并发用
        with self._lock:
            data = self._load()
            data["logins"].append(entry)
            self._save(data)


login_store = LeonardoLoginStore(CONFIG_DIR / "leonardo_logins.json")
