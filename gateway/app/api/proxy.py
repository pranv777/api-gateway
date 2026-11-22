"""The gateway's forwarding endpoint: /api/{service}/{path...}."""

import logging
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from app.api.deps import Principal, enforce_ip_limit, get_principal
from app.core.rate_limit import RateLimitResult, get_client_ip
from app.services.logging import record_request
from app.services.routing import (
    BodyTooLargeError,
    InvalidPathError,
    UpstreamError,
    UpstreamTimeoutError,
    build_upstream_headers,
    build_upstream_url,
    filter_response_headers,
    forward,
    read_body_limited,
)

logger = logging.getLogger("app.proxy")
router = APIRouter(tags=["gateway"])

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _error(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


async def proxy(
    service: str, request: Request, principal: Principal = Depends(get_principal)
) -> Response:
    started = request.state.started_at
    state = request.app.state
    settings = state.settings
    request_id: str = request.state.request_id
    client_ip = get_client_ip(request, settings.trusted_proxy_count)

    def finish(response: Response, *, rate_limited: bool = False) -> Response:
        # Persisting the log row happens after the response is sent.
        response.background = BackgroundTask(
            record_request,
            state.sessionmaker,
            request_id=request_id,
            user_id=principal.user_id,
            api_key_id=principal.api_key_id,
            method=request.method,
            endpoint=request.url.path,
            status_code=response.status_code,
            duration_ms=(time.perf_counter() - started) * 1000,
            is_rate_limited=rate_limited,
        )
        return response

    route = state.routes.get(service)
    if route is None:
        return finish(_error(404, "Unknown route"))
    try:
        url = build_upstream_url(route, request.path_params.get("rest", ""), request.url.query)
    except InvalidPathError:
        return finish(_error(400, "Invalid path"))

    # Per-user and per-key limits are both enforced (the per-IP limit already ran).
    result = await state.rate_limiter.check_all(
        [("user", str(principal.user_id)), ("api_key", str(principal.api_key_id))]
    )
    if not result.allowed:
        return finish(_error(429, "Rate limit exceeded", result.headers()), rate_limited=True)

    try:
        body = await read_body_limited(request.stream(), settings.max_request_body_bytes)
    except BodyTooLargeError:
        return finish(_error(413, "Request body too large"))

    headers = build_upstream_headers(
        request.headers,
        request_id=request_id,
        user_id=str(principal.user_id),
        api_key_id=str(principal.api_key_id),
        client_ip=client_ip,
    )
    try:
        upstream = await forward(state.http_client, route, request.method, url, headers, body)
    except UpstreamTimeoutError:
        logger.warning("upstream timeout", extra={"request_id": request_id, "service": route.name})
        return finish(_error(504, "Upstream service timed out"))
    except UpstreamError:
        logger.warning("upstream failure", extra={"request_id": request_id, "service": route.name})
        return finish(_error(502, "Bad gateway"))

    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name, value in filter_response_headers(upstream.headers):
        response.headers.append(name, value)

    ip_result: RateLimitResult | None = getattr(request.state, "ip_rate_limit", None)
    tightest = min((r for r in (result, ip_result) if r), key=lambda r: r.remaining)
    for name, value in tightest.headers().items():
        response.headers[name] = value
    return finish(response)


for method in PROXY_METHODS:
    router.add_api_route(
        "/api/{service}",
        proxy,
        methods=[method],
        dependencies=[Depends(enforce_ip_limit)],
        include_in_schema=False,
        operation_id=f"proxy_{method.lower()}_root",
    )

    router.add_api_route(
        "/api/{service}/{rest:path}",
        proxy,
        methods=[method],
        dependencies=[Depends(enforce_ip_limit)],
        operation_id=f"proxy_{method.lower()}_path",
        summary=f"Forward a {method} request to an upstream service",
        description=("Requires an X-API-Key header. service is products or orders."),
    )
