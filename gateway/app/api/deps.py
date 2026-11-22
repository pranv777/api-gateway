"""Shared FastAPI dependencies: authentication and rate-limit enforcement."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LimitScope
from app.core.rate_limit import get_client_ip
from app.core.security import (
    TokenExpiredError,
    TokenInvalidError,
    decode_access_token,
    hash_api_key,
)
from app.db.models import ApiKey, User
from app.db.session import get_session

bearer_scheme = HTTPBearer(auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

# last_used_at is refreshed at most this often per key, to avoid a write on every request.
LAST_USED_UPDATE_INTERVAL = timedelta(seconds=60)
MAX_API_KEY_LENGTH = 200


@dataclass(frozen=True, slots=True)
class Principal:
    """The identity behind an authenticated gateway request."""

    user_id: uuid.UUID
    api_key_id: uuid.UUID


def _unauthorized(detail: str, scheme: str = "Bearer") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": scheme})


# ------------------------------------------------------------------ JWT
async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        user_id = uuid.UUID(
            decode_access_token(
                credentials.credentials, request.app.state.settings.jwt_secret.get_secret_value()
            )
        )
    except TokenExpiredError:
        raise _unauthorized("Token has expired") from None
    except (TokenInvalidError, ValueError):
        raise _unauthorized("Invalid token") from None

    user = await session.get(User, user_id)
    if user is None:
        raise _unauthorized("Invalid token")
    return user


# -------------------------------------------------------------- API keys
async def get_principal(
    request: Request, api_key: str | None = Depends(api_key_scheme)
) -> Principal:
    if not api_key:
        raise _unauthorized("Missing API key", scheme="ApiKey")
    if len(api_key) > MAX_API_KEY_LENGTH:
        raise _unauthorized("Invalid or revoked API key", scheme="ApiKey")

    # A short-lived session (not the request-scoped one): the DB connection is returned to
    # the pool before the gateway starts waiting on the upstream service.
    async with request.app.state.sessionmaker() as session:
        row = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.key_hash == hash_api_key(api_key), ApiKey.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _unauthorized("Invalid or revoked API key", scheme="ApiKey")

        now = datetime.now(UTC)
        if row.last_used_at is None or now - row.last_used_at > LAST_USED_UPDATE_INTERVAL:
            await session.execute(
                update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now)
            )
            await session.commit()
        return Principal(user_id=row.user_id, api_key_id=row.id)


# --------------------------------------------------------- IP rate limits
async def _enforce_ip_limit(request: Request, scope: LimitScope) -> None:
    ip = get_client_ip(request, request.app.state.settings.trusted_proxy_count)
    result = await request.app.state.rate_limiter.check(scope, ip)
    request.state.ip_rate_limit = result
    if not result.allowed:
        raise HTTPException(status_code=429, detail="Too many requests", headers=result.headers())


async def enforce_ip_limit(request: Request) -> None:
    """General per-IP limit. Runs before authentication so unauthenticated floods are cheap."""
    await _enforce_ip_limit(request, "ip")


async def enforce_auth_ip_limit(request: Request) -> None:
    """Stricter per-IP limit for register/login, to slow down brute-force attempts."""
    await _enforce_ip_limit(request, "auth_ip")
