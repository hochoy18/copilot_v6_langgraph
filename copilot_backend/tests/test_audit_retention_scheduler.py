"""Tests for `AuditRetentionScheduler` (T42 / #37).

Mirrors the T39 / `ConversationLifecycleScheduler` coverage:

* `start` is idempotent — calling it twice doesn't spawn a second
  loop task.
* `stop` is safe to call when never started (no-op).
* The master switch (`enabled=False`) makes `start` a no-op.
* The loop ticks on the configured interval — `run_once` is
  exposed for tests to drive the sweep deterministically.
* A failing tick (e.g. Mongo blip) doesn't crash the loop — the
  next tick retries.

We don't drive the loop long enough to observe a real interval
elapsed; the seam is exposed via `run_once` for deterministic tests,
and `start`/`stop` is verified by the lifespan wiring test
(`test_audit_retention_lifespan.py`).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.audit.cold_storage import FileAuditColdStorage
from app.audit.retention import (
    AuditRetentionScheduler,
    AuditRetentionService,
)
from app.db.init_db import init_database
from app.repositories.audit_logs import AuditLogRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Tight scan interval so a single integration run sees at least one tick."""
    return Settings(audit_cold_sweep_interval_seconds=60.0)


@pytest.fixture
async def repo() -> AuditLogRepository:
    db = AsyncMongoMockClient()["copilot_audit_scheduler_test"]
    await init_database(db)
    return AuditLogRepository(db)


@pytest.fixture
def cold_storage(tmp_path: Path) -> FileAuditColdStorage:
    return FileAuditColdStorage(tmp_path)


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    return AesGcmEncryptor(
        MasterKey(key_bytes=bytes(range(32)), key_id="test-primary"),
    )


def _build_scheduler(
    *,
    settings: Settings,
    repo: AuditLogRepository,
    cold_storage: FileAuditColdStorage,
    encryptor: AesGcmEncryptor,
) -> AuditRetentionScheduler:
    service = AuditRetentionService(
        audit_repository=repo,
        cold_storage=cold_storage,
        encryptor=encryptor,
        hot_retention_seconds=settings.audit_hot_retention_seconds,
    )
    return AuditRetentionScheduler(
        service=service,
        scan_interval_seconds=settings.audit_cold_sweep_interval_seconds,
        enabled=settings.audit_retention_enabled,
    )


class TestAuditRetentionSchedulerLifecycle:
    """`start` / `stop` / `is_running` — the FastAPI lifespan seam."""

    async def test_start_then_stop_creates_and_cancels_task(
        self,
        settings: Settings,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        scheduler = _build_scheduler(
            settings=settings,
            repo=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
        )
        assert scheduler.is_running is False
        await scheduler.start()
        assert scheduler.is_running is True
        await scheduler.stop()
        assert scheduler.is_running is False

    async def test_stop_without_start_is_noop(
        self,
        settings: Settings,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        scheduler = _build_scheduler(
            settings=settings,
            repo=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
        )
        # Must not raise even though `start()` was never called.
        await scheduler.stop()
        assert scheduler.is_running is False

    async def test_start_is_idempotent(
        self,
        settings: Settings,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        scheduler = _build_scheduler(
            settings=settings,
            repo=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
        )
        await scheduler.start()
        first_task = scheduler._task  # noqa: SLF001 — test-only inspection
        await scheduler.start()
        second_task = scheduler._task  # noqa: SLF001
        assert first_task is second_task
        await scheduler.stop()


class TestAuditRetentionSchedulerDisabled:
    """`enabled=False` makes the background loop a no-op."""

    async def test_disabled_scheduler_does_not_start_task(
        self,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        service = AuditRetentionService(
            audit_repository=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
            hot_retention_seconds=365 * 86_400,
        )
        scheduler = AuditRetentionScheduler(
            service=service,
            scan_interval_seconds=60.0,
            enabled=False,
        )
        await scheduler.start()
        assert scheduler.is_running is False
        await scheduler.stop()


class TestAuditRetentionSchedulerTick:
    """`run_once` delegates to the service seam."""

    async def test_run_once_delegates_to_service(
        self,
        settings: Settings,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        scheduler = _build_scheduler(
            settings=settings,
            repo=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
        )
        result = await scheduler.run_once()
        assert result.flipped == 0
        assert result.failed == 0
        assert result.scanned == 0

    async def test_scheduler_loop_survives_failing_tick(
        self,
        settings: Settings,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        encryptor: AesGcmEncryptor,
    ) -> None:
        """A sweep tick that raises must not crash the loop."""
        scheduler = _build_scheduler(
            settings=settings,
            repo=repo,
            cold_storage=cold_storage,
            encryptor=encryptor,
        )
        # Sabotage `run_once` to raise; the loop should swallow it.
        original_run_once = scheduler._service.run_once  # noqa: SLF001

        async def _boom() -> object:
            raise RuntimeError("simulated mongo blip")

        scheduler._service.run_once = _boom  # type: ignore[method-assign, assignment]
        await scheduler.start()
        # Wait briefly for at least one tick to fire.
        await asyncio.sleep(0.1)
        # The loop must still be running after the failing tick.
        assert scheduler.is_running is True
        # Restore so `stop()` cleanup sees a healthy service.
        scheduler._service.run_once = original_run_once  # type: ignore[method-assign]
        await scheduler.stop()
        assert scheduler.is_running is False