"""Conversation lifecycle sweep — T39 / #45, ADR-0011.

The sweep runs on a fixed cadence (default 60s) and is the **only**
writer of `idle_since` and `archived_since` timestamps — manual
archive via `ConversationService.archive` only flips the row into
`idle` without stamping a timestamp. Two transitions in scope:

* `active → idle` — `last_activity_at < now - idle_after_seconds`.
* `idle → archived` — `idle_since < now - archive_after_seconds`.

The sweep is composed of two pieces that share a clock seam so a
test can drive both the candidate query and the per-row atomic
write with the same `now`:

1. `ConversationLifecycleService.run_once()` — the idempotent
   work loop. Reads candidates, flips them one row at a time via
   the repository's `mark_idle` / `mark_archived`. Returns a
   `LifecycleSweepResult` carrying the counts so callers (the
   lifespan scheduler, the test harness) can log them.
2. `ConversationLifecycleScheduler` — a thin asyncio wrapper that
   drives `run_once` on the configured cadence. The lifespan
   starts it during `lifespan` and awaits `stop()` on shutdown so
   the task is cancelled cleanly.

The sweep never raises on a per-row failure — a transient Mongo
hiccup on one row is logged and the next tick will retry. The
return type carries the per-row failure count so the test seam
can assert it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.db.schemas import Conversation
from app.repositories.base import utcnow
from app.repositories.conversations import ConversationRepository
from app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseCounts:
    """Per-phase sweep counters (`flipped`, `failed`, `scanned`).

    The two sweep phases (idle / archive) share this shape;
    `LifecycleSweepResult` flattens both into the public result.
    """

    flipped: int = 0
    failed: int = 0
    scanned: int = 0


@dataclass(frozen=True)
class LifecycleSweepResult:
    """Outcome of one `ConversationLifecycleService.run_once` pass.

    Both counters are integers the lifespan logs as structured
    metadata; the test seam asserts on them. `failed_idle` /
    `failed_archive` capture per-row write failures (e.g. a row
    that raced into `active` between the candidate scan and the
    per-row write) so a single bad row doesn't fail the whole
    sweep.
    """

    idle: PhaseCounts = PhaseCounts()
    archive: PhaseCounts = PhaseCounts()

    # Convenience properties so existing call sites read flatly.
    @property
    def idle_flipped(self) -> int:
        return self.idle.flipped

    @property
    def archived_flipped(self) -> int:
        return self.archive.flipped

    @property
    def failed_idle(self) -> int:
        return self.idle.failed

    @property
    def failed_archive(self) -> int:
        return self.archive.failed

    @property
    def scanned_idle_candidates(self) -> int:
        return self.idle.scanned

    @property
    def scanned_archive_candidates(self) -> int:
        return self.archive.scanned


class ConversationLifecycleService:
    """Stateless sweep body — single `run_once` is the only public method.

    Holds a `ConversationRepository` handle plus the configured
    thresholds; the lifespan instantiates one and the scheduler
    wraps it. The clock is injectable so tests can drive the
    threshold boundaries deterministically without sleeping. The
    default clock is `app.repositories.base.utcnow` — the same
    millisecond-precision naive-UTC clock the repositories stamp
    rows with, so threshold comparisons never mix clock models.
    """

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        idle_after_seconds: int,
        archive_after_seconds: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._conversations = conversation_repository
        self._idle_after = timedelta(seconds=idle_after_seconds)
        self._archive_after = timedelta(seconds=archive_after_seconds)
        self._now: Callable[[], datetime] = clock

    async def run_once(self) -> LifecycleSweepResult:
        """One sweep pass: flip stale `active` → `idle`, then stale `idle` → `archived`.

        The two phases run sequentially so an `idle → archived`
        transition never interleaves with a fresh `active → idle`
        flip on the same row. The candidate scan returns a list;
        the per-row write is atomic (`mark_idle` /
        `mark_archived`) and skips rows whose status changed under
        us, logging at debug level rather than failing the whole
        sweep.
        """
        now = self._now()
        idle_threshold = now - self._idle_after
        archive_threshold = now - self._archive_after

        idle_counts = await self._sweep_phase(
            label="idle",
            now=now,
            threshold=idle_threshold,
            candidates=await self._conversations.list_idle_candidates(
                threshold=idle_threshold,
            ),
            marker=self._conversations.mark_idle,
        )
        archive_counts = await self._sweep_phase(
            label="archive",
            now=now,
            threshold=archive_threshold,
            candidates=await self._conversations.list_archive_candidates(
                threshold=archive_threshold,
            ),
            marker=self._conversations.mark_archived,
        )

        return LifecycleSweepResult(idle=idle_counts, archive=archive_counts)

    async def _sweep_phase(
        self,
        *,
        label: Literal["idle", "archive"],
        now: datetime,
        threshold: datetime,
        candidates: list[Conversation],
        marker: Callable[..., Awaitable[Conversation]],
    ) -> PhaseCounts:
        """Flip every candidate older than `threshold` via `marker`.

        One implementation for both sweep phases: iterate the
        pre-fetched candidates, atomically apply the phase's
        `mark_*` repository write, count per-row failures without
        aborting the pass, and emit a single structured summary
        line when anything actually moved.
        """
        flipped = 0
        failed = 0
        for candidate in candidates:
            try:
                await marker(candidate.id, now=now)
                flipped += 1
            except Exception as exc:  # noqa: BLE001 — sweep must not crash
                # Per-row failure: the row may have raced into a
                # different status between the candidate scan and
                # this write, or Mongo returned a transient error.
                # Log and let the next tick retry.
                failed += 1
                logger.debug(
                    "lifecycle: failed to mark conversation %s %s: %s",
                    candidate.id,
                    label,
                    exc,
                )
        if flipped or failed:
            logger.info(
                "lifecycle: %s sweep flipped=%d failed=%d candidates=%d threshold=%s",
                label,
                flipped,
                failed,
                len(candidates),
                threshold.isoformat(),
            )
        return PhaseCounts(
            flipped=flipped, failed=failed, scanned=len(candidates)
        )


class ConversationLifecycleScheduler:
    """Asyncio driver that ticks `ConversationLifecycleService.run_once`.

    The scheduler is started by the FastAPI lifespan (see
    `app.main.create_app.lifespan`) and cancelled on shutdown.
    `start()` is idempotent — calling it twice does not spawn a
    second task. The sweep cadence is taken from the passed
    `Settings` (`conversation_lifecycle_scan_interval_seconds`) so
    an operator can dial it without touching the service code.

    The `enabled` switch (`conversation_lifecycle_enabled`) lets
    tests and single-shot CLI runs skip the background task
    entirely; `start()` becomes a no-op when the switch is off.
    """

    def __init__(
        self,
        *,
        service: ConversationLifecycleService,
        scan_interval_seconds: float,
        enabled: bool = True,
    ) -> None:
        self._service = service
        self._interval = scan_interval_seconds
        self._enabled = enabled
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @classmethod
    def from_settings(
        cls,
        *,
        settings: Settings,
        conversation_repository: ConversationRepository,
    ) -> ConversationLifecycleScheduler:
        """Build a scheduler wired against the configured thresholds.

        Convenience constructor used by the lifespan — accepts a
        fully-resolved `Settings` instance and a repository, builds
        the inner service, and returns the scheduler. Tests build
        the service directly to inject a fake clock.
        """
        service = ConversationLifecycleService(
            conversation_repository=conversation_repository,
            idle_after_seconds=settings.conversation_idle_after_seconds,
            archive_after_seconds=settings.conversation_archive_after_seconds,
        )
        return cls(
            service=service,
            scan_interval_seconds=settings.conversation_lifecycle_scan_interval_seconds,
            enabled=settings.conversation_lifecycle_enabled,
        )

    async def start(self) -> None:
        """Begin the periodic sweep. No-op when disabled or already running.

        The task runs until `stop()` is awaited (or the loop is
        cancelled). Each tick calls `run_once` and sleeps for the
        configured interval; a slow sweep tick sleeps for the
        full interval regardless of how long the work took, which
        is the documented contract (overlap is harmless because the
        sweep is idempotent).
        """
        if not self._enabled:
            logger.info("lifecycle scheduler: disabled by settings, not starting")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name="conversation-lifecycle-scheduler",
        )
        logger.info(
            "lifecycle scheduler: started, interval=%.1fs",
            self._interval,
        )

    async def stop(self) -> None:
        """Signal the loop to exit and await the task's clean shutdown.

        Safe to call when the scheduler was never started (no-op).
        """
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("lifecycle scheduler: stopped")

    @property
    def is_running(self) -> bool:
        """True iff a sweep loop task is currently scheduled or running."""
        return self._task is not None and not self._task.done()

    async def run_once(self) -> LifecycleSweepResult:
        """Expose the service seam — useful for tests and admin tooling."""
        return await self._service.run_once()

    async def _loop(self) -> None:
        """Body of the scheduler task — ticks until `stop_event` is set."""
        while not self._stop_event.is_set():
            try:
                await self._service.run_once()
            except Exception as exc:  # noqa: BLE001 — loop must not crash
                # A transient sweep failure (Mongo down, etc.) is
                # logged and the loop continues on the next tick.
                # Crashing the loop would freeze the whole
                # lifecycle — better to skip one tick and retry.
                logger.warning(
                    "lifecycle scheduler: sweep tick failed: %s", exc
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
            except TimeoutError:
                # Expected: the interval elapsed, run again.
                continue


# Public re-export so callers can `from app.conversations.lifecycle import`
# the dataclass + scheduler + service without reaching into private names.
__all__ = [
    "ConversationLifecycleScheduler",
    "ConversationLifecycleService",
    "LifecycleSweepResult",
]