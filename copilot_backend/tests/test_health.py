"""Contract tests for the /healthz liveness endpoint.

Seam: HTTP API (per SPEC § Testing Decisions). We assert the wire shape that
deploy probes and the dev SPA both depend on, not internals.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_ok_envelope(client: AsyncClient) -> None:
    """`GET /healthz` returns 200 with `{"status": "ok"}`."""
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_does_not_require_auth(client: AsyncClient) -> None:
    """`/healthz` is a public probe — no Authorization header is sent, and it still 200s.

    Guards against the scaffold accidentally wrapping healthz behind a
    future JWT dependency.
    """
    response = await client.get("/healthz", headers={})
    assert response.status_code == 200
    assert "status" in response.json()