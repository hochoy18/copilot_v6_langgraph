"""Lifespan wiring tests — confirm the audit retention scheduler is
started by the FastAPI app (T42 / #37).

Acceptance criterion (Issue #37 AC2, "background migration moves
>1-year logs") is only credible if the sweep actually runs in the
deployed process. The lifespan test below exercises the same
`lifespan` context manager the FastAPI app uses and asserts the
scheduler is running on `app.state` for the duration of the
lifespan window and cleanly torn down on exit.

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
    """Tight sweep interval so the loop ticks at least once during the test window."""
    return Settings(
        oidc_issuer_url="https://test.example.com",
        oidc_id_token_signing_key="test-idp-hs256-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
        audit_cold_sweep_interval_seconds=60.0,
        # Disable the scheduler so the lifespan test can boot a
        # fresh app without a real sweep firing — the lifespan
        # test only asserts on wiring, not on actual migration.
        # Per-tick behaviour is covered by the scheduler + service
        # unit tests.
        audit_retention_enabled=False,
    )


class _AsyncMongoMockForLifespan:
    """Stand-in `MongoClient` the lifespan can `close()` cleanly.

    The lifespan calls `MongoClient(settings)`, so the constructor
    accepts the settings argument and ignores it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_audit_retention_lifespan_test"]

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
        return _AsyncMongoMockForLifespan(settings)

    app = create_app(settings=settings)
    original = getattr(app_main, "MongoClient")  # noqa: B009
    setattr(app_main, "MongoClient", _fake_mongo_client)  # noqa: B010
    try:
        fake = _fake_mongo_client(settings)
        app.state.database = fake.database
        await init_database(fake.database)
        yield app
    finally:
        setattr(app_main, "MongoClient", original)  # noqa: B010


class TestAuditRetentionSchedulerWiring:
    """The audit retention scheduler is installed by the lifespan."""

    async def test_scheduler_is_not_running_before_lifespan(
        self, app: Any
    ) -> None:
        """Before the lifespan runs the scheduler exists but is idle."""
        # The lifespan hasn't run yet — `app.state.audit_retention_scheduler`
        # is the instance create_app *would* install, but the start()
        # call only fires inside the lifespan context manager.
        scheduler = getattr(app.state, "audit_retention_scheduler", None)
        assert scheduler is None, (
            "create_app must not start the scheduler outside the lifespan"
        )

    async def test_scheduler_is_running_inside_lifespan(
        self, app: Any, settings: Settings
    ) -> None:
        """With `audit_retention_enabled=True`, the loop is alive during lifespan."""
        # Re-create with the scheduler enabled — the default-disabled
        # fixture guards against background churn during the rest of
        # the lifespan test set; this single test explicitly turns
        # the switch on.
        from app.main import create_app

        enabled_settings = settings.model_copy(update={"audit_retention_enabled": True})
        enabled_app = create_app(settings=enabled_settings)
        # Replicate the lifespan's Mongo swap.
        import app.main as app_main

        def _fake_mongo_client(settings: Settings) -> _AsyncMongoMockForLifespan:
            return _AsyncMongoMockForLifespan(settings)

        original = getattr(app_main, "MongoClient")  # noqa: B009
        setattr(app_main, "MongoClient", _fake_mongo_client)  # noqa: B010
        try:
            async with enabled_app.router.lifespan_context(enabled_app):
                scheduler = getattr(
                    enabled_app.state, "audit_retention_scheduler", None
                )
                assert scheduler is not None, (
                    "lifespan did not install the audit retention scheduler"
                )
                assert scheduler.is_running is True
                assert scheduler._task is not None  # noqa: SLF001
                assert scheduler._task.get_name() == "audit-retention-scheduler"

            # After exiting the lifespan the scheduler must be stopped.
            assert scheduler.is_running is False
        finally:
            setattr(app_main, "MongoClient", original)  # noqa: B010

    async def test_cold_storage_is_installed_on_app_state(
        self, app: Any
    ) -> None:
        """The `FileAuditColdStorage` backend is wired through the lifespan."""
        # Outside the lifespan the state isn't installed yet (create_app
        # only does so inside the lifespan). The wiring check below
        # is the inside-lifespan view.
        import app.main as app_main

        def _fake_mongo_client(settings: Settings) -> _AsyncMongoMockForLifespan:
            return _AsyncMongoMockForLifespan(settings)

        original = getattr(app_main, "MongoClient")  # noqa: B009
        setattr(app_main, "MongoClient", _fake_mongo_client)  # noqa: B010
        try:
            async with app.router.lifespan_context(app):
                cold = getattr(app.state, "audit_cold_storage", None)
                assert cold is not None, (
                    "lifespan did not install the audit cold storage"
                )
                # The base_dir is what settings.audit_cold_storage_dir
                # resolved to — confirm it round-trips.
                assert str(cold.base_dir).endswith("audit_cold")
        finally:
            setattr(app_main, "MongoClient", original)  # noqa: B010