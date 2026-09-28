"""Query-param authentication for the SSE stream — T23 / #20.

`EventSource` (WHATWG) cannot set custom request headers; the spec
only lets the consumer pass a `withCredentials` flag, not an
`Authorization: Bearer …` value. Per ADR-0010 the backend therefore
accepts the access JWT on the *query string*: the Frontend opens
`/api/v1/conversations/{id}/stream?token=<jwt>` and the server pulls
the token off the URL before any event is emitted.

This module is the SSE-only analogue of `app.security.auth`. The two
share the same JWT verification path — `get_current_user` already
covers signature + expiry + sub-lookup + is_active checks — so the
seam here is just "extract from `?token=` instead of
`Authorization: Bearer …`, then re-run the same checks".

Why a dedicated dependency (not a copy of `get_current_user`)
-------------------------------------------------------------

`get_current_user` is wired into the global router stack via
`Depends(get_current_user)`; FastAPI resolves it eagerly before the
SSE generator runs. A dedicated `get_sse_user` dependency lets the
SSE endpoint:

1. Pull the token from the query string explicitly.
2. Reuse `get_current_user`'s payload validation / user lookup by
   delegating after we've done the header-vs-query distinction.
3. Surface a distinct error envelope (`auth_missing_token` →
   `auth_missing_sse_token`) so the Frontend can distinguish "the
   page didn't pass a token" from "the page passed a bad bearer".
4. Return both the `User` row and the decoded JWT payload so the
   stream generator can read `exp` for the token-expiry watcher
   without re-decoding the JWT a second time.

The wire-level envelope is otherwise identical to the bearer path so
the Frontend's existing 401 handler can render the same banner.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Query, status

from app.auth.errors import UserInactiveError
from app.db.dependencies import get_user_repository
from app.db.errors import NotFoundError
from app.db.schemas import User
from app.repositories.users import UserRepository
from app.security.auth import (
    AuthInvalidTokenError,
    _decode_and_validate,  # internal but stable — re-uses the
                           # signature/exp/sub gauntlet for the bearer path
)
from app.settings import Settings, get_settings


class SSEMissingTokenError(AuthInvalidTokenError):
    """`?token=` is absent from the SSE URL.

    Distinct error code so the Frontend's SSE-aware error handler can
    tell "you forgot to attach a token" from "you attached a bad
    one". Inherits the 401 envelope from `AuthInvalidTokenError` —
    the wire-level contract is the same `auth_*_token` family per
    ADR-0031's normalised error shape.
    """

    code = "auth_missing_sse_token"
    message_zh = "事件流缺少 token 参数"
    message_en = "SSE request missing ?token="
    http_status = status.HTTP_401_UNAUTHORIZED


def _extract_sse_token(token: str | None) -> str:
    """Pull the access JWT off the `?token=` query parameter.

    Raises the dedicated `SSEMissingTokenError` when the parameter
    is absent or whitespace-only; downstream validation reuses the
    bearer-path gauntlet (`_decode_and_validate`) so envelope
    consistency is preserved.
    """
    if not token or not token.strip():
        raise SSEMissingTokenError()
    return token.strip()


@dataclass(frozen=True)
class AuthenticatedSseToken:
    """Result of validating an SSE query-param JWT.

    Bundles the canonical `User` row with the decoded payload so
    the stream generator can read `exp` (for the token-expiry
    watcher) and `sub` (for ownership auditing) without reaching
    back into the request or re-decoding the JWT.
    """

    user: User
    payload: dict[str, Any]


async def authenticate_sse_token(
    token: str,
    *,
    signing_key: str,
    users: UserRepository,
) -> AuthenticatedSseToken:
    """Run the JWT gauntlet on `token` and resolve the owning user.

    Returns an `AuthenticatedSseToken` carrying the canonical `User`
    row and the decoded JWT payload. The dependency below is a thin
    FastAPI wrapper around this coroutine; the split keeps the test
    seam narrow.

    SSE-specific extra: explicit `exp` check on connect. `EventSource`
    cannot refresh the token mid-stream, so accepting a token whose
    `exp` is already past would just open and immediately close the
    stream with `auth.expired` — better to fail on connect with the
    same `AuthInvalidTokenError` the bearer path emits. The watcher
    (`_token_expiry_watcher`) still handles the "connect valid,
    expire during stream" case.

    Raises:
        SSEMissingTokenError: token empty / whitespace (401).
        AuthInvalidTokenError: bad signature, malformed shape,
            expired, missing `sub` claim (401).
        UserInactiveError: row exists but `is_active=False` (403).
    """
    cleaned = _extract_sse_token(token)
    payload = _decode_and_validate(cleaned, signing_key=signing_key)
    sub = payload["sub"]
    assert isinstance(sub, str)  # narrowed by `_decode_and_validate`
    # SSE-only `exp` check — see docstring.
    exp = payload.get("exp")
    if isinstance(exp, int) and exp < int(time.time()):
        raise AuthInvalidTokenError(details={"reason": "expired"})
    try:
        user = await users.get(sub)
    except NotFoundError as exc:
        # Same envelope as `get_current_user`: a signed token whose
        # `sub` is unknown surfaces as 401 (not 404) so the
        # Frontend's refresh-or-relogin path is the only handler.
        raise AuthInvalidTokenError(
            details={"reason": "subject_unknown"},
        ) from exc
    if not user.is_active:
        raise UserInactiveError(details={"user_id": user.id})
    return AuthenticatedSseToken(user=user, payload=payload)


# ---------------------------------------------------------------------------
# FastAPI dependency — the shape `app.realtime.stream.stream_conversation`
# depends on.
# ---------------------------------------------------------------------------


async def get_sse_user(
    token: str = Query(  # noqa: B008 — FastAPI idiom
        default="",
        description=(
            "Access JWT — required for SSE because EventSource cannot "
            "set custom headers. Same JWT used for the bearer path."
        ),
    ),
    settings: Settings = Depends(get_settings),  # noqa: B008
    users: UserRepository = Depends(get_user_repository),  # noqa: B008
) -> AuthenticatedSseToken:
    """FastAPI dependency: authenticate the SSE query-param token.

    Returns the `AuthenticatedSseToken` (User + payload) the stream
    generator consumes. The route handler reads `.user` for the
    ownership check; the generator reads `.payload["exp"]` for the
    token-expiry watcher.
    """
    return await authenticate_sse_token(
        token,
        signing_key=settings.oidc_jwt_signing_key,
        users=users,
    )


__all__ = [
    "AuthenticatedSseToken",
    "SSEMissingTokenError",
    "authenticate_sse_token",
    "get_sse_user",
]
