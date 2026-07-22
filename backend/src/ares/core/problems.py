"""RFC 9457-style errors for the HTTP boundary."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

problem_logger = logging.getLogger("ares.problem")


class ProblemDetail(BaseModel):
    """Stable error envelope returned by every ARES endpoint."""

    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str
    request_id: str


class AresProblem(Exception):
    """Known application problem safe to expose to a local client."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        type_uri: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.type_uri = type_uri or f"urn:ares:error:{code.lower().replace('_', '-')}"


def install_problem_handlers(app: FastAPI) -> None:
    """Install consistent handlers without leaking traces or input values."""

    @app.exception_handler(AresProblem)
    async def handle_ares_problem(request: Request, exc: AresProblem) -> JSONResponse:
        return _response(
            request,
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            type_uri=exc.type_uri,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_problem(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"] if part not in {"body", "query"})
                for error in exc.errors()
            }
        )
        suffix = f" Invalid fields: {', '.join(fields)}." if any(fields) else ""
        return _response(
            request,
            status=422,
            code="REQUEST_VALIDATION_FAILED",
            title="Request validation failed",
            detail=f"The request does not match the expected schema.{suffix}",
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status_phrase = HTTPStatus(exc.status_code).phrase
        return _response(
            request,
            status=exc.status_code,
            code=f"HTTP_{exc.status_code}",
            title=status_phrase,
            detail=status_phrase,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_problem(request: Request, exc: Exception) -> JSONResponse:
        problem_logger.error(
            "Unhandled application error",
            extra={
                "event": "http.request.failed",
                "request_id": _request_id(request),
                "error_code": "INTERNAL_ERROR",
                "exception_type": type(exc).__name__,
            },
        )
        return _response(
            request,
            status=500,
            code="INTERNAL_ERROR",
            title="Internal server error",
            detail="An unexpected local error occurred.",
        )

    _install_problem_openapi(app)


def _install_problem_openapi(app: FastAPI) -> None:
    """Make generated clients see the same validation media type as runtime."""

    original_openapi = app.openapi

    def openapi_with_problem_details() -> dict[str, Any]:
        schema = original_openapi()
        paths = schema.get("paths", {})
        if isinstance(paths, dict):
            for path_item in paths.values():
                if not isinstance(path_item, dict):
                    continue
                for operation in path_item.values():
                    if not isinstance(operation, dict):
                        continue
                    responses = operation.get("responses")
                    if isinstance(responses, dict) and "422" in responses:
                        responses["422"] = {
                            "description": "Request validation failed",
                            "content": {
                                "application/problem+json": {
                                    "schema": ProblemDetail.model_json_schema()
                                }
                            },
                        }
        return schema

    app.openapi = openapi_with_problem_details  # type: ignore[method-assign]


def _response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    type_uri: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    problem = ProblemDetail(
        type=type_uri or f"urn:ares:error:{code.lower().replace('_', '-')}",
        title=title,
        status=status,
        code=code,
        detail=detail,
        instance=request.url.path,
        request_id=request_id,
    )
    response_headers = {
        key: value for key, value in (headers or {}).items() if key.lower() != "x-request-id"
    }
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=response_headers,
    )


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))
