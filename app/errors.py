"""One error envelope for the whole service.

Every error body is ``{"error": "<code>", "message": "<prose>"}`` — the shape
sync-store established and the one `agent_mcp`'s ASGI gate already returns for 401
and 403. FastAPI's default is ``{"detail": ...}``, so without these handlers a
client would need two parsers to read errors from one service, and which one it
needed would depend on whether the failure happened in Bloom's code or in the
library's middleware.

The codes are stable strings, not prose: Aperture branches on them. Messages are
for a human reading a log or a toast.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "unprocessable",
    503: "unavailable",
}


class ApiError(Exception):
    """An error with a chosen status and code, raised from route code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message},
        headers=headers or None,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Attach the envelope to a FastAPI app. Call once, at construction."""

    @app.exception_handler(ApiError)
    async def _on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Flattened to one line: a GUI shows this in a toast, and pydantic's nested
        # error list is unreadable there. The full detail is still in the log.
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()) if p != 'body')}: {err.get('msg')}"
            for err in exc.errors()
        )
        return error_response(422, "unprocessable", detail or "invalid request body")

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(
            exc.status_code,
            _CODES.get(exc.status_code, "error"),
            str(exc.detail),
            **(exc.headers or {}),
        )
