import asyncio

from app.core.config import LimitScope
from app.core.rate_limit import RateLimiter
from tests.helpers import create_key, key_header, signup


# ------------------------------------------------------------- limiter level
def _limiter(infra, make_settings, **overrides) -> RateLimiter:
    return RateLimiter(infra.redis, make_settings(**overrides))


async def test_token_bucket_enforces_capacity(infra, make_settings):
    limiter = _limiter(
        infra, make_settings, token_bucket_capacity=3, token_bucket_refill_rate=0.001
    )
    results = [await limiter.check("api_key", "k1") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert all(r.limit == 3 for r in results)
    assert results[3].retry_after >= 1


async def test_token_bucket_refills_over_time(infra, make_settings):
    limiter = _limiter(infra, make_settings, token_bucket_capacity=2, token_bucket_refill_rate=10)
    assert (await limiter.check("api_key", "k")).allowed
    assert (await limiter.check("api_key", "k")).allowed
    assert not (await limiter.check("api_key", "k")).allowed
    await asyncio.sleep(0.3)  # ~3 tokens refilled, capped at the capacity of 2
    assert (await limiter.check("api_key", "k")).allowed


async def test_sliding_window_enforces_limit_and_expires(infra, make_settings):
    limiter = _limiter(
        infra,
        make_settings,
        rate_limit_algorithm="sliding_window",
        sliding_window_limit=2,
        sliding_window_seconds=1,
    )
    assert (await limiter.check("api_key", "k")).allowed
    assert (await limiter.check("api_key", "k")).allowed
    denied = await limiter.check("api_key", "k")
    assert not denied.allowed and 1 <= denied.retry_after <= 1
    await asyncio.sleep(1.2)
    assert (await limiter.check("api_key", "k")).allowed


async def test_identifiers_and_scopes_are_independent(infra, make_settings):
    limiter = _limiter(
        infra, make_settings, token_bucket_capacity=1, token_bucket_refill_rate=0.001
    )
    assert (await limiter.check("api_key", "a")).allowed
    assert not (await limiter.check("api_key", "a")).allowed
    assert (await limiter.check("api_key", "b")).allowed  # other identifier
    assert (await limiter.check("user", "a")).allowed  # other scope, same identifier


async def test_redis_keys_expire(infra, make_settings):
    tb = _limiter(infra, make_settings, token_bucket_capacity=5, token_bucket_refill_rate=5)
    sw = _limiter(
        infra,
        make_settings,
        rate_limit_algorithm="sliding_window",
        sliding_window_limit=5,
        sliding_window_seconds=10,
    )
    await tb.check("api_key", "ttl")
    await sw.check("api_key", "ttl")
    assert 0 < await infra.redis.pttl("rl:token_bucket:api_key:ttl") <= 2000
    assert 0 < await infra.redis.pttl("rl:sliding_window:api_key:ttl") <= 11_000


async def test_token_bucket_is_atomic_under_concurrency(infra, make_settings):
    limiter = _limiter(
        infra, make_settings, token_bucket_capacity=10, token_bucket_refill_rate=0.001
    )
    results = await asyncio.gather(*[limiter.check("api_key", "race") for _ in range(100)])
    assert sum(r.allowed for r in results) == 10


async def test_sliding_window_is_atomic_under_concurrency(infra, make_settings):
    limiter = _limiter(
        infra,
        make_settings,
        rate_limit_algorithm="sliding_window",
        sliding_window_limit=10,
        sliding_window_seconds=60,
    )
    results = await asyncio.gather(*[limiter.check("api_key", "race") for _ in range(100)])
    assert sum(r.allowed for r in results) == 10


async def test_check_all_stops_at_first_denial(infra, make_settings):
    limiter = _limiter(
        infra, make_settings, token_bucket_capacity=1, token_bucket_refill_rate=0.001
    )
    checks: list[tuple[LimitScope, str]] = [("user", "u"), ("api_key", "k")]
    assert (await limiter.check_all(checks)).allowed
    assert not (await limiter.check_all(checks)).allowed


# ----------------------------------------------------------------- HTTP level
async def _setup(client, name="k"):
    user = await signup(client)
    return user, await create_key(client, user, name)


async def test_over_limit_returns_429_with_headers(make_client):
    client = await make_client(token_bucket_capacity=3, token_bucket_refill_rate=0.001)
    _, key = await _setup(client)
    responses = [await client.get("/api/products", headers=key_header(key)) for _ in range(4)]

    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert responses[0].headers["x-ratelimit-limit"] == "3"
    assert responses[0].headers["x-ratelimit-remaining"] == "2"
    assert "retry-after" not in responses[0].headers
    assert responses[3].json() == {"detail": "Rate limit exceeded"}
    assert int(responses[3].headers["retry-after"]) >= 1
    assert responses[3].headers["x-ratelimit-remaining"] == "0"


async def test_sliding_window_algorithm_over_http(make_client):
    client = await make_client(
        rate_limit_algorithm="sliding_window", sliding_window_limit=3, sliding_window_seconds=60
    )
    _, key = await _setup(client)
    statuses = [
        (await client.get("/api/orders", headers=key_header(key))).status_code for _ in range(5)
    ]
    assert statuses == [200, 200, 200, 429, 429]
    limited = await client.get("/api/orders", headers=key_header(key))
    assert 1 <= int(limited.headers["retry-after"]) <= 60


async def test_per_key_limits_are_independent_within_one_user(make_client):
    # user limit = 10 x key limit, so each key hits its own limit first.
    client = await make_client(
        token_bucket_capacity=2, token_bucket_refill_rate=0.001, user_limit_multiplier=10
    )
    user = await signup(client)
    key_a, key_b = await create_key(client, user, "a"), await create_key(client, user, "b")

    a = [
        (await client.get("/api/products", headers=key_header(key_a))).status_code for _ in range(3)
    ]
    assert a == [200, 200, 429]
    assert (await client.get("/api/products", headers=key_header(key_b))).status_code == 200


async def test_per_user_limit_applies_across_keys(make_client):
    # Same limit for user and key: the user's shared budget (2) runs out across both keys,
    # even though each key individually still has headroom.
    client = await make_client(
        token_bucket_capacity=2, token_bucket_refill_rate=0.001, user_limit_multiplier=1
    )
    user = await signup(client)
    key_a, key_b = await create_key(client, user, "a"), await create_key(client, user, "b")

    assert (await client.get("/api/products", headers=key_header(key_a))).status_code == 200
    assert (await client.get("/api/products", headers=key_header(key_b))).status_code == 200
    assert (await client.get("/api/products", headers=key_header(key_a))).status_code == 429
    assert (await client.get("/api/products", headers=key_header(key_b))).status_code == 429


async def test_other_users_are_not_affected(make_client):
    client = await make_client(token_bucket_capacity=1, token_bucket_refill_rate=0.001)
    _, key1 = await _setup(client)
    _, key2 = await _setup(client)
    assert (await client.get("/api/products", headers=key_header(key1))).status_code == 200
    assert (await client.get("/api/products", headers=key_header(key1))).status_code == 429
    assert (await client.get("/api/products", headers=key_header(key2))).status_code == 200


async def test_ip_limit_throttles_even_valid_keys(make_client):
    client = await make_client(ip_rate_limit_requests=5, ip_rate_limit_window_seconds=3600)
    _, key = await _setup(client)  # creating the key already used 1 of the 5 IP tokens
    statuses = []
    for _ in range(8):
        response = await client.get("/api/products", headers=key_header(key))
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert int(response.headers["retry-after"]) >= 1
            break
    assert statuses[-1] == 429
    assert statuses.count(200) == 4


async def test_ip_limit_is_per_client_address(make_client):
    client_a = await make_client(
        client_host="10.0.0.1", ip_rate_limit_requests=2, ip_rate_limit_window_seconds=3600
    )
    _, key = await _setup(client_a)
    # Same gateway state (Redis), different source address.
    client_b = await make_client(
        client_host="10.0.0.2", ip_rate_limit_requests=2, ip_rate_limit_window_seconds=3600
    )
    assert (await client_b.get("/api/products", headers=key_header(key))).status_code == 200


async def test_forged_forwarded_for_header_does_not_evade_ip_limit(make_client):
    client = await make_client(ip_rate_limit_requests=3, ip_rate_limit_window_seconds=3600)
    _, key = await _setup(client)
    statuses = []
    for i in range(6):
        headers = {**key_header(key), "X-Forwarded-For": f"203.0.113.{i}"}
        statuses.append((await client.get("/api/products", headers=headers)).status_code)
    assert 429 in statuses


async def test_limit_state_is_shared_across_gateway_instances(make_client):
    limits = {"token_bucket_capacity": 5, "token_bucket_refill_rate": 0.001}
    gateway_1, gateway_2 = await make_client(**limits), await make_client(**limits)
    _, key = await _setup(gateway_1)

    statuses = []
    for i in range(8):
        gateway = gateway_1 if i % 2 == 0 else gateway_2
        statuses.append((await gateway.get("/api/products", headers=key_header(key))).status_code)
    assert statuses.count(200) == 5
    assert statuses.count(429) == 3


async def test_concurrent_http_requests_cannot_exceed_the_limit(make_client):
    client = await make_client(token_bucket_capacity=10, token_bucket_refill_rate=0.001)
    _, key = await _setup(client)
    responses = await asyncio.gather(
        *[client.get("/api/products", headers=key_header(key)) for _ in range(40)]
    )
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 10
    assert codes.count(429) == 30
