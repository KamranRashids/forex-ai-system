"""Central exception hierarchy and RFC 7807 ``problem+json`` error handling.

Every API error — raised domain errors, framework HTTP exceptions, request
validation failures, and unhandled exceptions — is serialized into a single
stable shape::

    {
      "type": "https://forex-ai.dev/problems/<code>",
      "title": "...",
      "status": <int>,
      "detail": "...",
      "instance": "<request path>",
      "correlation_id": "...",   # when available
      ...optional extra fields (e.g. "errors" for validation, "retry_after")
    }

Responses use media type ``application/problem+json`` and always carry the
correlation id response header.
"""

from __future__ import annotations

from typing import Any

from asgi_correlation_id import correlation_id
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE: str = "application/problem+json"
PROBLEM_TYPE_BASE: str = "https://forex-ai.dev/problems/"


class AppError(Exception):
    """Base class for all expected application errors."""

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        extras: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extras = extras or {}
        self.headers = headers


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthorized"
    title = "Authentication required"

    def __init__(self, detail: str = "Not authenticated") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"
    title = "Permission denied"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflicting resource"


class InvalidInputError(AppError):
    """Semantically invalid input that passes schema validation."""

    status_code = 400
    code = "invalid_input"
    title = "Invalid input"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "Rate limit exceeded; retry later.",
            extras={"retry_after": retry_after_seconds},
            headers={"Retry-After": str(max(1, retry_after_seconds))},
        )


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    title = "Service unavailable"


def build_problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble an RFC 7807 body for the given request."""
    problem: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "correlation_id": correlation_id.get(None),
    }
    if extras:
        problem.update(extras)
    return problem


def _problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    extras: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = build_problem(
        request,
        status_code=status_code,
        code=code,
        title=title,
        detail=detail,
        extras=extras,
    )
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install uniform problem+json handlers on the application."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _problem_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            extras=exc.extras,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem_response(
            request,
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            title=getattr(exc, "title", None) or "Request failed",
            detail=str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "loc": [str(loc) for loc in err.get("loc", [])],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return _problem_response(
            request,
            status_code=422,
            code="validation_error",
            title="Request validation failed",
            detail="One or more fields are invalid.",
            extras={"errors": errors},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger = _get_logger()
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=exc,
        )
        return _problem_response(
            request,
            status_code=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred. Reference the correlation id in logs.",
        )


def _get_logger() -> Any:
    # Imported lazily to avoid a circular import with app.core.logging.
    from structlog.stdlib import get_logger

    return get_logger(__name__)
