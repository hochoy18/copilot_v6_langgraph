"""Domain errors raised by the repository layer.

Repositories translate MongoDB-level outcomes (`DuplicateKeyError`,
`None` from `find_one`) into domain exceptions that the FastAPI global
handler (see `app.exceptions`) renders as a uniform error envelope.
The repository layer never raises a generic `Exception` — only these
classes — so the error contract on the wire is predictable.
"""
from __future__ import annotations

from fastapi import status

from app.exceptions import AppError


class NotFoundError(AppError):
    """Raised when a `find_one` returns `None` for a lookup that must exist."""

    code = "not_found"
    message_zh = "资源不存在"
    message_en = "Resource not found"
    http_status = status.HTTP_404_NOT_FOUND


class DuplicateKeyError(AppError):
    """Raised when a unique index rejects an insert / update.

    Translates `pymongo.errors.DuplicateKeyError` into the unified
    error shape so callers can render an i18n'd message instead of
    parsing the underlying driver's string.
    """

    code = "duplicate_key"
    message_zh = "唯一键冲突"
    message_en = "Duplicate key"
    http_status = status.HTTP_409_CONFLICT


class ValidationError(AppError):
    """Raised when repository-side invariants are violated.

    Example: trying to insert a `User` with `source='sso'` but no
    `sso_subject`. The Pydantic models catch most input errors at the
    boundary; this is the last-line guard for invariants that span
    multiple fields.
    """

    code = "validation_error"
    message_zh = "参数校验失败"
    message_en = "Validation failed"
    http_status = status.HTTP_400_BAD_REQUEST
