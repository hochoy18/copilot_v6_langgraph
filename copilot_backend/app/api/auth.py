"""SSO + refresh + local-login auth router.

Three families of endpoints, one shape per family:

* SSO (`/api/v1/auth/sso/login`, `/api/v1/auth/sso/callback`) — T08.
* Refresh (`/api/v1/auth/refresh`) — T08b.
* Local admin login + logout (`/api/v1/auth/login`,
  `/api/v1/auth/logout`) — T09.

Plus `/api/v1/admin/me` — the authenticated-admin "who am I"
endpoint the admin shell calls to render the username (T09
acceptance: "/admin 显示用户名").

All endpoints sit behind no auth middleware (by definition: this is
how the user *gets* tokens, or how the admin shell fetches its own
identity). The `GET /api/v1/admin/me` endpoint requires a valid
access token via the `get_current_user` dependency.

The router is intentionally thin: every byte of auth-domain logic
lives in `app.auth.oidc` (IdP I/O), `app.auth.login` (SSO
orchestration), `app.auth.local` (local-login orchestration),
`app.auth.tokens` (refresh-token rotation rules), and
`app.auth.passwords` (bcrypt). Routers exist to translate HTTP
envelopes into service calls; nothing more.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.errors import AdminEndpointRequiresLocalUserError
from app.auth.local import LocalLoginService
from app.auth.login import OIDCLoginService
from app.auth.tokens import RefreshTokenService
from app.db.dependencies import (
    get_local_login_service,
    get_oidc_login_service,
    get_refresh_token_service,
)
from app.db.schemas import User
from app.security.auth import get_current_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Wire shapes — SSO
# ---------------------------------------------------------------------------


class SSOLoginResponse(BaseModel):
    """Body of `GET /api/v1/auth/sso/login`."""

    authorization_url: str = Field(
        description=(
            "IdP authorization endpoint with `response_type`, `client_id`, "
            "`redirect_uri`, `scope`, `state`, `nonce`, `code_challenge`, "
            "`code_challenge_method` already set."
        ),
    )
    state: str = Field(
        description=(
            "Opaque CSRF value the front-end must echo on the callback. "
            "Tied server-side to the `nonce` + `code_verifier`."
        ),
    )


class SSOCallbackRequest(BaseModel):
    """Body of `POST /api/v1/auth/sso/callback`."""

    code: str = Field(description="Authorization code from the IdP redirect.")
    state: str = Field(description="Echo of the `state` we returned at login start.")


class SSOCallbackResponse(BaseModel):
    """Body of `POST /api/v1/auth/sso/callback`."""

    access_token: str = Field(description="Short-lived JWT for subsequent API calls.")
    refresh_token: str = Field(description="Opaque token; rotate via `/auth/refresh`.")
    token_type: str = Field(default="Bearer")
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    user: dict[str, Any] = Field(
        description="Canonical `User` shape from the upsert.",
    )


# ---------------------------------------------------------------------------
# Wire shapes — refresh  (T08b / #49)
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    """Body of `POST /api/v1/auth/refresh`.

    The refresh token travels in the body, not in a cookie: ADR-0032
    keeps it in `localStorage` (JS-readable, so SSE auth via Query
    Param can work alongside it — ADR-0010). Pydantic's `min_length=1`
    matches the service-side token-format expectations without
    duplicating the SHA-256 size check (that lives in `RefreshTokenService`
    and surfaces as 404 / `refresh_token_not_found`).
    """

    refresh_token: str = Field(
        min_length=1,
        description="Opaque token previously issued at login or refresh.",
    )


class RefreshResponse(BaseModel):
    """Body of `POST /api/v1/auth/refresh`.

    Shape deliberately mirrors `SSOCallbackResponse` so the front-end
    can use one response parser for both login-completion and
    refresh (ADR-0009 / ADR-0032). `refresh_token` is the *new*
    rotated value — the old one is revoked atomically by the service.
    """

    access_token: str = Field(description="Fresh short-lived JWT.")
    refresh_token: str = Field(description="Rotated opaque token; previous is revoked.")
    token_type: str = Field(default="Bearer")
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    user: dict[str, Any] = Field(
        description="Canonical `User` shape — mirrors the SSO callback response.",
    )


# ---------------------------------------------------------------------------
# Wire shapes — local admin login (T09 / #10)
# ---------------------------------------------------------------------------


class LocalLoginRequest(BaseModel):
    """Body of `POST /api/v1/auth/login`.

    `username` is the `users.local_username` value (the admin login
    id, not the email). `password` is verified by bcrypt against the
    persisted `password_hash` (T09 / #10, ADR-0006).
    """

    username: str = Field(
        min_length=1,
        max_length=128,
        description="Admin local username (T04 / #5).",
    )
    password: str = Field(
        min_length=1,
        max_length=512,
        description="Plaintext password. Verified via bcrypt at the service layer.",
    )


class LocalLoginResponse(BaseModel):
    """Body of `POST /api/v1/auth/login`.

    Mirrors `SSOCallbackResponse` so the front-end can use one
    response parser (ADR-0032). The wire shape is intentionally
    identical: same `access_token` / `refresh_token` / `token_type`
    / `expires_in` / `user` keys, in the same order.
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: dict[str, Any]


class LogoutRequest(BaseModel):
    """Body of `POST /api/v1/auth/logout`.

    The refresh token travels in the body, mirroring the refresh
    endpoint's shape (ADR-0032). Revoking the row in
    `refresh_tokens` invalidates the session atomically — the
    subsequent `/auth/refresh` lands in the reuse-detection path and
    burns the family.
    """

    refresh_token: str = Field(
        min_length=1,
        description="Opaque refresh token to revoke.",
    )


class LogoutResponse(BaseModel):
    """Body of `POST /api/v1/auth/logout`.

    Confirms the revocation landed by echoing back the now-revoked
    token's hash prefix. Future callers (e.g. an admin force-logout
    tool) can correlate against the audit log without holding the
    raw token.
    """

    revoked: bool = Field(default=True)
    token_hash_prefix: str = Field(
        description="First 8 chars of the revoked row's `token_hash`.",
    )


# ---------------------------------------------------------------------------
# Routes — SSO
# ---------------------------------------------------------------------------


@router.get(
    "/sso/login",
    response_model=SSOLoginResponse,
    summary="Begin OIDC code+PKCE login",
)
async def sso_login(
    svc: OIDCLoginService = Depends(get_oidc_login_service),  # noqa: B008
) -> SSOLoginResponse:
    """Mint state + PKCE + nonce and return the IdP authorization URL."""
    result = await svc.start_login()
    return SSOLoginResponse(
        authorization_url=result.authorization_url,
        state=result.state,
    )


@router.post(
    "/sso/callback",
    response_model=SSOCallbackResponse,
    summary="Complete OIDC code+PKCE login",
)
async def sso_callback(
    body: SSOCallbackRequest,
    svc: OIDCLoginService = Depends(get_oidc_login_service),  # noqa: B008
) -> SSOCallbackResponse:
    """Exchange the IdP `code`, verify the `id_token`, upsert the user, mint tokens."""
    result = await svc.complete_login(code=body.code, state=body.state)
    return SSOCallbackResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        user=result.user.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Routes — refresh  (T08b / #49)
# ---------------------------------------------------------------------------


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Rotate the refresh token and mint a new access JWT",
)
async def auth_refresh(
    body: RefreshRequest,
    svc: OIDCLoginService = Depends(get_oidc_login_service),  # noqa: B008
) -> RefreshResponse:
    """Rotate the presented refresh token and return fresh credentials.

    On success the previous refresh token is atomically revoked by
    `RefreshTokenService.rotate` (T07 / #8); replaying it after this
    call lands in the reuse-detection path and burns the entire
    family (`refresh_token_reuse_detected`, 401).

    Errors propagate from the service layer through the global
    `AppError` handler:

    * `refresh_token_not_found` — token absent (404).
    * `refresh_token_expired` — `expires_at` elapsed (401).
    * `refresh_token_revoked` — explicit revocation since issue (401).
    * `refresh_token_reuse_detected` — replay of an already-rotated
      token; family revoked (401).
    * `user_inactive` — local admin deactivated the user mid-session
      (403).
    """
    result = await svc.refresh(body.refresh_token)
    return RefreshResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        user=result.user.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Routes — local admin login (T09 / #10)
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=LocalLoginResponse,
    summary="Admin local login (username + bcrypt)",
)
async def local_login(
    body: LocalLoginRequest,
    svc: LocalLoginService = Depends(get_local_login_service),  # noqa: B008
) -> LocalLoginResponse:
    """Authenticate an admin by `username` + `password`.

    On success returns the same wire shape as the SSO callback:
    access JWT, refresh token, `token_type`, `expires_in`, canonical
    `user`. The front-end can use one response parser for both
    paths (ADR-0032).

    Errors (all raised from `LocalLoginService.login`, rendered by
    the global `AppError` handler):

    * `invalid_local_credentials` (401) — username unknown OR
      password mismatch. One envelope so the wire response cannot be
      used to enumerate usernames.
    * `user_inactive` (403) — admin row has `is_active=False` (local
      offboarding; distinct from "wrong password" so the front-end
      can render "account disabled").
    """
    result = await svc.login(username=body.username, password=body.password)
    return LocalLoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        user=result.user.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Routes — logout (T09 / #10)
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Revoke a refresh token (logout / force-logout)",
)
async def auth_logout(
    body: LogoutRequest,
    svc: RefreshTokenService = Depends(get_refresh_token_service),  # noqa: B008
) -> LogoutResponse:
    """Revoke the presented refresh token.

    Idempotent at the row level: revoking an already-revoked token
    is a no-op (`revoke` only flips rows whose `revoked_at IS NULL`).
    Unknown tokens return 404 (`refresh_token_not_found`) — the
    client should not send tokens it never received.

    After this call lands, the next `/auth/refresh` with the same
    token hits the reuse-detection branch and burns the entire
    family (T07 / #8). The user is forced to log in again — which
    is the goal of logout.
    """
    revoked = await svc.revoke(body.refresh_token)
    return LogoutResponse(revoked=True, token_hash_prefix=revoked.token_hash[:8])


# ---------------------------------------------------------------------------
# Routes — /admin/me (T09 / #10)
# ---------------------------------------------------------------------------
#
# Lives in this router file rather than `app.api.admin` because (a)
# the auth router already owns the `/admin` tag in OpenAPI, and
# (b) the only consumer is the admin shell, which already speaks
# `auth` for the rest of its lifecycle. A future admin-tooling
# ticket can group these into a dedicated `admin` router if the
# surface grows.


admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class AdminMeResponse(BaseModel):
    """Body of `GET /api/v1/admin/me`.

    The admin shell renders the username (`local_username`) and
    display name next to the user avatar. ADR-0006 binds the local
    admin's identity to `users.local_username`; `display_name` is
    what the SSO path mirrors from the IdP and what the local
    seed sets explicitly.

    T09 (`#10`) only wires the local-admin path. SSO callers reach
    the route through the same `get_current_user` dependency but
    are rejected with 403 — they don't have a `local_username` and
    the `/admin` shell isn't theirs to render.
    """

    id: str = Field(description="ObjectId of the `users` row, as string.")
    username: str = Field(description="`local_username` (the login id).")
    display_name: str = Field(description="Human-readable name.")
    email: str = Field(description="Account email (canonical across SSO/local).")
    source: str = Field(description="Identity source. Always `local` for T09 / #10.")


@admin_router.get(
    "/me",
    response_model=AdminMeResponse,
    summary="Return the authenticated admin's profile",
)
async def admin_me(
    user: User = Depends(get_current_user),  # noqa: B008
) -> AdminMeResponse:
    """Return the authenticated admin's username + display name.

    Drives the `/admin` shell header (T09 acceptance: "/admin 显示
    用户名"). `get_current_user` decodes the access JWT, looks up the
    `users` row, and raises 401/403 on the failure paths — the route
    only ever sees a valid, active `User`.

    Rejects SSO callers with 403: the `/admin` shell is for local
    admins, and SSO users don't have a `local_username`. A future
    ticket that wants SSO `/me` semantics can route through a
    different endpoint.
    """
    if user.source != "local" or not user.local_username:
        raise AdminEndpointRequiresLocalUserError(
            details={"user_id": user.id, "source": user.source},
        )
    return AdminMeResponse(
        id=user.id,
        username=user.local_username,
        display_name=user.display_name,
        email=user.email,
        source=user.source,
    )


__all__ = ["router", "admin_router"]
