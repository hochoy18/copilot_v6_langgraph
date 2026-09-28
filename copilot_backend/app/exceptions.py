"""Unified error format and global exception handlers.

Per ADR-0031, every error response from the API carries:

    { "code": str, "message_zh": str, "message_en"?: str, "details"?: dict }

The format is the single source of truth for front-end i18n; raising an
`AppError` from any router guarantees the contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorPayload(BaseModel):
    """The wire shape of every error response (ADR-0031)."""

    code: str
    message_zh: str
    message_en: str | None = None
    details: dict[str, Any] | None = None


class AppError(Exception):
    """Base class for expected, domain-level errors raised by routers.

    Subclass per error family (auth / validation / tool / etc.) and set
    `http_status` to control the response code. The global handler in
    `register_exception_handlers` converts these into ErrorPayload JSON.
    """

    code: str = "internal_error"
    message_zh: str = "服务器内部错误"
    message_en: str = "Internal server error"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(
        self,
        *,
        code: str | None = None,
        message_zh: str | None = None,
        message_en: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message_en or message_zh or self.message_en)
        if code is not None:
            self.code = code
        if message_zh is not None:
            self.message_zh = message_zh
        if message_en is not None:
            self.message_en = message_en
        self.details = details


def _payload_from(exc: AppError) -> ErrorPayload:
    return ErrorPayload(
        code=exc.code,
        message_zh=exc.message_zh,
        message_en=exc.message_en,
        details=exc.details,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire AppError + fallback handlers onto the FastAPI app."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload_from(exc).model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Last-resort safety net. We deliberately don't leak internals; the
        # real trace lands in logs / Langfuse (see ADR-0025).
        # Reuse the AppError defaults so the wire shape stays in lockstep
        # with `_app_error_handler` — no duplicated literals.
        payload_dict = _payload_from(AppError()).model_dump(exclude_none=True)
        # Keep the exception class name in details for ops triage; no
        # arguments / traceback because those can contain PII.
        payload_dict["details"] = {"exception_type": type(exc).__name__}
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=payload_dict,
        )