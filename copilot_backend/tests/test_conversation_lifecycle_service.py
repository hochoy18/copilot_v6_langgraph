"""Tests for `ConversationLifecycleService` and `ConversationLifecycleScheduler` (T39 / #45).

The service drives the `active → idle` and `idle → archived`
transitions on a fixed cadence. Tests cover the threshold
boundaries with an injected fake clock so we don't have to
`asyncio.sleep(900)` to cross the 15-minute idle window.

The scheduler tests focus on the asyncio wiring: idempotent
`start()` / clean `stop()` / `is_running` reporting. The actual
sweep body is exercised via `service.run_once()` so the two seams
stay independently testable.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.conversations.lifecycle import (
    ConversationLifecycleScheduler,
    ConversationLifecycleService,
    LifecycleSweepResult,
)
from app.db.init_db import init_database
from app.db.schemas import ConversationCreate
from app.repositories.base import utcnow
from app.repositories.conversations import ConversationRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo() -> ConversationRepository:
    db = AsyncMongoMockClient()["copilot_lifecycle_service_test"]
    await init_database(db)
    return ConversationRepository(db)


def _conv_input(**overrides: object) -> ConversationCreate:
    base: dict[str, object] = {
        "user_id": str(ObjectId()),
        "title": "session",
        "status": "active",
    }
    base.update(overrides)
    return ConversationCreate(**base)  # type: ignore[arg-type]


class FakeClock:
    """Manual clock — tests advance it via `advance(...)`."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or utcnow()

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def __call__(self) -> datetime:
        return self.now


