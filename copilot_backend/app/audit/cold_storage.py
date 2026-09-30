"""Audit cold-storage surface (T42 / #37, ADR-0028).

A thin byte-sink Protocol with two implementations:

* `FileAuditColdStorage` — the local-filesystem default. Each
  archived row is one file under `base_dir`, lazily created on the
  first write so deployments that disable the sweep never touch
  the disk.
* Production swaps in an S3 / OSS / KMS-backed client without
  touching the retention service.

ADR-0028 defers the storage *form* to the deployment and fixes only
the contract: "system specifies the interface, doesn't lock down the
implementation". The Protocol below is the seam; the retention service
sees only `write(blob) -> ref` and `read(ref) -> blob`.

Encryption at rest is enforced **one layer up** in
`AuditRetentionService` (re-using the per-process
`CredentialEncryptor`). Keeping the storage interface
encryption-agnostic means an S3 deployment can opt into SSE-KMS
without the retention code changing — and the encryption-at-rest
property is still verifiable in tests by inspecting the on-disk
bytes against the original blob.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol


class AuditColdStorage(Protocol):
    """Async byte store for `audit_logs` cold blobs.

    The retention service sees only this Protocol — never a concrete
    implementation — so a deployment can swap in S3 / OSS / KMS-
    backed storage without touching call sites. Implementations must
    be safe under concurrent access from the sweep loop and the
    admin recall endpoint running on the same process.
    """

    async def write(self, *, audit_log_id: str, blob: bytes) -> str:
        """Persist `blob` for `audit_log_id` and return an opaque ref.

        The ref is opaque to the retention service; it's stored
        verbatim on the audit row's `cold_storage_ref` column. Two
        writes for the same `audit_log_id` return different refs
        (the retention service never overwrites — the unique
        constraint is implicit in the audit-log PK).

        Raises:
            AuditColdStorageError: write failed (disk full, network
                down, permission denied). Callers log + skip the row.
        """
        ...

    async def read(self, ref: str) -> bytes:
        """Return the blob previously written under `ref`.

        Raises:
            AuditColdStorageError: the ref points at a missing or
                unreadable blob. The retention service surfaces this
                as a recall failure so the admin sees an actionable
                error rather than silent data loss.
        """
        ...


class AuditColdStorageError(Exception):
    """Raised when cold-storage I/O fails for any reason.

    Distinct from `app.exceptions.AppError` because failures here
    are operational, not domain-level. The retention service
    catches this exception, logs it, and increments a per-sweep
    failure counter — the lifespan scheduler retries on the next
    tick. The recall endpoint propagates the exception upward so
    the admin sees an explicit failure envelope.
    """

    def __init__(
        self,
        message: str,
        *,
        ref: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.ref = ref
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# Local-filesystem implementation — development / single-tenant default.
# ---------------------------------------------------------------------------


# Ref format: relative POSIX path under the base directory, e.g.
# "audit/{oid_hex}.bin". Keeping the ref format stable lets future
# relocations (move the file under a different prefix, copy the whole
# tree into S3) leave the persisted `cold_storage_ref` column valid.
#
# The path layout groups rows into 256-bucket shards (`{xx}/{rest}.bin`)
# so a single directory never accumulates millions of sibling files —
# ext4 / most filesystems start hurting past 10k entries per dir.
_PATH_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_PATH_REF_RE = re.compile(r"^[0-9a-fA-F]{2}/[0-9a-fA-F]{22}\.bin$")
_PATH_TEMPLATE = "{shard}/{rest}.bin"


class FileAuditColdStorage(AuditColdStorage):
    """Local-filesystem `AuditColdStorage`.

    One file per audit row, lazily created under `base_dir` on the
    first sweep tick. The directory tree never has to exist before
    `start()` — `write()` creates the missing parents.

    File I/O is dispatched through `asyncio.to_thread` so the sweep
    loop never blocks the event loop on a slow disk. Production
    deployments don't use this class (the retention service picks
    whatever backend matches the deployment target), but the local
    default keeps dev / single-tenant setups + tests working without
    a network dependency.
    """

    def __init__(self, base_dir: str | Path) -> None:
        """Bind to `base_dir`; the directory is created lazily on first write.

        The constructor does NOT touch the filesystem so lifespan
        builds succeed in environments where the cold-storage mount
        isn't ready yet (container start order, read-only mounts
        during probe, etc.). The first `write` call does the mkdir.
        """
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        """The absolute root directory this storage writes under."""
        return self._base_dir

    async def write(self, *, audit_log_id: str, blob: bytes) -> str:
        """Persist `blob` for `audit_log_id` and return its relative ref.

        The ref format is `{shard}/{rest}.bin` where `shard` is the
        first two hex chars of the id and `rest` is the remaining 22.
        This is intentionally stable: a future migration to S3 with
        the same key layout doesn't require rewriting the persisted
        `cold_storage_ref` column.
        """
        if not _PATH_ID_RE.match(audit_log_id):
            raise AuditColdStorageError(
                f"invalid audit_log_id for cold-storage write: {audit_log_id!r}",
                ref=None,
            )
        if not blob:
            raise AuditColdStorageError(
                "refusing to write an empty blob",
                ref=None,
            )

        rel_path = _PATH_TEMPLATE.format(shard=audit_log_id[:2], rest=audit_log_id[2:])
        target = self._base_dir / rel_path
        ref = rel_path

        def _do_write() -> None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # O_CREAT | O_EXCL prevents accidental overwrite of a
                # blob from a previous sweep run; the retention
                # service treats a collision as a per-row skip.
                target.write_bytes(blob)
            except OSError as exc:
                raise AuditColdStorageError(
                    f"cold-storage write failed for {ref}",
                    ref=ref,
                    cause=exc,
                ) from exc

        await asyncio.to_thread(_do_write)
        return ref

    async def read(self, ref: str) -> bytes:
        """Return the bytes previously written under `ref`.

        The ref is interpreted as a relative path under
        `base_dir`. The path is revalidated on read so a tampered
        `cold_storage_ref` column can't be coerced into reading
        arbitrary files (e.g. `../../etc/passwd`).
        """
        self._validate_ref_path(ref)

        def _do_read() -> bytes:
            target = self._base_dir / ref
            try:
                data = target.read_bytes()
            except FileNotFoundError as exc:
                raise AuditColdStorageError(
                    f"cold-storage ref not found: {ref}",
                    ref=ref,
                    cause=exc,
                ) from exc
            except OSError as exc:
                raise AuditColdStorageError(
                    f"cold-storage read failed for {ref}",
                    ref=ref,
                    cause=exc,
                ) from exc
            if not data:
                raise AuditColdStorageError(
                    f"cold-storage blob is empty: {ref}",
                    ref=ref,
                )
            return data

        return await asyncio.to_thread(_do_read)

    # -- Internal --------------------------------------------------------

    @staticmethod
    def _validate_ref_path(ref: str) -> None:
        """Reject refs that don't conform to the canonical layout.

        `read` calls this before any I/O so a poisoned `cold_storage_ref`
        column — whether set by a misbehaving admin script, a manual
        Mongo edit, or an attacker — can't escape the base directory.
        The check rejects:

        * empty / non-string refs;
        * path separators in the middle (`../`, `\\`) — the canonical
          layout is fixed at exactly two levels (`xx/rest.bin`);
        * non-hex characters.

        Mismatches raise `AuditColdStorageError` so the recall path
        surfaces an actionable error rather than leaking bytes from a
        filesystem path the admin didn't expect.
        """
        if not ref or not _PATH_REF_RE.match(ref):
            raise AuditColdStorageError(
                f"unexpected ref shape: {ref!r}",
                ref=ref,
            )


# Public re-export for the protocol + impl pair.
__all__ = [
    "AuditColdStorage",
    "AuditColdStorageError",
    "FileAuditColdStorage",
]