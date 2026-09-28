"""SSO + refresh auth router — T08 / #46 + T08b / #49.

Three endpoints, one shape each:

* `GET /api/v1/auth/sso/login` — front-end initiates a login; returns
  the IdP authorization URL and the `state` it must echo on the
  callback.
* `POST /api/v1/auth/sso/callback` — IdP redirects back with `code`
  + `state`; the backend exchanges the code, verifies the
  `id_token`, upserts the local `users` row, and returns the
  access + refresh tokens for the session.
* `POST /api/v1/auth/refresh` — front-end presents its current
  refresh token (ADR-0032 keeps it in `localStorage`); the backend
  rotates the refresh token (T07 / #8) and mints a fresh access JWT.

All three endpoints sit behind no auth middleware (by definition: this
is how the user *gets* tokens). CORS is configured at the global
middleware layer; no per-route configuration.

The router is intentionally thin: every byte of auth-domain logic
lives in `app.auth.oidc` (IdP I/O), `app.auth.login` (orchestration),
and `app.auth.tokens` (refresh-token rotation rules). Routers exist
to translate HTTP envelopes into service calls; nothing more.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.login import OIDCLoginService
from app.db.dependencies import get_oidc_login_service

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


__all__ = ["router"]