def _service(
    repo: ConversationRepository, clock: FakeClock, *, idle: int = 900, archive: int = 2_592_000
) -> ConversationLifecycleService:
    return ConversationLifecycleService(
        conversation_repository=repo,
        idle_after_seconds=idle,
        archive_after_seconds=archive,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# run_once — idle branch
# ---------------------------------------------------------------------------


class TestRunOnceIdleBranch:
    """The `active → idle` sweep."""

    @pytest.mark.asyncio
    async def test_flips_active_rows_older_than_idle_threshold(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        conv = await repo.create(_conv_input())
        # Advance the clock past the 15-min idle window. The conv
        # was created with `last_activity_at = now()` at t=0, so
        # at t=15min+1s it qualifies.
        clock.advance(901)

        svc = _service(repo, clock)
        result = await svc.run_once()

        assert isinstance(result, LifecycleSweepResult)
        assert result.idle_flipped == 1
        assert result.failed_idle == 0
        assert result.scanned_idle_candidates == 1

        refreshed = await repo.get(conv.id)
        assert refreshed.status == "idle"
        assert refreshed.idle_since == clock()

    @pytest.mark.asyncio
    async def test_does_not_flip_rows_younger_than_threshold(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        await repo.create(_conv_input())

        svc = _service(repo, clock)
        result = await svc.run_once()

        assert result.idle_flipped == 0
        assert result.scanned_idle_candidates == 0

    @pytest.mark.asyncio
    async def test_idempotent_when_run_twice(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        await repo.create(_conv_input())
        clock.advance(901)
        svc = _service(repo, clock)

        first = await svc.run_once()
        second = await svc.run_once()

        assert first.idle_flipped == 1
        # The second pass sees zero active candidates because the
        # only candidate already flipped to idle.
        assert second.idle_flipped == 0

    @pytest.mark.asyncio
    async def test_does_not_archive_just_idled_rows(
        self, repo: ConversationRepository
    ) -> None:
        """A fresh idle row is still far from the 30-day archive cutoff."""
        clock = FakeClock()
        await repo.create(_conv_input())
        clock.advance(901)

        svc = _service(repo, clock)
        result = await svc.run_once()

        assert result.idle_flipped == 1
        assert result.archived_flipped == 0


# ---------------------------------------------------------------------------
# run_once — archive branch
# ---------------------------------------------------------------------------


class TestRunOnceArchiveBranch:
    """The `idle → archived` sweep."""

    @pytest.mark.asyncio
    async def test_flips_idle_rows_older_than_archive_threshold(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        # Pre-create an `idle` conversation with `idle_since` 31 days ago.
        conv = await repo.create(_conv_input())
        await repo.set_status(conv.id, "idle")
        await repo._collection.update_one(
            {"_id": ObjectId(conv.id)},
            {"$set": {"idle_since": clock() - timedelta(days=31)}},
        )

        svc = _service(repo, clock)
        result = await svc.run_once()

        assert result.archived_flipped == 1
        assert result.scanned_archive_candidates == 1
        refreshed = await repo.get(conv.id)
        assert refreshed.status == "archived"
        assert refreshed.archived_since == clock()

    @pytest.mark.asyncio
    async def test_does_not_archive_fresh_idle_rows(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        conv = await repo.create(_conv_input())
        await repo.set_status(conv.id, "idle")
        # `idle_since` defaults to `now()` via set_status.

        svc = _service(repo, clock)
        result = await svc.run_once()
        assert result.archived_flipped == 0
        refreshed = await repo.get(conv.id)
        assert refreshed.status == "idle"


# ---------------------------------------------------------------------------
# Sweep ordering — both branches in one pass
# ---------------------------------------------------------------------------


class TestSweepOrdering:
    """`run_once` flips idle first, then archive."""

    @pytest.mark.asyncio
    async def test_two_phase_sweep_handles_both_branches(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        # Stale `active` → should flip to `idle`.
        active_old = await repo.create(_conv_input(title="active-old"))
        await repo._collection.update_one(
            {"_id": await _oid(active_old.id)},
            {"$set": {"last_activity_at": clock() - timedelta(minutes=20)}},
        )
        # Stale `idle` → should flip to `archived`.
        idle_old = await repo.create(_conv_input(title="idle-old"))
        await repo.set_status(idle_old.id, "idle")
        await repo._collection.update_one(
            {"_id": await _oid(idle_old.id)},
            {"$set": {"idle_since": clock() - timedelta(days=31)}},
        )

        svc = _service(repo, clock)
        result = await svc.run_once()

        assert result.idle_flipped == 1
        assert result.archived_flipped == 1

        refreshed_active = await repo.get(active_old.id)
        refreshed_idle = await repo.get(idle_old.id)
        assert refreshed_active.status == "idle"
        assert refreshed_idle.status == "archived"


async def _oid(hex_str: str) -> ObjectId:
    return ObjectId(hex_str)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TestScheduler:
    """Asyncio driver: `start` / `stop` / `is_running` / `run_once`."""

    @pytest.mark.asyncio
    async def test_disabled_scheduler_does_not_start(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        scheduler = ConversationLifecycleScheduler(
            service=_service(repo, clock),
            scan_interval_seconds=0.01,
            enabled=False,
        )

        await scheduler.start()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_idempotent_start(self, repo: ConversationRepository) -> None:
        clock = FakeClock()
        scheduler = ConversationLifecycleScheduler(
            service=_service(repo, clock),
            scan_interval_seconds=60.0,
            enabled=True,
        )

        await scheduler.start()
        first_task = scheduler._task
        await scheduler.start()
        # Second `start()` did not spawn a second task.
        assert scheduler._task is first_task
        assert scheduler.is_running is True

        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_stop_is_safe_when_never_started(
        self, repo: ConversationRepository
    ) -> None:
        scheduler = ConversationLifecycleScheduler(
            service=_service(repo, FakeClock()),
            scan_interval_seconds=60.0,
        )
        await scheduler.stop()  # No-op, no exception.

    @pytest.mark.asyncio
    async def test_run_once_delegates_to_service(
        self, repo: ConversationRepository
    ) -> None:
        clock = FakeClock()
        scheduler = ConversationLifecycleScheduler(
            service=_service(repo, clock),
            scan_interval_seconds=60.0,
        )
        await repo.create(_conv_input())
        clock.advance(901)

        result = await scheduler.run_once()
        assert result.idle_flipped == 1

    @pytest.mark.asyncio
    async def test_scheduler_loop_runs_multiple_ticks(
        self, repo: ConversationRepository
    ) -> None:
        """The loop runs `run_once` repeatedly until `stop()`.

        Tests a tick boundary by manually scheduling the loop with
        a tiny interval — the test seeds two conversations, advances
        the clock past the idle threshold, lets the loop run for
        one tick, then stops and asserts the flip happened.
        """
        clock = FakeClock()
        await repo.create(_conv_input())

        scheduler = ConversationLifecycleScheduler(
            service=_service(repo, clock),
            scan_interval_seconds=0.05,
            enabled=True,
        )

        # Start the loop, let one tick run, then advance the clock
        # past the idle threshold so the tick that runs after the
        # advance has something to flip.
        await scheduler.start()
        await asyncio.sleep(0.05)
        clock.advance(901)
        await asyncio.sleep(0.10)  # Give the loop room to tick at least twice.

        await scheduler.stop()

        # The seed conversation should now be `idle` (flipped on
        # some tick after the clock advance).
        seeded = (await repo.list_by_status("active")) + (
            await repo.list_by_status("idle")
        )
        assert any(c.title == "session" and c.status == "idle" for c in seeded)


# ---------------------------------------------------------------------------
# from_settings factory
# ---------------------------------------------------------------------------


def test_from_settings_builds_scheduler_with_configured_thresholds() -> None:
    """The factory wires thresholds from `Settings` rather than hard-coded values."""
    from app.settings import Settings

    settings = Settings(
        conversation_idle_after_seconds=120,
        conversation_archive_after_seconds=86400,
        conversation_lifecycle_scan_interval_seconds=10.0,
    )
    # Build a fake repository — we don't even need a DB for the
    # factory call because the scheduler only stores the handle.
    repo = ConversationRepository(AsyncMongoMockClient()["noop"])
    scheduler = ConversationLifecycleScheduler.from_settings(
        settings=settings, conversation_repository=repo
    )

    assert scheduler._interval == 10.0
    # `service` should carry the configured thresholds — we read
    # them through the `_idle_after` / `_archive_after` deltas.
    assert scheduler._service._idle_after == timedelta(seconds=120)
    assert scheduler._service._archive_after == timedelta(seconds=86400)