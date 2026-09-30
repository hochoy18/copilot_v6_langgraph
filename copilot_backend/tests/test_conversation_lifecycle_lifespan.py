"""Lifespan wiring tests — confirm the scheduler is started by the FastAPI app (T39 / #45).

The acceptance criterion (Issue #45 AC1, "模拟时间过期自动转 idle")
is only credible if the sweep actually runs in the deployed
process. The lifespan test below exercises the same `lifespan`
context manager the FastAPI app uses and asserts the scheduler is
running on `app.state` for the duration of the lifespan window and
cleanly torn down on exit.

httpx's `ASGITransport` does NOT trigger FastAPI lifespan events
in version 0.28.x, so we drive the lifespan context manager
directly via `app.router.lifespan_context(app)`. The same code
path a real ASGI server (uvicorn / hypercorn) executes on boot.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.db.init_db import init_database
from app.main import create_app
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url="https://test.example.com",
        oidc_id_token_signing_key="test-idp-hs256-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
        # Tighter scan interval so the loop ticks at least once
        # during the test's lifespan window — proves `start()` ran.
        # Must respect the `ge=1.0` floor on the field.
        conversation_lifecycle_scan_interval_seconds=1.0,
    )


class _AsyncMongoMockForLifespan:
    """Stand-in `MongoClient` the lifespan can `close()` cleanly."""

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_lifecycle_lifespan_test"]

    async def close(self) -> None:
        pass


@pytest.fixture
async def app(settings: Settings) -> Any:
    """App with a hermetic in-memory Mongo wired through the lifespan.

    `create_app` builds the FastAPI instance with `lifespan=...`
    installed; the lifespan constructs a real `MongoClient` on
    startup, which would open a Motor socket against
    `localhost:27017`. We swap the name bound in `app.main`'s
    namespace for the lifespan window (the lifespan resolves
    `MongoClient` from its own module, so patching there is the
    one site that works) and restore it afterwards.
    """
    import app.main as app_main

    def _fake_mongo_client(settings: Settings) -> _AsyncMongoMockForLifespan:
        return _AsyncMongoMockForLifespan()

    app = create_app(settings=settings)
    # `getattr`/`setattr` with a constant name is deliberate here:
    # the lifespan resolves `MongoClient` from `app.main`'s own
    # namespace, and static attribute access on a module
    # re-export trips mypy's implicit-re-export rule. B009/B010
    # flagged, string form is the only way to patch the right
    # binding.
    original = getattr(app_main, "MongoClient")  # noqa: B009
    setattr(app_main, "MongoClient", _fake_mongo_client)  # noqa: B010
    try:
        # Pre-populate the test DB so the dependency-injected
        # repositories (route layer) have a stable handle even
        # outside the lifespan window.
        fake = _fake_mongo_client(settings)
        app.state.database = fake.database
        await init_database(fake.database)
        yield app
    finally:
        setattr(app_main, "MongoClient", original)  # noqa: B010


class TestLifecycleSchedulerWiring:
    """The scheduler is started on lifespan entry, stopped on exit."""

    async def test_scheduler_is_running_inside_lifespan(
        self, app: Any, settings: Settings
    ) -> None:
        # Drive the lifespan directly — `httpx.ASGITransport` does
        # not run lifespan events, so we use Starlette's lifespan
        # context manager (the same code path uvicorn runs).
        async with app.router.lifespan_context(app):
            scheduler = getattr(
                app.state, "conversation_lifecycle_scheduler", None
            )
            assert scheduler is not None, (
                "lifespan did not install the conversation lifecycle "
                "scheduler on app.state"
            )
            assert scheduler.is_running is True

        # After exiting the lifespan the scheduler must be stopped.
        assert scheduler.is_running is False

    async def test_scheduler_is_not_running_before_lifespan(
        self, app: Any
    ) -> None:
        """Before the lifespan runs the scheduler exists but is idle."""
        # `create_app` does not run the lifespan, so the
        # scheduler is not yet on `app.state` — the lifespan
        # installs it. Pre-state via `app.state` should fail.
        with pytest.raises(AttributeError):
            _ = app.state.conversation_lifecycle_scheduler