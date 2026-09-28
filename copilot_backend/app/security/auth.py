"""Authentication dependency — T09 / #10.

`get_current_user` decodes the access JWT carried in the
`Authorization: Bearer …` header, looks up the `users` row, and
returns it. Routes that need "the authenticated user" depend on
this; the global `AppError` handler renders the 401/403 envelopes
the dependency raises.

Why a dedicated module
----------------------

Putting `get_current_user` here, alongside the JWT mint/verify
primitives in `app.security.jwt`, makes the
"signing-key + decoding" + "fetch-from-DB" coupling obvious. The
alternative — burying the dependency in `app.db.dependencies` — would
hide the JWT verification step behind a repo import.

Wire shape
----------

* Header: `Authorization: Bearer <jwt>` (case-sensitive scheme per
  RFC 6750 §2.1).
* 401 envelopes raised here:
  - `auth_missing_token` — header absent.
  - `auth_invalid_token` — header present but unparseable, bad
    signature, malformed claims, or expired.
  - `auth_token_subject_unknown` — JWT decodes cleanly but the
    `users._id` no longer exists (admin hard-deleted the user).
* 403 envelopes raised here:
  - `user_inactive` — row exists but `is_active=False`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request, status

from app.auth.errors import UserInactiveError
from app.db.dependencies import get_user_repository
from app.db.errors import NotFoundError
from app.db.schemas import User
from app.exceptions import AppError
from app.repositories.users import UserRepository
from app.security.jwt import decode_jwt
from app.settings import Settings, get_settings

if TYPE_CHECKING:
    pass


class AuthMissingTokenError(AppError):
    """No `Authorization: Bearer …` header on the request.

    Distinct from `AuthInvalidTokenError`: this is the client
    forgetting to authenticate, not the client authenticating
    badly. Same HTTP status (401), different `code` so the
    front-end can render "log in" vs "your session is broken".
    """

    code = "auth_missing_token"
    message_zh = "缺少身份认证凭据"
    message_en = "Missing authentication credentials"
    http_status = status.HTTP_401_UNAUTHORIZED


class AuthInvalidTokenError(AppError):
    """The presented token is unparseable, signed with the wrong key,
    expired, or carries claims we can't accept.

    Covers every "the JWT was bad" path with one envelope so we don't
    leak which specific check failed (signature vs expiry vs shape).
    """

    code = "auth_invalid_token"
    message_zh = "身份认证凭据无效"
    message_en = "Invalid authentication credentials"
    http_status = status.HTTP_401_UNAUTHORIZED


def _bearer_token_from_header(authorization_header: str | None) -> str:
    """Extract the opaque token from `Authorization: Bearer <token>`.

    Raises `AuthMissingTokenError` on missing header and
    `AuthInvalidTokenError` on a header that isn't `Bearer …`. The
    401 envelope is the same — the front-end can't distinguish a
    typo'd scheme from a missing token because doing so leaks
    "you sent a header but we didn't like it".
    """
    if authorization_header is None:
        raise AuthMissingTokenError()
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthInvalidTokenError()
    return parts[1].strip()


def _decode_and_validate(token: str, *, signing_key: str) -> dict[str, Any]:
    """Run the verification gauntlet on `token`.

    Returns the decoded payload. Raises `AuthInvalidTokenError` for
    every failure path so the caller only ever sees a clean dict or
    a 401 — never a partial result. The return type matches
    `decode_jwt` so callers can index into the dict without
    `assert` gymnastics.
    """
    try:
        payload = decode_jwt(token, signing_key=signing_key)
    except ValueError as exc:
        # `decode_jwt` raises ValueError for malformed shape, bad
        # signature, expired `exp`, etc. — collapse to one envelope
        # so we don't leak which check failed.
        raise AuthInvalidTokenError(
            details={"reason": "decode_failed"},
        ) from exc
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthInvalidTokenError(details={"reason": "missing_sub"})
    return payload


async def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
    users: UserRepository = Depends(get_user_repository),  # noqa: B008
) -> User:
    """FastAPI dependency that returns the authenticated `User`.

    Sequence:

    1. Pull the `Authorization` header, extract the bearer token.
    2. Verify the JWT against `settings.oidc_jwt_signing_key`.
    3. Resolve `sub` (the `users._id`) via the repository.
    4. Re-check `is_active` — the JWT could be fresh while an admin
       offboarded the user in the last few milliseconds; the DB is
       authoritative.

    Raises:
        AuthMissingTokenError: no `Authorization` header (401).
        AuthInvalidTokenError: header present but invalid (401).
        NotFoundError: `sub` does not match any row (404).
        UserInactiveError: row exists but is deactivated (403).
    """
    token = _bearer_token_from_header(request.headers.get("Authorization"))
    payload = _decode_and_validate(token, signing_key=settings.oidc_jwt_signing_key)
    sub = payload["sub"]
    assert isinstance(sub, str)  # narrowed by `_decode_and_validate`
    try:
        user = await users.get(sub)
    except NotFoundError as exc:
        # The token was signed by us but the user vanished — admin
        # hard-delete between login and request. Surface as 401 so
        # the front-end walks the user through re-auth rather than
        # rendering a confusing 404.
        raise AuthInvalidTokenError(
            details={"reason": "subject_unknown"},
        ) from exc
    if not user.is_active:
        raise UserInactiveError(details={"user_id": user.id})
    return user


__all__ = [
    "AuthMissingTokenError",
    "AuthInvalidTokenError",
    "get_current_user",
]
