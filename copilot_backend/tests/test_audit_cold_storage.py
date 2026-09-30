"""Tests for `FileAuditColdStorage` (T42 / #37, ADR-0048).

AC-driven coverage:

* **Encryption at rest** lives one layer up in the retention
  service; this test only checks the storage is a faithful byte
  sink that doesn't tamper with the blob it was handed. The
  *actual* encryption-at-rest assertion is in
  `test_audit_retention_service.py` — on-disk bytes must not be
  the plaintext when the retention service writes them.
* **Roundtrip**: write → read returns the same bytes.
* **Missing ref**: a ref that was never written raises
  `AuditColdStorageError` so the recall path surfaces an actionable
  error.
* **Path-traversal guard**: a malformed ref can't escape the base
  directory even if `cold_storage_ref` was poisoned by a manual
  Mongo edit.
* **Lazy base-dir creation**: the constructor doesn't touch the
  filesystem; the directory is created on the first write.
"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.audit.cold_storage import (
    AuditColdStorageError,
    FileAuditColdStorage,
)


def _make_id() -> str:
    """Return a 24-hex-char audit-log id for tests."""
    # 12 random bytes → 24 hex chars, matches `bson.ObjectId` shape.
    return secrets.token_hex(12)


@pytest.fixture
def storage(tmp_path: Path) -> FileAuditColdStorage:
    """A fresh cold-storage rooted in a per-test temp directory."""
    return FileAuditColdStorage(tmp_path)


class TestFileAuditColdStorageRoundtrip:
    """Write → read returns the same bytes; the ref is stable."""

    async def test_write_then_read_returns_original_bytes(
        self, storage: FileAuditColdStorage
    ) -> None:
        audit_log_id = _make_id()
        blob = b"hello audit cold storage"
        ref = await storage.write(audit_log_id=audit_log_id, blob=blob)
        assert await storage.read(ref) == blob

    async def test_ref_shape_is_canonical(self, storage: FileAuditColdStorage) -> None:
        """Ref is `{shard}/{rest}.bin` — two + twenty-two + 4 chars."""
        audit_log_id = _make_id()
        ref = await storage.write(audit_log_id=audit_log_id, blob=b"x")
        assert len(ref) == 2 + 1 + 22 + 4  # 'xx/yyyy.bin'
        shard, _, tail = ref.partition("/")
        assert len(shard) == 2
        assert shard == audit_log_id[:2]
        assert tail.endswith(".bin")
        assert tail[: -len(".bin")] == audit_log_id[2:]


class TestFileAuditColdStorageErrors:
    """Failure modes the retention service relies on."""

    async def test_read_missing_ref_raises(
        self, storage: FileAuditColdStorage
    ) -> None:
        audit_log_id = _make_id()
        ref = await storage.write(audit_log_id=audit_log_id, blob=b"x")
        # Simulate a lost cold blob (the row's ref was set, but
        # the file got GC'd by an operator).
        (storage.base_dir / ref).unlink()
        with pytest.raises(AuditColdStorageError):
            await storage.read(ref)

    async def test_read_malformed_ref_raises(
        self, storage: FileAuditColdStorage
    ) -> None:
        # Path-traversal attempt — the validator must reject before
        # any filesystem access so an attacker can't read
        # `../../etc/passwd` even if they can poison the column.
        for bad in (
            "",
            "..",
            "../etc/passwd",
            "/etc/passwd",
            "no-slash.bin",
            "zz/nothex.bin",
            "zzzzzzzzzzzzzzzzzzzzzzzz.bin",  # missing shard separator
            "aa/zzzzzzzzzzzzzzzzzzzzzzz",  # missing .bin suffix
        ):
            with pytest.raises(AuditColdStorageError):
                await storage.read(bad)

    async def test_write_rejects_non_hex_audit_log_id(
        self, storage: FileAuditColdStorage
    ) -> None:
        with pytest.raises(AuditColdStorageError):
            await storage.write(audit_log_id="not-an-oid", blob=b"x")

    async def test_write_rejects_empty_blob(
        self, storage: FileAuditColdStorage
    ) -> None:
        with pytest.raises(AuditColdStorageError):
            await storage.write(audit_log_id=_make_id(), blob=b"")


class TestFileAuditColdStorageLazyBaseDir:
    """The constructor doesn't touch the filesystem."""

    def test_constructor_does_not_create_base_dir(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "nested" / "subdir" / "cold"
        # `Path.resolve()` (called by the ctor) doesn't create the
        # directory. We assert that explicitly — a deployment that
        # mounts `audit_cold_storage_dir` after the lifespan boots
        # must not fail at startup time.
        FileAuditColdStorage(target)
        assert not target.exists()

    async def test_write_creates_missing_parents(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "deep" / "nested"
        storage = FileAuditColdStorage(target)
        await storage.write(audit_log_id=_make_id(), blob=b"hello")
        # The shard subdir under the base was created on first write.
        assert any(target.iterdir())