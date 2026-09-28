"""Contract tests for CORS middleware wiring (issue #2 acceptance criterion).

The CORS *policy* is deferred to V1.1 per SPEC; the scaffold only requires
the middleware be wired and that configured origins pass preflight while
unconfigured origins are blocked.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_cors_allows_configured_origin(client: AsyncClient) -> None:
    """An OPTIONS preflight from the configured dev origin returns CORS headers."""
    response = await client.options(
        "/healthz",
        headers={
            "Origin": "http://testclient",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette's CORSMiddleware returns 200 to a passing preflight.
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "http://testclient"


@pytest.mark.asyncio
async def test_cors_blocks_unconfigured_origin(client: AsyncClient) -> None:
    """An origin not in the allow-list does not get the ACAO header."""
    response = await client.options(
        "/healthz",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Middleware may either omit ACAO or echo the disallowed origin — both fail the contract.
    assert response.headers.get("access-control-allow-origin") != "http://evil.example"