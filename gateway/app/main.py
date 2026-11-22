"""Application factory.

Run with:  uvicorn --factory app.main:create_app
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from app.api import analytics, api_keys, auth, proxy
from app.core.config import Settings
from app.core.cookies import RejectAllCookiesPolicy
from app.core.logs import configure_logging
from app.core.middleware import RequestIdMiddleware
from app.core.rate_limit import RateLimiter
from app.db.session import create_engine, create_sessionmaker
from app.services.routing import build_routes

logger = logging.getLogger("app")


def create_app(
    settings: Settings | None = None, *, http_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """`http_transport` lets tests replace real network calls to the upstream services."""
    settings = settings or Settings()  # pyright: ignore[reportCallIssue]
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        redis = Redis.from_url(settings.redis_url)
        # One shared client = one connection pool, reused across all forwarded requests.
        http_client = httpx.AsyncClient(
            transport=http_transport,
            cookies=CookieJar(policy=RejectAllCookiesPolicy()),
            timeout=httpx.Timeout(
                settings.upstream_timeout_seconds,
                connect=settings.upstream_connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
            ),
            follow_redirects=False,
        )
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = redis
        app.state.rate_limiter = RateLimiter(redis, settings)
        app.state.http_client = http_client
        app.state.routes = build_routes(settings)
        try:
            yield
        finally:
            await http_client.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="API Gateway",
        version="1.0.0",
        description="Authenticated, rate-limited gateway for the product and order services.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)

    app.include_router(auth.router)
    app.include_router(api_keys.router)
    app.include_router(analytics.router)
    app.include_router(proxy.router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready(request: Request) -> JSONResponse:
        """Readiness: PostgreSQL and Redis are reachable."""
        try:
            async with request.app.state.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            await request.app.state.redis.ping()
        except Exception:
            logger.exception("readiness check failed")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.exception_handler(RedisError)
    async def redis_error_handler(request: Request, exc: RedisError) -> JSONResponse:
        # Fail closed: without Redis the limits cannot be enforced, so requests are refused.
        logger.error("redis error", extra={"error": type(exc).__name__})
        return JSONResponse({"detail": "Rate limiter unavailable"}, status_code=503)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex

        logger.exception(
            "unhandled error",
            extra={
                "path": request.url.path,
                "request_id": request_id,
            },
        )

        return JSONResponse(
            {"detail": "Internal server error"},
            status_code=500,
            headers={"X-Request-ID": request_id},
        )

    return app
