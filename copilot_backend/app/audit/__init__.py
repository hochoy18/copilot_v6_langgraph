"""Audit retention + cold-storage surface (T42 / #37, ADR-0028).

This package owns the second half of the audit-log lifecycle:

* `cold_storage` — the swappable byte sink that holds archived audit
  rows. The local-filesystem implementation is the development /
  single-tenant default; a production deployment swaps in an S3 /
  OSS / KMS-backed client without touching the retention service.
* `retention` — the daily sweep + recall orchestrator. Wired into
  the FastAPI lifespan as a background scheduler; route handlers
  call the recall seam synchronously to hit the P95 < 5-minute SLO.

The retention sweep *atomically* migrates a row's heavy payload
(`parameters` / `response` / `error`) to an `AuditColdStorage`
backend and flips the hot row into a slim tombstone (`{}` / `None`)
that points at the cold blob. Recall reads the blob, decrypts it,
and writes the payload back in one Mongo update.

Encryption at rest is non-negotiable (ADR-0002 inherited by ADR-0028);
the seam reuses the per-process `CredentialEncryptor` (AES-256-GCM,
AEAD) with the audit-log id as `aad` so a swapped-in blob fails to
decrypt rather than leaking plaintext into another row's hot record.
"""

from app.audit.cold_storage import (
    AuditColdStorage,
    AuditColdStorageError,
    FileAuditColdStorage,
)

__all__ = [
    "AuditColdStorage",
    "AuditColdStorageError",
    "FileAuditColdStorage",
]