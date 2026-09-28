"""Auth-domain exceptions (T07 / #8).

The repository layer raises generic database-shaped errors
(`NotFoundError`, `DuplicateKeyError`, …). The auth layer needs
finer-grained errors so callers can render the right HTTP envelope:

* `RefreshTokenNotFoundError` — token absent (404 envelope).
* `RefreshTokenRevokedError` — token was revoked (rotation,
  logout, admin force-logout).
* `RefreshTokenExpiredError` — `expires_at` is in the past. The TTL
  index will purge the row eventually; this gives a sensible
  message in the meantime.
* `RefreshTokenReuseError` — a previously-rotated token was presented
  again. The service has already revoked the entire family; this
  signal lets the caller route the user to "re-authenticate" rather
  than "session expired".

All classes derive from `app.exceptions.AppError` so the global
exception handler renders them uniformly.
"""
from __future__ import annotations

from fastapi import status

from app.exceptions import AppError


class RefreshTokenNotFoundError(AppError):
    """Raised when a presented refresh token has no row at all.

    Distinct from `RefreshTokenRevokedError`: this means the token was
    never issued (typo, never issued, or manually inserted for testing
    without `issue(...)`). 404 keeps the auth envelope consistent with
    `NotFoundError`.
    """

    code = "refresh_token_not_found"
    message_zh = "刷新令牌不存在"
    message_en = "Refresh token not found"
    http_status = status.HTTP_404_NOT_FOUND


class RefreshTokenRevokedError(AppError):
    """Raised when a presented refresh token has `revoked_at` set.

    Either the user logged out, an admin force-logged them out, or an
    earlier refresh rotated it. The reuse path raises a sibling error
    (`RefreshTokenReuseError`) that the service has already-revoked
    the family for; plain revocations land here.
    """

    code = "refresh_token_revoked"
    message_zh = "刷新令牌已失效"
    message_en = "Refresh token revoked"
    http_status = status.HTTP_401_UNAUTHORIZED


class RefreshTokenExpiredError(AppError):
    """Raised when `expires_at` is in the past.

    The TTL index on `expires_at` will purge the row eventually; this
    error gives a human-readable 401 in the gap.
    """

    code = "refresh_token_expired"
    message_zh = "刷新令牌已过期"
    message_en = "Refresh token expired"
    http_status = status.HTTP_401_UNAUTHORIZED


class RefreshTokenReuseError(AppError):
    """Raised when a previously-rotated token is presented again.

    Per the OAuth 2.0 Security BCP, this is a compromise signal: an
    attacker who exfiltrated a refresh token and replayed it after the
    legitimate user rotated has just exposed themselves. The service
    revokes the entire `family_id` before raising this; the caller
    should log the event and walk the user through re-authentication.
    """

    code = "refresh_token_reuse_detected"
    message_zh = "检测到刷新令牌被重放,会话已强制登出"
    message_en = "Refresh token reuse detected; family revoked"
    http_status = status.HTTP_401_UNAUTHORIZED


__all__ = [
    "RefreshTokenNotFoundError",
    "RefreshTokenRevokedError",
    "RefreshTokenExpiredError",
    "RefreshTokenReuseError",
]
