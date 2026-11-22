import httpx
from sqlalchemy import select

from app.db.models import RequestLog
from tests.helpers import create_key, key_header, signup


def _fail_on_boom(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("boom"):
        return httpx.Response(500, json={"detail": "upstream exploded"})
    return httpx.Response(200, json={"ok": True})


async def test_usage_counts_are_accurate(client, upstream):
    upstream.respond = _fail_on_boom
    user = await signup(client)
    key = await create_key(client, user)
    for _ in range(3):
        await client.get("/api/products", headers=key_header(key))
    await client.get("/api/products/boom", headers=key_header(key))
    await client.get("/api/nothing-here", headers=key_header(key))  # unknown route -> 404

    usage = (await client.get("/analytics/usage", headers=user["headers"])).json()
    assert usage["total_requests"] == 5
    assert usage["successful_requests"] == 3
    assert usage["failed_requests"] == 2
    assert usage["rate_limited_requests"] == 0
    assert usage["average_response_time_ms"] > 0


async def test_average_response_time_matches_logged_durations(client):
    user = await signup(client)
    key = await create_key(client, user)
    for _ in range(4):
        await client.get("/api/orders", headers=key_header(key))

    page = (await client.get("/analytics/requests", headers=user["headers"])).json()
    durations = [item["duration_ms"] for item in page["items"]]
    assert len(durations) == 4 and all(d >= 0 for d in durations)
    usage = (await client.get("/analytics/usage", headers=user["headers"])).json()
    assert abs(usage["average_response_time_ms"] - sum(durations) / 4) < 0.01


async def test_rate_limited_requests_are_tracked(make_client):
    client = await make_client(
        token_bucket_capacity=2, token_bucket_refill_rate=0.001, user_limit_multiplier=10
    )
    user = await signup(client)
    key = await create_key(client, user)
    statuses = [
        (await client.get("/api/products", headers=key_header(key))).status_code for _ in range(3)
    ]
    assert statuses == [200, 200, 429]

    usage = (await client.get("/analytics/usage", headers=user["headers"])).json()
    assert usage["total_requests"] == 3
    assert usage["successful_requests"] == 2
    assert usage["rate_limited_requests"] == 1
    assert usage["failed_requests"] == 0  # a gateway 429 is counted only as rate-limited

    items = (await client.get("/analytics/requests", headers=user["headers"])).json()["items"]
    assert [i["is_rate_limited"] for i in items] == [True, False, False]  # newest first
    assert items[0]["status_code"] == 429


async def test_filtering_by_api_key_and_date_range(client):
    user = await signup(client)
    key_a, key_b = await create_key(client, user, "a"), await create_key(client, user, "b")
    for _ in range(2):
        await client.get("/api/products", headers=key_header(key_a))
    await client.get("/api/products", headers=key_header(key_b))
    auth = user["headers"]

    by_a = (
        await client.get("/analytics/usage", params={"api_key_id": key_a["id"]}, headers=auth)
    ).json()
    by_b = (
        await client.get("/analytics/usage", params={"api_key_id": key_b["id"]}, headers=auth)
    ).json()
    assert (by_a["total_requests"], by_b["total_requests"]) == (2, 1)

    future = await client.get(
        "/analytics/usage", params={"start": "2999-01-01T00:00:00Z"}, headers=auth
    )
    past = await client.get(
        "/analytics/usage", params={"end": "2000-01-01T00:00:00Z"}, headers=auth
    )
    window = await client.get(
        "/analytics/usage",
        params={"start": "2000-01-01T00:00:00Z", "end": "2999-01-01T00:00:00Z"},
        headers=auth,
    )
    assert future.json()["total_requests"] == 0
    assert past.json()["total_requests"] == 0
    assert window.json()["total_requests"] == 3

    inverted = await client.get(
        "/analytics/usage",
        params={"start": "2999-01-01T00:00:00Z", "end": "2000-01-01T00:00:00Z"},
        headers=auth,
    )
    assert inverted.status_code == 422


async def test_requests_endpoint_paginates(client):
    user = await signup(client)
    key = await create_key(client, user)
    for _ in range(5):
        await client.get("/api/products", headers=key_header(key))

    first = (
        await client.get("/analytics/requests", params={"limit": 2}, headers=user["headers"])
    ).json()
    last = (
        await client.get(
            "/analytics/requests", params={"limit": 2, "offset": 4}, headers=user["headers"]
        )
    ).json()
    assert first["total"] == 5 and len(first["items"]) == 2
    assert len(last["items"]) == 1


async def test_users_only_see_their_own_data(client):
    alice, bob = await signup(client), await signup(client)
    alice_key, bob_key = await create_key(client, alice), await create_key(client, bob)
    for _ in range(3):
        await client.get("/api/products", headers=key_header(alice_key))
    await client.get("/api/orders", headers=key_header(bob_key))

    alice_usage = (await client.get("/analytics/usage", headers=alice["headers"])).json()
    bob_usage = (await client.get("/analytics/usage", headers=bob["headers"])).json()
    assert (alice_usage["total_requests"], bob_usage["total_requests"]) == (3, 1)

    # Bob cannot get at Alice's data by naming her key.
    peek = (
        await client.get(
            "/analytics/usage", params={"api_key_id": alice_key["id"]}, headers=bob["headers"]
        )
    ).json()
    assert peek["total_requests"] == 0
    bob_items = (await client.get("/analytics/requests", headers=bob["headers"])).json()["items"]
    assert all(i["endpoint"] == "/api/orders" for i in bob_items)


async def test_analytics_require_login(client):
    assert (await client.get("/analytics/usage")).status_code == 401
    assert (await client.get("/analytics/requests")).status_code == 401


async def test_log_rows_hold_metadata_only(client, infra, upstream):
    user = await signup(client)
    key = await create_key(client, user)
    response = await client.post(
        "/api/products?token=super-secret-query",
        json={"password": "hunter2-body-secret"},
        headers=key_header(key),
    )
    async with infra.sessionmaker() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()

    assert row.request_id == response.headers["x-request-id"]
    assert row.method == "POST" and row.endpoint == "/api/products"  # no query string
    assert row.status_code == 200 and row.is_rate_limited is False
    stored = " ".join(str(v) for v in vars(row).values())
    for secret in [key["key"], "hunter2-body-secret", "super-secret-query"]:
        assert secret not in stored
