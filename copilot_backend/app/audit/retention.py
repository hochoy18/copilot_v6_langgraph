"""Audit retention sweep + recall (T42 / #37, ADR-0028).

This module covers **acceptance criteria #2 / #3 / #4** of the
ticket:

* **AC#2** — the daily sweep (`AuditRetentionService.run_once`)
  migrates `audit_logs` rows older than `audit_hot_retention_seconds`
  from the hot tier (Mongo) to cold storage, leaving a slim
  tombstone behind.
* **AC#3** — `AuditRetentionService.recall(id)` hydrates an
  archived row back to queryable state. Local-filesystem cold is
  millisecond-fast; the round-trip is well inside ADR-0028's
  P95 < 5-minute SLO.
* **AC#4** — cold blobs are AES-256-GCM-encrypted with the
  per-process `CredentialEncryptor`; on-disk bytes are NOT the
  plaintext row (verified by
  `tests/test_audit_retention_service.py::test_cold_blob_is_encrypted_not_plaintext`).

**AC#1** ("Plan run completed audit is intact") is satisfied one
layer down by the audit-writing chain — the Plan Executor (T21 /
#18, see `app/tools/executor.py`) and the Plan-edit / reactivate
flows (T26 / T39, `app/conversations/service.py`) each call
`AuditLogRepository.create` for every Tool invocation. This module
is the **retention** half of the lifecycle, not the write half,
and intentionally does not touch the executor / service code.

Total retention + cold-side expiry
----------------------------------

ADR-0028 §5 places cold-side auto-expiry past `audit_cold_total_retention_seconds`
on the cold-storage backend (S3 lifecycle policy, OSS rule, etc.) —
this module does **not** GC cold blobs. Operators using the local
filesystem backend are responsible for an external cleanup
mechanism (cron + age filter on `audit_cold_storage_dir`).

Idempotency note
----------------

The sweep is idempotent at the row level: `list_archive_candidates`
filters on `lifecycle_status == "active"`, so already-archived
rows are invisible to subsequent ticks. If the cold-write step
succeeds but the `mark_archived` Mongo update fails, the next
tick re-encrypts + writes a new blob — the previous one becomes an
orphan (no `cold_storage_ref` points at it) and is subject to the
cold-side expiry above. The orphan-blob tolerance is acceptable
because (a) the disk cost is bounded by the sweep retry rate and
(b) cold-side TTL is the operator's policy anyway.

Sweep semantics
---------------

* `run_once` never raises on a per-row failure — a transient I/O
  or Mongo error is logged and the next tick retries the row
  (it's still in `active` because the atomic `mark_archived` is
  the final step). The result carries `failed` so the test seam
  can assert it.
* A row already in `archived` / `recalled` is invisible to the
  sweep — the candidate query filters on `lifecycle_status ==
  "active"`.
* The sweep processes up to `batch_size` rows per tick. Larger
  batches amortise Mongo round-trips but extend the per-tick
  latency. The default (200) keeps a single tick well under the
  1-minute sweep-cadence budget on a 100k-row corpus.

Encryption envelope
-------------------

Every cold blob is an `EncryptedPayload` serialised as JSON:

    {
      "schema_version": 1,
      "key_id": <str>,          # identifies which key sealed the blob
      "nonce":  <base64 bytes>, # AES-GCM 12-byte nonce
      "ciphertext": <base64 bytes>,
    }

The ciphertext is the audit row's full `model_dump(mode="json")`
serialisation, encrypted with `aad=audit_log_id` so a swap-in blob
fails to decrypt against another row's id (AEAD binding).

Why the envelope is one layer above the encryptor's wire shape:
`EncryptedPayload` is the credential-store contract (ADR-0002) and
mustn't carry audit-specific fields. The retention service owns the
envelope shape so a future v2 schema (compression, key rotation
header, etc.) can land without touching credential code.

Recall semantics
----------------

* `active` rows: recall is rejected — the row is already hot; a
  second copy wouldn't help. Surfaces as 409 (`audit_log_not_archived`).
* `archived` rows: read cold → decrypt → `restore_payload` (atomic
  restore + lifecycle flip).
* `recalled` rows: idempotent no-op return of the existing row —
  re-recalling an already-hydrated row is a no-op, not a 409.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import status

from app.audit.cold_storage import AuditColdStorage, AuditColdStorageError
from app.db.schemas import AuditLog
from app.exceptions import AppError
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.base import utcnow
from app.security.crypto import CredentialEncryptor, EncryptedPayload, EncryptionError
from app.settings import Settings

logger = logging.getLogger(__name__)


# Envelope schema version. Bump on incompatible changes to the
# blob format (compression, header additions, key-rotation hints).
# A mismatch on read raises `AuditRecallError` so the admin sees an
# actionable error rather than silent partial-decryption.
ENVELOPE_SCHEMA_VERSION = 1

# Default batch size for the sweep. See class docstring for the
# trade-off; tweak via the constructor for production tunings.
DEFAULT_BATCH_SIZE = 200


class AuditRetentionError(Exception):
    """Base class for retention-specific failures.

    Distinct from `AuditColdStorageError` (which is purely a
    cold-storage I/O error): this covers envelope decode failures
    and "everything I forgot" that comes from the service layer
    itself.
    """


class AuditRecallEnvelopeError(AuditRetentionError):
    """The cold blob's envelope couldn't be decoded or has a future schema.

    The on-disk format is incompatible (different `schema_version`)
    or malformed JSON / missing fields. Recall fails closed.
    """


class AuditRecallTamperError(AuditRetentionError):
    """The cold blob failed AEAD verification on recall.

    Either the wrong key, a tampered ciphertext, or a swapped-in
    blob from another audit log id. Recall fails closed — we never
    surface partial plaintext bits in an exception message
    (mirrors the credential-decrypt contract in `app.security.crypto`).
    """


class AuditRecallConflictError(AppError):
    """Recall was triggered on a row that is not in cold storage.

    `active` rows are already hot, so a recall is a contract error.
    Renders 409 with the standard error envelope; the admin UI
    can re-check the row's `lifecycle_status` from the response
    details.

    `recalled` rows are *not* in this category — re-recalling an
    already-hydrated row is idempotent, not a 409.
    """

    code = "audit_log_not_archived"
    message_zh = "该审计日志尚处于热存,无需调档"
    message_en = "Audit log is still in hot storage; nothing to recall"
    http_status = status.HTTP_409_CONFLICT


# ---------------------------------------------------------------------------
# Sweep result + counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRetentionSweepResult:
    """Outcome of one `AuditRetentionService.run_once` pass.

    `flipped` counts rows that successfully transitioned
    `active → archived`. `failed` counts per-row failures (cold
    storage error, decrypt error, Mongo write error) so a single
    bad row doesn't fail the whole sweep. `scanned` is the number
    of candidates the candidate query returned (== flipped + failed
    + "skipped because mid-flight concurrent write").
    """

    flipped: int = 0
    failed: int = 0
    scanned: int = 0


# ---------------------------------------------------------------------------
# Envelope helpers — keep the wire format localised.
# ---------------------------------------------------------------------------


def _serialize_envelope(payload: EncryptedPayload) -> bytes:
    """Encode an `EncryptedPayload` as JSON bytes for cold storage.

    Uses base64 for the byte fields so the JSON is portable across
    implementations (some Mongo / S3 sanity checks flag raw bytes
    in JSON columns). ASCII base64 keeps the JSON UTF-8-safe.
    """
    return json.dumps(
        {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "key_id": payload.key_id,
            "nonce": base64.b64encode(payload.nonce).decode("ascii"),
            "ciphertext": base64.b64encode(payload.ciphertext).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _deserialize_envelope(blob: bytes) -> EncryptedPayload:
    """Decode a cold-storage blob back into an `EncryptedPayload`.

    Raises:
        AuditRecallEnvelopeError: malformed JSON, missing fields, or
            a `schema_version` we don't understand.
    """
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditRecallEnvelopeError(
            f"cold-storage blob is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AuditRecallEnvelopeError("cold-storage envelope must be an object")
    schema_version = data.get("schema_version")
    if schema_version != ENVELOPE_SCHEMA_VERSION:
        raise AuditRecallEnvelopeError(
            f"unknown cold-storage envelope schema_version={schema_version!r}; "
            f"this build understands version {ENVELOPE_SCHEMA_VERSION}",
        )
    try:
        nonce_b64 = cast(str, data["nonce"])
        ciphertext_b64 = cast(str, data["ciphertext"])
        key_id = cast(str, data["key_id"])
        return EncryptedPayload(
            nonce=base64.b64decode(nonce_b64),
            ciphertext=base64.b64decode(ciphertext_b64),
            key_id=key_id,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AuditRecallEnvelopeError(
            f"cold-storage envelope missing required fields: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AuditRetentionService:
    """Stateless retention surface — sweep + recall.

    Holds an `AuditLogRepository`, an `AuditColdStorage` (Protocol),
    a `CredentialEncryptor` (the per-process one — ADR-0028 inherits
    ADR-0002's encryption standard), and the configured hot-tier
    window. The clock is injectable so tests can drive the threshold
    boundaries deterministically without sleeping.

    The retention service is the only place that encrypts and
    decrypts audit blobs. Callers (sweep loop, recall route) never
    see the `EncryptedPayload` directly — they get back parsed
    `AuditLog` rows.
    """

    def __init__(
        self,
        *,
        audit_repository: AuditLogRepository,
        cold_storage: AuditColdStorage,
        encryptor: CredentialEncryptor,
        hot_retention_seconds: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repo = audit_repository
        self._cold_storage = cold_storage
        self._encryptor = encryptor
        self._hot_window = timedelta(seconds=hot_retention_seconds)
        self._batch_size = batch_size
        self._now: Callable[[], datetime] = clock

    # -- Sweep ----------------------------------------------------------

    async def run_once(self) -> AuditRetentionSweepResult:
        """One sweep pass: encrypt + cold-store + tombstone every active row older than `hot_window`.

        The candidate scan returns up to `batch_size` rows. Each
        row is processed independently — a transient cold-storage
        or Mongo failure on one row logs + increments `failed` and
        the next tick retries the row. Because `mark_archived` is
        the final atomic write, a row that errors before that
        step is still in `active` and visible to the next sweep.
        """
        now = self._now()
        threshold = now - self._hot_window
        candidates = await self._repo.list_archive_candidates(
            threshold=threshold,
            limit=self._batch_size,
        )
        flipped = 0
        failed = 0
        for row in candidates:
            try:
                await self._archive_one(row)
                flipped += 1
            except Exception as exc:  # noqa: BLE001 — sweep must not crash
                # Per-row isolation: a transient cold-storage or
                # Mongo failure on one row is logged and the
                # sweep continues. The row is still `active`
                # because the atomic `mark_archived` is the final
                # step; the next tick retries it.
                failed += 1
                logger.warning(
                    "audit retention: failed to archive %s: %s",
                    row.id,
                    exc,
                )
        if flipped or failed:
            logger.info(
                "audit retention: sweep flipped=%d failed=%d candidates=%d threshold=%s",
                flipped,
                failed,
                len(candidates),
                threshold.isoformat(),
            )
        return AuditRetentionSweepResult(
            flipped=flipped, failed=failed, scanned=len(candidates)
        )

    async def _archive_one(self, row: AuditLog) -> None:
        """Encrypt + cold-write + tombstone one row.

        Failures at any step propagate to `run_once`, which logs
        and counts them. The atomic guarantee is in
        `mark_archived`: the row's lifecycle + tombstone slim is
        one Mongo write, so a row that errors before that write is
        still in `active` for the next tick.
        """
        blob_bytes = json.dumps(
            row.model_dump(mode="json"),
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._encryptor.encrypt(
            blob_bytes,
            aad=row.id.encode("utf-8"),
        )
        ref = await self._cold_storage.write(
            audit_log_id=row.id,
            blob=_serialize_envelope(payload),
        )
        # Slim the tombstone so the hot row stops holding the heavy
        # payload. `tool_snapshot` is kept (FK / display integrity
        # — the audit UI surfaces it on the row without recall).
        # `cold_storage_ref` is set by `mark_archived`.
        await self._repo.mark_archived(
            row.id,
            ref,
            tombstone_overrides={
                "parameters": {},
                "response": None,
                "error": None,
            },
        )

    # -- Recall ---------------------------------------------------------

    async def recall(self, audit_log_id: str) -> AuditLog:
        """Hydrate one archived row back to queryable hot state.

        Steps:

        1. `repo.get` — 404 (`NotFoundError`) for unknown ids.
        2. Lifecycle guard — `active` raises 409
           (`AuditRecallConflictError`); `recalled` returns the
           existing row idempotently.
        3. Read the cold blob (raises `AuditColdStorageError` if
           the ref is missing / unreadable — recall fails
           closed).
        4. Decrypt + JSON-decode the full original row.
        5. `repo.restore_payload` writes the recalled payload
           fields and flips the lifecycle atomically. The
           repository filter `{lifecycle_status: archived}` guards
           against a concurrent recall — second writer sees 404
           and treats it as "already recalled".

        Latency budget: the local-filesystem backend is
        millisecond-tier; the whole call is well inside the
        ADR-0028 P95 < 5-minute SLO even on a cold-mounted S3.
        """
        row = await self._repo.get(audit_log_id)
        if row.lifecycle_status == "active":
            raise AuditRecallConflictError(
                details={"audit_log_id": audit_log_id, "lifecycle_status": "active"},
            )
        if row.lifecycle_status == "recalled":
            return row
        if not row.cold_storage_ref:
            # Data-integrity guard — `archived` rows without a
            # cold-storage ref are corrupt. Surfacing an explicit
            # error is safer than silently returning the tombstone.
            raise AuditRetentionError(
                f"audit row {audit_log_id} archived without cold_storage_ref",
            )

        envelope = await self._read_envelope(row.cold_storage_ref)
        recalled_doc = await self._decrypt_envelope(envelope, row.id)
        payload_fields = _extract_payload_fields(recalled_doc)
        return await self._repo.restore_payload(audit_log_id, payload_fields)

    async def _read_envelope(self, ref: str) -> EncryptedPayload:
        try:
            blob = await self._cold_storage.read(ref)
        except AuditColdStorageError:
            raise
        return _deserialize_envelope(blob)

    async def _decrypt_envelope(
        self,
        payload: EncryptedPayload,
        audit_log_id: str,
    ) -> dict[str, Any]:
        try:
            plaintext = self._encryptor.decrypt(
                payload,
                aad=audit_log_id.encode("utf-8"),
            )
        except EncryptionError as exc:
            # AEAD verification failed — wrong key, tampered
            # ciphertext, or a swapped-in blob from another id.
            # Fail closed; never leak plaintext bits in the
            # exception.
            raise AuditRecallTamperError(
                f"cold-storage blob failed AEAD verification for {audit_log_id}",
            ) from exc
        try:
            decoded = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditRecallEnvelopeError(
                f"decrypted cold-storage blob is not valid JSON for {audit_log_id}",
            ) from exc
        if not isinstance(decoded, dict):
            raise AuditRecallEnvelopeError(
                f"decrypted cold-storage blob is not an object for {audit_log_id}",
            )
        return decoded


def _extract_payload_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Pick the slim-removed fields back out of the recalled JSON.

    `AuditLogRepository.restore_payload` writes whatever the
    service gives it; this helper picks only the fields the sweep
    slimmed (per `_archive_one`). Other audit-row columns
    (`tool_snapshot`, FKs, `risk_level`, etc.) are still on the
    hot row — the slim tombstone preserved them — so they don't
    need restoring.
    """
    fields: dict[str, Any] = {}
    for key in ("parameters", "response", "error"):
        if key in doc:
            fields[key] = doc[key]
    return fields


# ---------------------------------------------------------------------------
# Scheduler — mirrors `ConversationLifecycleScheduler` (T39 / #45).
# ---------------------------------------------------------------------------


class AuditRetentionScheduler:
    """Asyncio driver that ticks `AuditRetentionService.run_once`.

    The scheduler is started by the FastAPI lifespan (see
    `app.main.create_app.lifespan`) and cancelled on shutdown.
    `start()` is idempotent — calling it twice does not spawn a
    second task. The sweep cadence comes from the passed
    `Settings` (`audit_cold_sweep_interval_seconds`) so an
    operator can tighten it without touching the service code. The
    `enabled` switch (`audit_retention_enabled`) lets tests and
    single-shot CLI runs skip the background task entirely;
    `start()` becomes a no-op when the switch is off.
    """

    def __init__(
        self,
        *,
        service: AuditRetentionService,
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
        audit_repository: AuditLogRepository,
        cold_storage: AuditColdStorage,
        encryptor: CredentialEncryptor,
    ) -> AuditRetentionScheduler:
        """Build a scheduler wired against the configured retention window.

        Convenience constructor used by the lifespan — accepts a
        fully-resolved `Settings` instance and the three
        collaborators, builds the inner service, and returns the
        scheduler. Tests build the service directly to inject a
        fake clock.
        """
        service = AuditRetentionService(
            audit_repository=audit_repository,
            cold_storage=cold_storage,
            encryptor=encryptor,
            hot_retention_seconds=settings.audit_hot_retention_seconds,
            batch_size=DEFAULT_BATCH_SIZE,
        )
        return cls(
            service=service,
            scan_interval_seconds=settings.audit_cold_sweep_interval_seconds,
            enabled=settings.audit_retention_enabled,
        )

    async def start(self) -> None:
        """Begin the periodic sweep. No-op when disabled or already running."""
        if not self._enabled:
            logger.info("audit retention scheduler: disabled by settings, not starting")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name="audit-retention-scheduler",
        )
        logger.info(
            "audit retention scheduler: started, interval=%.1fs",
            self._interval,
        )

    async def stop(self) -> None:
        """Signal the loop to exit and await the task's clean shutdown."""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("audit retention scheduler: stopped")

    @property
    def is_running(self) -> bool:
        """True iff a sweep loop task is currently scheduled or running."""
        return self._task is not None and not self._task.done()

    async def run_once(self) -> AuditRetentionSweepResult:
        """Expose the service seam — useful for tests and admin tooling."""
        return await self._service.run_once()

    async def recall(self, audit_log_id: str) -> AuditLog:
        """Expose the service seam — used by the admin recall route."""
        return await self._service.recall(audit_log_id)

    @property
    def service(self) -> AuditRetentionService:
        """Expose the inner service — used by the route layer DI seam."""
        return self._service

    async def _loop(self) -> None:
        """Body of the scheduler task — ticks until `stop_event` is set."""
        while not self._stop_event.is_set():
            try:
                await self._service.run_once()
            except Exception as exc:  # noqa: BLE001 — loop must not crash
                logger.warning(
                    "audit retention scheduler: sweep tick failed: %s", exc
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
            except TimeoutError:
                continue


__all__ = [
    "AuditRecallConflictError",
    "AuditRecallEnvelopeError",
    "AuditRecallTamperError",
    "AuditRetentionError",
    "AuditRetentionScheduler",
    "AuditRetentionService",
    "AuditRetentionSweepResult",
    "DEFAULT_BATCH_SIZE",
    "ENVELOPE_SCHEMA_VERSION",
]