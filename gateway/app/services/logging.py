"""Persisting request metadata to PostgreSQL."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RequestLog

logger = logging.getLogger("app.request_log")


async def record_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    request_id: str,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID | None,
    method: str,
    endpoint: str,
    status_code: int,
    duration_ms: float,
    is_rate_limited: bool,
) -> None:
    """Store one request-log row. Runs as a background task after the response is sent, and
    must never raise: losing a log row is better than failing a request that already succeeded.

    Only metadata is stored: no bodies, headers, query strings, keys, passwords or JWTs.
    """
    try:
        async with sessionmaker() as session:
            session.add(
                RequestLog(
                    request_id=request_id,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    method=method,
                    endpoint=endpoint[:512],
                    status_code=status_code,
                    duration_ms=max(0.0, duration_ms),
                    is_rate_limited=is_rate_limited,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("failed to persist request log", extra={"request_id": request_id})
        return
    logger.info(
        "request",
        extra={
            "request_id": request_id,
            "user_id": str(user_id),
            "api_key_id": str(api_key_id) if api_key_id else None,
            "method": method,
            "endpoint": endpoint,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
            "is_rate_limited": is_rate_limited,
        },
    )
