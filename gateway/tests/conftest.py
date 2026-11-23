"""Shared fixtures.

Integration tests need real PostgreSQL and Redis (see README). Point them at disposable
databases with TEST_DATABASE_URL / TEST_REDIS_URL; the test database is dropped and recreated
for every test and the Redis database is flushed. If either service is unreachable the tests
are skipped, unless REQUIRE_INFRA=1 is set (as in the docker-compose `test` service), in which
case they fail.
"""

import asyncio
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from redis.connection import parse_url
from redis.exceptions import AuthenticationError as RedisAuthenticationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test"
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_JWT_SECRET = "test-secret-" + "x" * 40

GATEWAY_DIR = Path(__file__).resolve().parents[1]


def migrate_test_database() -> None:
    config = Config(str(GATEWAY_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(GATEWAY_DIR / "migrations"),
    )
    config.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(config, "head")


@dataclass
class Infra:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis


@pytest_asyncio.fixture
async def infra():
    validate_test_targets(TEST_DATABASE_URL, TEST_REDIS_URL)

    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
    )
    redis = Redis.from_url(
        TEST_REDIS_URL,
        socket_connect_timeout=2,
        socket_timeout=2,
    )

    try:
        try:
            await redis.ping()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        except RedisAuthenticationError:
            # Incorrect credentials should fail, not skip.
            raise

        except (
            OSError,
            RedisConnectionError,
            RedisTimeoutError,
        ) as exc:
            if os.getenv("REQUIRE_INFRA") == "1":
                raise
            pytest.skip(f"Test infrastructure unavailable: {exc}")

        # Includes removal of alembic_version so migrations start fresh.
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))

        # Alembic's environment uses asyncio.run(), so run it in a worker
        # thread rather than inside pytest's running event loop.
        await asyncio.to_thread(migrate_test_database)

        await redis.flushdb()

        try:
            yield Infra(
                engine,
                async_sessionmaker(engine, expire_on_commit=False),
                redis,
            )
        finally:
            await redis.flushdb()

    finally:
        await redis.aclose()
        await engine.dispose()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Settings with limits high enough that they never interfere, unless a test lowers them."""

    def _make(**overrides) -> Settings:
        values: dict[str, Any] = {
            "environment": "test",
            "database_url": TEST_DATABASE_URL,
            "redis_url": TEST_REDIS_URL,
            "jwt_secret": TEST_JWT_SECRET,
            "token_bucket_capacity": 1000,
            "token_bucket_refill_rate": 1000.0,
            "sliding_window_limit": 1000,
            "sliding_window_seconds": 60,
            "user_limit_multiplier": 1.0,
            "ip_rate_limit_requests": 10_000,
            "auth_rate_limit_attempts": 1000,
            "upstream_timeout_seconds": 2.0,
            "product_service_url": "http://products:8001",
            "order_service_url": "http://orders:8002",
        }
        values.update(overrides)
        return Settings(**{"_env_file": None, **values})

    return _make


class FakeUpstream:
    """An httpx MockTransport handler that records requests and returns a configurable response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda req: httpx.Response(
            200, json={"upstream_path": req.url.path}
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


class GatewayClient(httpx.AsyncClient):
    settings: Settings


@pytest_asyncio.fixture
async def make_client(infra, make_settings, upstream):
    """Build a running gateway app plus an HTTP client for it. Can be called repeatedly to
    create several independent gateway instances that share the same PostgreSQL and Redis."""
    stack = AsyncExitStack()

    async def _make(client_host: str = "127.0.0.1", **settings_overrides) -> GatewayClient:
        settings = make_settings(**settings_overrides)
        app = create_app(settings, http_transport=httpx.MockTransport(upstream))
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = httpx.ASGITransport(app=app, client=(client_host, 50000))
        client = GatewayClient(transport=transport, base_url="http://gateway.test")
        await stack.enter_async_context(client)
        client.settings = settings
        return client

    yield _make
    await stack.aclose()


@pytest_asyncio.fixture
async def client(make_client) -> GatewayClient:
    return await make_client()


def validate_test_targets(database_url: str, redis_url: str) -> None:
    database = make_url(database_url).database

    # Check the effective Redis database, including ?db= query overrides.
    redis_db = parse_url(redis_url).get("db", 0)

    if database != "gateway_test":
        raise RuntimeError(
            "Refusing destructive test setup: TEST_DATABASE_URL must target 'gateway_test'."
        )

    if redis_db != 15:
        raise RuntimeError(
            "Refusing destructive test setup: TEST_REDIS_URL must target Redis database 15."
        )
