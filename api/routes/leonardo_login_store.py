import json, os, threading, uuid
from pathlib import Path

from core.token_mgr import CONFIG_DIR

FAIL_ALERT_THRESHOLD = 3
BALANCE_ALERT_THRESHOLD = 1000
_EMPTY = {"logins": [], "yescaptcha_balance": None, "balance_at": None}


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

    def remove(self, id: str) -> dict:
        with self._lock:
            data = self._load()
            before = len(data["logins"])
            data["logins"] = [x for x in data["logins"] if x.get("id") != id]
            self._save(data)
            return {"removed": before - len(data["logins"]), "count": len(data["logins"])}

    def _with_lock_append(self, entry: dict) -> None:  # 仅测试并发用
        with self._lock:
            data = self._load()
            data["logins"].append(entry)
            self._save(data)


login_store = LeonardoLoginStore(CONFIG_DIR / "leonardo_logins.json")
