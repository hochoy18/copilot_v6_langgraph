"""SSO auth router — T08 / #46.

Two endpoints, one shape each:

* `GET /api/v1/auth/sso/login` — front-end initiates a login; returns
  the IdP authorization URL and the `state` it must echo on the
  callback.
* `POST /api/v1/auth/sso/callback` — IdP redirects back with `code`
  + `state`; the backend exchanges the code, verifies the
  `id_token`, upserts the local `users` row, and returns the
  access + refresh tokens for the session.

Both endpoints sit behind no auth middleware (by definition: this is
how the user *gets* tokens). CORS is configured at the global
middleware layer; no per-route configuration.

The router is intentionally thin: every byte of auth-domain logic
lives in `app.auth.oidc` (IdP I/O) and `app.auth.login`
(orchestration). Routers exist to translate HTTP envelopes into
service calls; nothing more.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.login import OIDCLoginService
from app.db.dependencies import get_oidc_login_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Wire shapes
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
# Routes
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


__all__ = ["router"]