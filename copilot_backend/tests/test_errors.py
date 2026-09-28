"""Contract tests for the unified error response format (ADR-0031).

Every error response must carry `code` + `message_zh`. `message_en` and
`details` are optional but must follow the documented shape when present.

Seam: HTTP API. We attach a temporary route that raises so we can observe
how the global handler shapes the response without depending on a future
domain module.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.exceptions import AppError, register_exception_handlers


@pytest.mark.asyncio
async def test_app_error_returns_unified_payload() -> None:
    """AppError -> JSONResponse with code, message_zh, message_en, details."""

    class _Probe(AppError):
        code = "probe_error"
        message_zh = "探测失败"
        message_en = "Probe failed"
        http_status = 418  # "I'm a teapot" — proves the status passes through

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe")
    async def _raise_probe() -> None:
        raise _Probe(details={"foo": "bar"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/probe")

    assert response.status_code == 418
    body = response.json()
    assert body["code"] == "probe_error"
    assert body["message_zh"] == "探测失败"
    assert body["message_en"] == "Probe failed"
    assert body["details"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_unhandled_exception_returns_internal_error_envelope() -> None:
    """Unhandled Exception falls back to the unified `internal_error` shape.

    Guards against leaking stack traces or arbitrary messages to clients.
    """

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def _raise_boom() -> None:
        raise RuntimeError("secret database password leaked in message")

    # raise_app_exceptions=False so the ServerErrorMiddleware's handler
    # chain runs (and our @app.exception_handler(Exception) catches it) instead
    # of bubbling the raw RuntimeError out to the test runner.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["message_zh"] == "服务器内部错误"
    assert body["message_en"] == "Internal server error"
    # Internal text must NOT be leaked; only the class name survives.
    assert "secret" not in response.text
    assert body["details"]["exception_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_unified_payload_omits_none_fields() -> None:
    """When details is None, the field is absent (not null) in the response."""

    class _NoDetails(AppError):
        code = "no_details"
        message_zh = "无细节"
        message_en = "No details"

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/no-details")
    async def _raise_no_details() -> None:
        raise _NoDetails()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/no-details")

    body = response.json()
    assert "details" not in body
    assert body["message_en"] == "No details"