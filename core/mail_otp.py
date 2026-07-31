import re
from typing import Optional

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
