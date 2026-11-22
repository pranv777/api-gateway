import httpx
import pytest

from app.services.routing import InvalidPathError, build_routes, build_upstream_url
from tests.helpers import create_key, key_header, signup


async def _authed(client):
    user = await signup(client)
    return user, key_header(await create_key(client, user))


# ------------------------------------------------------------------ unit
def test_build_upstream_url_uses_fixed_host_and_prefix(make_settings):
    route = build_routes(make_settings())["products"]
    url = build_upstream_url(route, "5/items", "a=1&b=x%20y")
    assert str(url) == "http://products:8001/products/5/items?a=1&b=x%20y"
    assert str(build_upstream_url(route, "", "")) == "http://products:8001/products"


@pytest.mark.parametrize("rest", ["..", "5/../../orders", "./x", "a\\b", "a\x00b"])
def test_build_upstream_url_rejects_unsafe_paths(make_settings, rest):
    route = build_routes(make_settings())["products"]
    with pytest.raises(InvalidPathError):
        build_upstream_url(route, rest, "")


def test_an_absolute_url_in_the_path_cannot_change_the_destination(make_settings):
    route = build_routes(make_settings())["products"]
    url = build_upstream_url(route, "http://evil.example/steal", "")
    assert url.host == "products" and url.port == 8001
    assert url.path.startswith("/products/")


# ----------------------------------------------------------- integration
async def test_health_and_readiness(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ready"}


async def test_routes_to_product_and_order_services(client, upstream):
    _, headers = await _authed(client)
    products = await client.get("/api/products/7", headers=headers)
    orders = await client.get("/api/orders", headers=headers)

    assert products.json() == {"upstream_path": "/products/7"}
    assert orders.json() == {"upstream_path": "/orders"}
    assert [(r.url.host, r.url.port, r.url.path) for r in upstream.requests] == [
        ("products", 8001, "/products/7"),
        ("orders", 8002, "/orders"),
    ]


async def test_query_parameters_are_preserved(client, upstream):
    _, headers = await _authed(client)
    await client.get("/api/products?tag=a&tag=b&q=x%20y", headers=headers)
    assert upstream.requests[0].url.query == b"tag=a&tag=b&q=x%20y"


async def test_query_string_cannot_override_the_path(client, upstream):
    _, headers = await _authed(client)
    await client.get("/api/products?rest=../orders&service=orders", headers=headers)
    assert upstream.requests[0].url.path == "/products"


async def test_request_body_and_response_are_passed_through(client, upstream):
    upstream.respond = lambda req: httpx.Response(
        201, json={"id": 9}, headers={"X-Custom": "yes", "Set-Cookie": "a=1"}
    )
    _, headers = await _authed(client)
    response = await client.post(
        "/api/products", json={"name": "Desk", "price": 120}, headers=headers
    )

    assert response.status_code == 201
    assert response.json() == {"id": 9}
    assert response.headers["x-custom"] == "yes"
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.headers["content-type"] == "application/json"
    assert sent.content == b'{"name":"Desk","price":120}'


async def test_upstream_error_status_and_body_are_returned_unchanged(client, upstream):
    upstream.respond = lambda req: httpx.Response(404, json={"detail": "Product not found"})
    _, headers = await _authed(client)
    response = await client.get("/api/products/999", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Product not found"}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_other_methods_are_forwarded(client, upstream, method):
    _, headers = await _authed(client)
    response = await client.request(method, "/api/orders/3", content=b"{}", headers=headers)
    assert response.status_code == 200
    assert upstream.requests[0].method == method
    assert upstream.requests[0].url.path == "/orders/3"


async def test_unsupported_method_is_rejected(client, upstream):
    _, headers = await _authed(client)
    assert (await client.options("/api/products", headers=headers)).status_code == 405
    assert upstream.requests == []


async def test_unknown_service_is_404_and_never_forwarded(client, upstream):
    _, headers = await _authed(client)
    for path in ["/api/users", "/api/admin/secrets", "/api/localhost:8000/x"]:
        assert (await client.get(path, headers=headers)).status_code == 404
    assert upstream.requests == []


async def test_credentials_and_untrusted_headers_are_not_forwarded(client, upstream):
    user, headers = await _authed(client)
    await client.get(
        "/api/products",
        headers={
            **headers,
            **user["headers"],  # Authorization: Bearer <jwt>
            "Cookie": "session=secret",
            "X-Forwarded-For": "6.6.6.6",
            "X-Forwarded-Host": "evil.example",
            "X-Original-URL": "/admin",
            "Accept": "application/json",
        },
    )
    sent = upstream.requests[0].headers
    for dropped in ["authorization", "x-api-key", "cookie", "x-forwarded-host", "x-original-url"]:
        assert dropped not in sent
    assert sent["x-forwarded-for"] == "127.0.0.1"  # set by the gateway, not the client
    assert sent["host"] == "products:8001"
    assert sent["accept"] == "application/json"
    assert sent["x-gateway-user-id"] == user["id"]
    assert len(sent["x-request-id"]) == 32


async def test_hop_by_hop_response_headers_are_stripped(client, upstream):
    upstream.respond = lambda req: httpx.Response(
        200, json={}, headers={"Connection": "close", "Keep-Alive": "timeout=5", "X-Ok": "1"}
    )
    _, headers = await _authed(client)
    response = await client.get("/api/products", headers=headers)
    assert "connection" not in response.headers and "keep-alive" not in response.headers
    assert response.headers["x-ok"] == "1"
    assert len(response.headers["x-request-id"]) == 32


async def test_oversized_body_is_rejected(make_client, upstream):
    client = await make_client(max_request_body_bytes=10)
    _, headers = await _authed(client)
    response = await client.post("/api/products", content=b"x" * 11, headers=headers)
    assert response.status_code == 413
    assert upstream.requests == []


async def test_per_service_timeouts_are_applied(make_client, upstream):
    client = await make_client(product_service_timeout_seconds=0.5, upstream_timeout_seconds=3)
    _, headers = await _authed(client)
    await client.get("/api/products", headers=headers)
    await client.get("/api/orders", headers=headers)
    products_timeout, orders_timeout = (r.extensions["timeout"] for r in upstream.requests)
    assert products_timeout["read"] == 0.5
    assert orders_timeout["read"] == 3


async def test_upstream_timeout_returns_504_and_gateway_survives(client, upstream):
    def respond(request):
        if request.url.path == "/products":
            raise httpx.ReadTimeout("secret-internal-detail", request=request)
        return httpx.Response(200, json={"ok": True})

    upstream.respond = respond
    _, headers = await _authed(client)
    timed_out = await client.get("/api/products", headers=headers)
    assert timed_out.status_code == 504
    assert timed_out.json() == {"detail": "Upstream service timed out"}
    assert "secret-internal-detail" not in timed_out.text
    assert (await client.get("/api/orders", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused to 10.0.0.9"),
        httpx.RemoteProtocolError("malformed response"),
    ],
)
async def test_upstream_failures_return_502_without_internals(client, upstream, error):
    def respond(request):
        raise error

    upstream.respond = respond
    _, headers = await _authed(client)
    response = await client.get("/api/products", headers=headers)
    assert response.status_code == 502
    assert response.json() == {"detail": "Bad gateway"}
    assert "10.0.0.9" not in response.text and "Traceback" not in response.text
