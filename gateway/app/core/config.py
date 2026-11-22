"""Application settings, loaded from environment variables (and an optional .env file)."""

import math
from pathlib import Path
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RateLimitAlgorithm = Literal["token_bucket", "sliding_window"]
LimitScope = Literal["ip", "auth_ip", "user", "api_key"]


@dataclass(frozen=True, slots=True)
class LimitConfig:
    """Parameters for one rate-limit scope. Both algorithms' parameters are always set;
    the limiter reads the ones that belong to the configured algorithm."""

    bucket_capacity: int
    bucket_refill_rate: float  # tokens per second
    window_limit: int
    window_seconds: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str
    redis_url: str

    # --- JWT ---
    jwt_secret: SecretStr
    jwt_expire_minutes: int = Field(default=30, ge=1)

    # --- Rate limiting ---
    rate_limit_algorithm: RateLimitAlgorithm = "token_bucket"
    # Base limits. These apply to each API key.
    token_bucket_capacity: int = Field(default=100, ge=1)
    token_bucket_refill_rate: float = Field(default=10.0, gt=0)
    sliding_window_limit: int = Field(default=100, ge=1)
    sliding_window_seconds: int = Field(default=60, ge=1)
    # The per-user limit is the base limit times this factor, so a user holding several
    # keys is capped in aggregate while each key keeps its own, smaller limit.
    user_limit_multiplier: float = Field(default=5.0, gt=0)
    # Per-IP limit for gateway and management traffic.
    ip_rate_limit_requests: int = Field(default=200, ge=1)
    ip_rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Stricter per-IP limit for /auth/register and /auth/login (brute-force protection).
    auth_rate_limit_attempts: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Number of reverse proxies in front of the gateway whose X-Forwarded-For entry we trust.
    # 0 (default) means: use the direct peer address and ignore X-Forwarded-For entirely.
    trusted_proxy_count: int = Field(default=0, ge=0)

    # --- Upstream services ---
    product_service_url: str = "http://products:8001"
    order_service_url: str = "http://orders:8002"
    upstream_timeout_seconds: float = Field(default=5.0, gt=0)
    upstream_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    product_service_timeout_seconds: float | None = Field(default=None, gt=0)
    order_service_timeout_seconds: float | None = Field(default=None, gt=0)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1)

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters (try: openssl rand -hex 32)"
            )
        if self.environment == "production" and secret.startswith("replace-with"):
            raise ValueError("JWT_SECRET still has its placeholder value")
        return self

    def limit_for(self, scope: LimitScope) -> LimitConfig:
        if scope == "api_key":
            return LimitConfig(
                self.token_bucket_capacity,
                self.token_bucket_refill_rate,
                self.sliding_window_limit,
                self.sliding_window_seconds,
            )
        if scope == "user":
            m = self.user_limit_multiplier
            return LimitConfig(
                math.ceil(self.token_bucket_capacity * m),
                self.token_bucket_refill_rate * m,
                math.ceil(self.sliding_window_limit * m),
                self.sliding_window_seconds,
            )
        if scope == "ip":
            n, window = self.ip_rate_limit_requests, self.ip_rate_limit_window_seconds
        else:  # auth_ip
            n, window = self.auth_rate_limit_attempts, self.auth_rate_limit_window_seconds
        return LimitConfig(n, n / window, n, window)
