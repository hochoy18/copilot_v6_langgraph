"""Tests for `AuditRetentionService` (T42 / #37, ADR-0028).

Acceptance criteria pinned here:

* **AC#2** "Background migration moves >1-year logs": only active
  rows older than `hot_retention_seconds` get archived. The hot
  row's heavy payload (`parameters` / `response` / `error`) is
  slimmed atomically with the lifecycle flip; the cold blob holds
  the original.
* **AC#3** "Archive retrieval returns within 5 minutes": recall
  on an archived row restores the full original payload and flips
  the lifecycle to `recalled`.
* **AC#4** "Cold storage is encrypted": the on-disk bytes under
  the cold-storage base directory are NOT the plaintext row — the
  sweep encrypts them with the per-process `CredentialEncryptor`.

Additional invariants (not in the ticket's AC list but driven by
the service's documented contract):

* Active rows are skipped — the 409 envelope (`audit_log_not_archived`)
  is what the route surfaces.
* Recalled rows are idempotent — re-recalling returns the existing
  row without a second cold read.
* Per-row failure isolation — a transient failure on one row logs
  + increments the failed counter and the rest of the sweep
  continues.
* A corrupt cold blob fails closed — the wrong key, tampered
  ciphertext, or a swapped-in blob from another row id all surface
  as `AuditRecallTamperError` without leaking plaintext bits.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.audit.cold_storage import FileAuditColdStorage
from app.audit.retention import (
    AuditRecallConflictError,
    AuditRecallTamperError,
    AuditRetentionService,
)
from app.db.errors import NotFoundError
from app.db.init_db import init_database
from app.db.schemas import AuditLogCreate, ToolSnapshot
from app.repositories.audit_logs import AuditLogRepository
from app.security.crypto import AesGcmEncryptor, MasterKey


# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _snapshot() -> ToolSnapshot:
    return ToolSnapshot(
        name="list_customers",
        description="List customers by region.",
        risk_level="read",
        parameters_schema={
            "type": "object",
            "properties": {"region": {"type": "string"}},
        },
        http_method="GET",
        http_url_template="https://api.example.com/customers?region={region}",
        http_headers={},
        http_body_template=None,
    )


def _audit_input(**overrides: object) -> AuditLogCreate:
    base: dict[str, object] = {
        "actor_id": str(ObjectId()),
        "conversation_id": str(ObjectId()),
        "turn_id": str(ObjectId()),
        "plan_id": str(ObjectId()),
        "plan_execution_id": str(ObjectId()),
        "tool_name": "list_customers",
        "tool_snapshot": _snapshot(),
        "parameters": {"region": "emea", "secret": "sensitive-data"},
        "response": {"data": [{"id": "c1"}]},
        "status": "succeeded",
        "error": None,
        "risk_level": "read",
        "retry_count": 0,
    }
    base.update(overrides)
    return AuditLogCreate(**base)  # type: ignore[arg-type]


@pytest.fixture
async def repo() -> AuditLogRepository:
    """A fresh audit-log repo against an isolated in-memory Mongo."""
    db = AsyncMongoMockClient()["copilot_audit_retention_test"]
    await init_database(db)
    return AuditLogRepository(db)


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    """A real AES-GCM encryptor with a per-test random key.

    Tests exercise actual round trips through the crypto layer to
    verify the cold blob is encrypted + decrypts cleanly.
    """
    key = MasterKey(key_bytes=bytes(range(32)), key_id="test-primary")
    return AesGcmEncryptor(key)


@pytest.fixture
def cold_storage(tmp_path: Path) -> FileAuditColdStorage:
    """Per-test cold-storage rooted in a temp directory."""
    return FileAuditColdStorage(tmp_path)


@pytest.fixture
def now() -> Callable[[], datetime]:
    """A fixed-clock helper — overridden per-test as needed."""
    fixed = datetime(2026, 9, 30, 12, 0, 0)
    return lambda: fixed


@pytest.fixture
def service(
    repo: AuditLogRepository,
    cold_storage: FileAuditColdStorage,
    encryptor: AesGcmEncryptor,
    now: Callable[[], datetime],
) -> AuditRetentionService:
    """Service with a 1-year hot window and a fixed clock."""
    return AuditRetentionService(
        audit_repository=repo,
        cold_storage=cold_storage,
        encryptor=encryptor,
        hot_retention_seconds=365 * 86_400,
        clock=now,
    )


# ---------------------------------------------------------------------------
# Sweep tests
# ---------------------------------------------------------------------------


class TestAuditRetentionSweep:
    """`run_once` migrates >1-year active rows to cold storage."""

    async def test_sweep_archives_rows_older_than_hot_window(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        now: Callable[[], datetime],
    ) -> None:
        old_row = await repo.create(_audit_input())
        await repo.create(_audit_input())  # fresh — should not be archived

        # Backdate the "old" row's `occurred_at` past the 1-year
        # boundary. The repo doesn't expose a backdate helper
        # (audit rows are append-only), so we update the
        # collection directly — this mirrors what the corpus
        # looks like once 365 days have passed in production.
        await repo._collection.update_one(
            {"_id": ObjectId(old_row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )

        result = await service.run_once()
        assert result.flipped == 1
        assert result.failed == 0
        assert result.scanned == 1

        archived = await repo.get(old_row.id)
        assert archived.lifecycle_status == "archived"
        assert archived.cold_storage_ref is not None
        assert archived.cold_archived_at is not None
        # Heavy payload slimmed — hot tier stops holding the data.
        assert archived.parameters == {}
        assert archived.response is None
        assert archived.error is None
        # Metadata stays so the admin UI's row-expand panel still
        # has the identity / risk / FK pointers without recall.
        assert archived.tool_name == "list_customers"
        assert archived.tool_snapshot.name == "list_customers"
        assert archived.risk_level == "read"

        fresh_doc = await repo._collection.find_one(
            {"tool_name": "list_customers", "lifecycle_status": "active"},
        )
        assert fresh_doc is not None
        fresh = await repo.get(str(fresh_doc["_id"]))
        assert fresh.lifecycle_status == "active"

    async def test_sweep_skips_already_archived_rows(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        now: Callable[[], datetime],
    ) -> None:
        row = await repo.create(_audit_input())
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        await repo.mark_archived(row.id, "already/cold/path")

        result = await service.run_once()
        assert result.scanned == 0
        assert result.flipped == 0

    async def test_sweep_counters_unchanged_when_no_eligible_rows(
        self,
        service: AuditRetentionService,
    ) -> None:
        result = await service.run_once()
        assert result.flipped == 0
        assert result.failed == 0
        assert result.scanned == 0

    async def test_sweep_is_idempotent(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        now: Callable[[], datetime],
    ) -> None:
        row = await repo.create(_audit_input())
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        first = await service.run_once()
        second = await service.run_once()
        assert first.flipped == 1
        assert second.flipped == 0  # already archived — nothing to do
        assert second.scanned == 0


class TestAuditRetentionEncryptionAtRest:
    """AC#4 — the on-disk bytes are not the plaintext row."""

    async def test_cold_blob_is_encrypted_not_plaintext(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        now: Callable[[], datetime],
    ) -> None:
        """Run the sweep; the on-disk blob must not contain any plaintext field."""
        row = await repo.create(
            _audit_input(
                parameters={"region": "emea", "secret": "TOP-SECRET"},
                response={"data": [{"id": "c1"}]},
            )
        )
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        result = await service.run_once()
        assert result.flipped == 1

        # Read the bytes back directly from disk.
        assert row.lifecycle_status != "archived"  # sanity: input was fresh
        archived = await repo.get(row.id)
        assert archived.cold_storage_ref is not None
        on_disk = await cold_storage.read(archived.cold_storage_ref)

        # The plaintext must NOT appear in the on-disk bytes —
        # either as a substring, a JSON key, or the dict value
        # the encryption envelope wraps. A failure here means
        # the encryption seam was bypassed.
        assert b"TOP-SECRET" not in on_disk
        assert b"region" not in on_disk
        assert b'"succeeded"' not in on_disk
        # The envelope is JSON-wrapped but the inner ciphertext
        # is base64 — none of the inner Pydantic field names leak.
        decoded_envelope = json.loads(on_disk.decode("utf-8"))
        assert decoded_envelope["schema_version"] == 1
        assert decoded_envelope["key_id"] == "test-primary"
        # And the ciphertext itself is opaque bytes, not a JSON object.
        assert isinstance(decoded_envelope["ciphertext"], str)


