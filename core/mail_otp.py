import re
import time
from typing import Optional, Tuple
from datetime import datetime, timezone

import requests

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class MailOTPError(Exception):
    """MailOTPReader 统一异常。"""


_OTP_PATTERNS = [
    r"验证码[是为:：]?\s*(\d{6})",                                       # zh: 验证码是100581
    r"(?:verification|security|login|one[-\s]?time)\s+code\s*(?:is|:)?\s*(\d{6})",  # en
    r"kode\s+canva(?:\s+anda)?\s*(?:adalah|:)?\s*(\d{6})",              # id
    r"\bcode\b[^0-9]{0,10}(\d{6})",                                     # generic: code ... 6 digits
    r"\bcanva\b[^0-9]{0,20}?(\d{6})",                                   # generic: canva ... 6 digits
]


def extract_canva_otp(text: str) -> Optional[str]:
    """从邮件主题+正文中提取 Canva 6 位验证码，覆盖中/英/印尼文，找不到返回 None。"""
    if not text:
        return None
    for pattern in _OTP_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def redeem_graph_token(
    client_id: str,
    refresh_token: str,
    *,
    http_post=None,
) -> Tuple[str, str]:
    """用 refresh token 兑换 Graph access token。返回 (access_token, current_refresh_token)。
    MSA refresh token 兑换即轮换：若响应含新 refresh token 返回新的，否则回退旧的。"""
    http_post = http_post or requests.post
    resp = http_post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": GRAPH_SCOPE,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise MailOTPError(f"token redeem failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    access = data.get("access_token")
    if not access:
        raise MailOTPError("token redeem response missing access_token")
    new_refresh = data.get("refresh_token") or refresh_token
    return access, new_refresh


GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"
_MESSAGES_QUERY = (
    "?$top=15&$select=from,subject,bodyPreview,receivedDateTime"
    "&$orderby=receivedDateTime%20desc"
)


def _parse_iso_epoch(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def fetch_latest_canva_otp(
    access_token: str,
    *,
    since_ts: Optional[float] = None,
    http_get=None,
) -> Optional[Tuple[str, float]]:
    """读收件箱，返回最新一封 Canva 验证码邮件的 (otp, received_epoch)；无则 None。"""
    http_get = http_get or requests.get
    resp = http_get(
        GRAPH_MESSAGES + _MESSAGES_QUERY,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise MailOTPError(f"graph messages failed: HTTP {resp.status_code}")
    for msg in resp.json().get("value", []):
        addr = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        if "canva" not in addr:
            continue
        received = _parse_iso_epoch(msg.get("receivedDateTime") or "")
        if since_ts is not None and received is not None and received <= since_ts:
            continue
        text = (msg.get("subject") or "") + "\n" + (msg.get("bodyPreview") or "")
        otp = extract_canva_otp(text)
        if otp:
            return otp, (received or 0.0)
    return None


def get_otp(
    client_id: str,
    refresh_token: str,
    *,
    since_ts: Optional[float] = None,
    on_rotate=None,
    poll_interval: float = 5,
    timeout: float = 120,
    http_post=None,
    http_get=None,
    sleep=time.sleep,
    now=time.time,
) -> Tuple[str, str]:
    """端到端取 OTP：兑换 Graph token（轮换则回调 on_rotate）→ 轮询收件箱直到取到验证码。
    返回 (otp, current_refresh_token)。超时抛 MailOTPError。"""
    access, current_refresh = redeem_graph_token(client_id, refresh_token, http_post=http_post)
    if on_rotate is not None and current_refresh != refresh_token:
        on_rotate(current_refresh)

    deadline = now() + timeout
    while True:
        hit = fetch_latest_canva_otp(access, since_ts=since_ts, http_get=http_get)
        if hit:
            return hit[0], current_refresh
        if now() >= deadline:
            raise MailOTPError("no Canva OTP received within timeout")
        sleep(poll_interval)
