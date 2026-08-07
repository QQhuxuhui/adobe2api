import math
from typing import Annotated, Any, List, Literal, Optional

from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    aspect_ratio: str = Field(default="auto")
    output_resolution: str = Field(default="2K")
    model: Optional[str] = None


class TokenAddRequest(BaseModel):
    token: str


class TokenBatchAddRequest(BaseModel):
    tokens: List[str]


class LeonardoTokenUpsertRequest(BaseModel):
    token: str = Field(min_length=1)
    label: Optional[str] = Field(default=None, max_length=200)


class LeonardoCookieUploadRequest(BaseModel):
    cookie: str = Field(min_length=1, max_length=32768)
    # 轮换回写时带上账号稳定 id，只就地更新该账号那条（指纹会变，按 id 才可靠）
    cookie_id: Optional[str] = None


class LeonardoLoginReportRequest(BaseModel):
    id: str
    credential_rev: int
    status: Literal["ok", "login_required"]
    last_error_kind: Optional[Literal["password", "captcha", "proxy", "upstream"]] = None
    balance: Optional[float] = None

    @field_validator("credential_rev")
    @classmethod
    def _rev_nonneg(cls, v):
        if v < 0:
            raise ValueError("credential_rev must be >= 0")
        return v

    @field_validator("balance")
    @classmethod
    def _bal(cls, v):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise ValueError("balance must be finite and >= 0")
        return v

    @model_validator(mode="after")
    def _err_matches_status(self):
        if self.status == "login_required" and not self.last_error_kind:
            raise ValueError("login_required requires last_error_kind")
        if self.status == "ok" and self.last_error_kind:
            raise ValueError("ok must not carry last_error_kind")
        return self


class LeonardoLoginImportRequest(BaseModel):
    # 每行 email:password；限长防止超大 body（超长 → 422）
    text: Annotated[str, StringConstraints(max_length=200_000)]


class ExportSelectionRequest(BaseModel):
    ids: Optional[List[str]] = None


class TokenCreditsBatchRefreshRequest(BaseModel):
    ids: Optional[List[str]] = None


class ProxyTestRequest(BaseModel):
    proxy: str = ""


class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    public_base_url: Optional[str] = None
    proxy: Optional[str] = None
    use_proxy: Optional[bool] = None
    generate_timeout: Optional[int] = None
    gemini_native_deadline_seconds: Optional[StrictInt] = None
    images_edits_deadline_seconds: Optional[StrictInt] = None
    rotation_max_accounts_default: Optional[StrictInt] = None
    rotation_max_accounts_images_edits: Optional[StrictInt] = None
    leonardo_credits_refresh_minutes: Optional[StrictInt] = None
    leonardo_credit_price_cny: Optional[float] = None
    adobe_credit_price_cny: Optional[float] = None
    refresh_interval_hours: Optional[int] = None
    retry_enabled: Optional[bool] = None
    retry_max_attempts: Optional[int] = None
    retry_backoff_seconds: Optional[float] = None
    retry_on_status_codes: Optional[List[int]] = None
    retry_on_error_types: Optional[List[str]] = None
    token_rotation_strategy: Optional[str] = None
    rate_limit_cooldown_seconds: Optional[int] = None
    concurrency_gate_enabled: Optional[bool] = None
    max_inflight_per_account: Optional[int] = None
    account_queue_size: Optional[int] = None
    account_queue_timeout_seconds: Optional[int] = None
    batch_concurrency: Optional[int] = None
    generated_max_size_mb: Optional[int] = None
    generated_prune_size_mb: Optional[int] = None
    gpt_image_quality: Optional[str] = None

    @field_validator(
        "leonardo_credit_price_cny", "adobe_credit_price_cny", mode="before"
    )
    @classmethod
    def _credit_price(cls, value):
        from core.credit_costs import normalize_credit_price

        return normalize_credit_price(value)


class RefreshCookieImportRequest(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportItem(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportRequest(BaseModel):
    items: List[RefreshCookieBatchImportItem]


class RefreshProfileEnabledRequest(BaseModel):
    enabled: bool


class AdminLoginRequest(BaseModel):
    username: str
    password: str