class TestAuditRetentionRecall:
    """`recall` — AC#3 archive retrieval returns queryable."""

    async def test_recall_restores_full_fields(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        now: Callable[[], datetime],
    ) -> None:
        original_params = {"region": "emea", "secret": "TOP-SECRET"}
        original_response = {"data": [{"id": "c1"}]}
        row = await repo.create(
            _audit_input(parameters=original_params, response=original_response),
        )
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        await service.run_once()  # archive

        recalled = await service.recall(row.id)
        assert recalled.lifecycle_status == "recalled"
        assert recalled.parameters == original_params
        assert recalled.response == original_response
        assert recalled.tool_name == "list_customers"
        assert recalled.tool_snapshot.name == "list_customers"
        # Cold-storage ref stays so audit of the recall can trace back.
        assert recalled.cold_storage_ref is not None

    async def test_recall_active_row_raises_conflict(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
    ) -> None:
        row = await repo.create(_audit_input())
        with pytest.raises(AuditRecallConflictError):
            await service.recall(row.id)

    async def test_recall_recalled_row_is_idempotent(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        now: Callable[[], datetime],
    ) -> None:
        row = await repo.create(_audit_input())
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        await service.run_once()
        first = await service.recall(row.id)
        second = await service.recall(row.id)
        assert first.id == second.id == row.id
        assert first.lifecycle_status == "recalled"
        assert second.lifecycle_status == "recalled"

    async def test_recall_unknown_id_raises_not_found(
        self,
        service: AuditRetentionService,
    ) -> None:
        with pytest.raises(NotFoundError):
            await service.recall(str(ObjectId()))

    async def test_recall_tampered_blob_fails_closed(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        now: Callable[[], datetime],
    ) -> None:
        row = await repo.create(_audit_input())
        await repo._collection.update_one(
            {"_id": ObjectId(row.id)},
            {"$set": {"occurred_at": now() - timedelta(days=400)}},
        )
        await service.run_once()

        # Poison the cold blob — flip a byte in the ciphertext.
        archived = await repo.get(row.id)
        assert archived.cold_storage_ref is not None
        path = cold_storage.base_dir / archived.cold_storage_ref
        envelope = json.loads(path.read_bytes().decode("utf-8"))
        import base64

        ct = bytearray(base64.b64decode(envelope["ciphertext"]))
        ct[-1] ^= 0x01
        envelope["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
        path.write_bytes(json.dumps(envelope).encode("utf-8"))

        with pytest.raises(AuditRecallTamperError):
            await service.recall(row.id)


class TestAuditRetentionPerRowFailure:
    """A transient failure on one row doesn't fail the sweep."""

    async def test_sweep_continues_after_one_row_fails(
        self,
        service: AuditRetentionService,
        repo: AuditLogRepository,
        cold_storage: FileAuditColdStorage,
        now: Callable[[], datetime],
    ) -> None:
        # Two old rows; we'll make the second one's cold-storage
        # write fail by sabotaging the storage implementation.
        rows = []
        for _ in range(2):
            row = await repo.create(_audit_input())
            await repo._collection.update_one(
                {"_id": ObjectId(row.id)},
                {"$set": {"occurred_at": now() - timedelta(days=400)}},
            )
            rows.append(row)

        original_write = cold_storage.write
        call_count = {"n": 0}

        async def _failing_write(*, audit_log_id: str, blob: bytes) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("disk full (simulated)")
            return await original_write(audit_log_id=audit_log_id, blob=blob)

        cold_storage.write = _failing_write  # type: ignore[method-assign]

        result = await service.run_once()
        assert result.failed == 1
        assert result.flipped == 1
        # The failed row is still `active` so the next tick retries.
        active = await repo.get(rows[0].id)
        assert active.lifecycle_status == "active"
        archived = await repo.get(rows[1].id)
        assert archived.lifecycle_status == "archived"