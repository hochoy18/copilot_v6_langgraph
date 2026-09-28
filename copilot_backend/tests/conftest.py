"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings with a permissive CORS origin so preflight tests work."""
    return Settings(cors_allow_origins=["http://testclient"])


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Fresh FastAPI app per test."""
    return create_app(settings=settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Async HTTP client wired directly to the ASGI app (no network)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def client_app(client: AsyncClient, app: FastAPI) -> FastAPI:
    """Public alias so tests can call `dependency_overrides` cleanly.

    Reaching into `client._transport.app` would work but it pokes at a
    private httpx attribute; this fixture surfaces the same FastAPI
    handle through a stable test seam.
    """
    return app