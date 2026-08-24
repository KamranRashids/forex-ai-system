"""Prometheus request metrics middleware (count + latency by route template)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_latency_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

_UNMATCHED: str = "<unmatched>"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        method = request.method
        with REQUEST_LATENCY.labels(method=method, path=_UNMATCHED).time():
            response = await call_next(request)
        # Routing has completed by now; prefer the matched route template so
        # cardinality stays bounded (no raw IDs/paths in labels).
        route = request.scope.get("route")
        path_template = getattr(route, "path", _UNMATCHED)
        if path_template != _UNMATCHED:
            REQUEST_LATENCY.labels(method=method, path=path_template)
        REQUEST_COUNT.labels(method=method, path=path_template, status=response.status_code).inc()
        return response
