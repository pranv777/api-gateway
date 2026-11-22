"""Redis-backed rate limiting: token bucket and sliding window.

Both algorithms run as Lua scripts, so each check is one atomic Redis operation. The scripts
read the clock from Redis (`TIME`) rather than from the gateway, so several gateway instances
agree on time and clock skew between them does not matter.
"""

import math
import uuid
from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis

from app.core.config import LimitScope, Settings

# KEYS[1] = bucket key
# ARGV    = capacity, refill_rate (tokens/s), cost
# Returns {allowed, remaining_tokens, retry_after_ms, reset_ms}
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil((cost - tokens) / refill * 1000)
end

redis.call('HSET', key, 'tokens', string.format('%.6f', tokens), 'ts', string.format('%.6f', now))
local reset_ms = math.ceil((capacity - tokens) / refill * 1000)
-- Once the bucket is full again its state equals a fresh bucket, so the key can expire.
redis.call('PEXPIRE', key, reset_ms + 1000)
return {allowed, math.floor(tokens), retry_ms, reset_ms}
"""

# KEYS[1] = sorted-set key (member = unique request id, score = timestamp in microseconds)
# ARGV    = limit, window_seconds, unique_member
# Returns {allowed, remaining, retry_after_ms, reset_ms}
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_us = tonumber(ARGV[2]) * 1000000
local member = ARGV[3]

local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_us - window_us)
local count = redis.call('ZCARD', key)

local allowed = 0
if count < limit then
  redis.call('ZADD', key, now_us, member)
  count = count + 1
  allowed = 1
end

local reset_ms = 0
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if oldest[2] then
  reset_ms = math.ceil((tonumber(oldest[2]) + window_us - now_us) / 1000)
  if reset_ms < 0 then reset_ms = 0 end
end
local retry_ms = 0
if allowed == 0 then retry_ms = reset_ms end

redis.call('PEXPIRE', key, math.ceil(window_us / 1000) + 1000)
return {allowed, limit - count, retry_ms, reset_ms}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # whole seconds to wait before retrying (meaningful when denied)
    reset_after: int  # whole seconds until the limit is fully replenished

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after))
        return headers


class RateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._settings = settings
        self._token_bucket = redis.register_script(TOKEN_BUCKET_LUA)
        self._sliding_window = redis.register_script(SLIDING_WINDOW_LUA)

    async def check(self, scope: LimitScope, identifier: str) -> RateLimitResult:
        """Consume one unit for `identifier` in `scope` (if available) and report the outcome."""
        algorithm = self._settings.rate_limit_algorithm
        cfg = self._settings.limit_for(scope)
        # The algorithm is part of the key: the two use different Redis types, so sharing a
        # key would raise WRONGTYPE after the algorithm setting is changed.
        key = f"rl:{algorithm}:{scope}:{identifier}"

        if algorithm == "token_bucket":
            raw = await self._token_bucket(
                keys=[key], args=[cfg.bucket_capacity, cfg.bucket_refill_rate, 1]
            )
            limit = cfg.bucket_capacity
        else:
            raw = await self._sliding_window(
                keys=[key], args=[cfg.window_limit, cfg.window_seconds, uuid.uuid4().hex]
            )
            limit = cfg.window_limit

        allowed, remaining, retry_ms, reset_ms = (int(v) for v in raw)
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=max(0, remaining),
            retry_after=math.ceil(retry_ms / 1000),
            reset_after=math.ceil(reset_ms / 1000),
        )

    async def check_all(self, checks: list[tuple[LimitScope, str]]) -> RateLimitResult:
        """Enforce every limit in `checks`, in order. Stops at the first denial and returns it.
        If all pass, returns the result with the least remaining headroom.

        Limits checked before a denial have already been charged for this request.
        """
        tightest: RateLimitResult | None = None
        for scope, identifier in checks:
            result = await self.check(scope, identifier)
            if not result.allowed:
                return result
            if tightest is None or result.remaining < tightest.remaining:
                tightest = result
        if tightest is None:
            raise ValueError("check_all needs at least one check")
        return tightest


def get_client_ip(request: Request, trusted_proxy_count: int = 0) -> str:
    """Identify the client. By default this is the direct peer address; X-Forwarded-For is
    ignored because any client can send it. With N trusted proxies in front of the gateway,
    the client is the Nth entry from the right (earlier entries are client-controlled)."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_count <= 0:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if len(parts) < trusted_proxy_count:
        return peer
    return parts[-trusted_proxy_count]
