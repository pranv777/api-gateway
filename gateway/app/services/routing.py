"""Request routing and forwarding to upstream services.

Security model: the destination host is fixed by server-side configuration. A client only
chooses a *path* below a fixed upstream prefix, and never a host, scheme or port, so the
gateway cannot be pointed at arbitrary URLs (no SSRF). Redirects are not followed, and only an
allow-list of request headers is forwarded.
"""

from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass

import httpx
from starlette.datastructures import Headers

from app.core.config import Settings

# RFC 9110 hop-by-hop headers must not be forwarded by a proxy. content-length and
# content-encoding are dropped from responses because the body is re-framed here.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_FROM_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding"}

# Only these client headers reach the upstream. Everything else (Authorization, X-API-Key,
# Cookie, Host, X-Forwarded-*, X-Original-URL, ...) is dropped.
FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "user-agent",
        "if-none-match",
        "if-modified-since",
        "cache-control",
    }
)


class InvalidPathError(Exception):
    pass


class UpstreamTimeoutError(Exception):
    pass


class UpstreamError(Exception):
    pass


class BodyTooLargeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ServiceRoute:
    name: str
    base_url: str
    upstream_prefix: str  # path prefix on the upstream, e.g. "/products"
    timeout: httpx.Timeout


def build_routes(settings: Settings) -> dict[str, ServiceRoute]:
    """Map the first path segment after /api/ to a trusted service definition."""

    def timeout(override: float | None) -> httpx.Timeout:
        return httpx.Timeout(
            override or settings.upstream_timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
        )

    return {
        "products": ServiceRoute(
            "product-service",
            settings.product_service_url,
            "/products",
            timeout(settings.product_service_timeout_seconds),
        ),
        "orders": ServiceRoute(
            "order-service",
            settings.order_service_url,
            "/orders",
            timeout(settings.order_service_timeout_seconds),
        ),
    }


def build_upstream_url(
    route: ServiceRoute,
    rest: str,
    raw_query: str,
) -> httpx.URL:
    for segment in rest.split("/"):
        if (
            segment in (".", "..")
            or any(char in segment for char in "\\?#%")
            or any(ord(char) < 32 or ord(char) == 127 for char in segment)
        ):
            raise InvalidPathError(rest)

    path = route.upstream_prefix + (f"/{rest}" if rest else "")

    try:
        return httpx.URL(route.base_url).copy_with(
            path=path,
            query=raw_query.encode() or None,
        )
    except httpx.InvalidURL as exc:
        raise InvalidPathError(rest) from exc


def build_upstream_headers(
    client_headers: Headers, *, request_id: str, user_id: str, api_key_id: str, client_ip: str
) -> dict[str, str]:
    headers = {k: v for k, v in client_headers.items() if k.lower() in FORWARDED_REQUEST_HEADERS}
    headers["X-Request-ID"] = request_id
    headers["X-Gateway-User-Id"] = user_id
    headers["X-Gateway-Api-Key-Id"] = api_key_id
    # Overwritten, never appended to: the client-supplied value is not trusted.
    headers["X-Forwarded-For"] = client_ip
    return headers


def filter_response_headers(
    upstream_headers: httpx.Headers,
) -> Iterator[tuple[str, str]]:
    connection_tokens = {
        token.strip().lower()
        for value in upstream_headers.get_list("connection")
        for token in value.split(",")
        if token.strip()
    }

    blocked = _DROP_FROM_RESPONSE | connection_tokens

    for name, value in upstream_headers.multi_items():
        lowered = name.lower()
        if lowered in blocked or lowered.startswith("x-ratelimit-"):
            continue
        yield name, value


async def read_body_limited(stream: AsyncIterator[bytes], limit: int) -> bytes:
    """Read a request body from an async chunk iterator, refusing to buffer more than `limit`."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > limit:
            raise BodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


async def forward(
    client: httpx.AsyncClient,
    route: ServiceRoute,
    method: str,
    url: httpx.URL,
    headers: Mapping[str, str],
    body: bytes,
) -> httpx.Response:
    try:
        return await client.request(
            method, url, headers=headers, content=body or None, timeout=route.timeout
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError from exc
    except httpx.HTTPError as exc:  # connection failures, protocol errors, decoding errors
        raise UpstreamError from exc
