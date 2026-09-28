"""Admin local-login orchestrator — T09 / #10.

Glues three pieces together:

* `UserRepository.get_by_local_username` (T04 / #5) — finds the row by
  the username the admin typed in.
* `app.auth.passwords.verify_password` — bcrypt constant-time compare.
* `RefreshTokenService.issue` + the JWT mint that T08 already uses for
  SSO (factored into `mint_access_token_for_user` below so the two
  login paths don't drift).

The orchestrator never calls `bcrypt` directly — that's the helper's
job. It does not know about FastAPI either — the router in
`app.api.auth` owns the request / response shaping.

Why a separate module from `OIDCLoginService`
---------------------------------------------

The two login paths share JWT + refresh-token plumbing but nothing
else: OIDC talks to an IdP, manages state + PKCE + nonce, and upserts
a user keyed by `sub`. Local login is username + password + bcrypt
verify. Bundling them would force every reader of the IdP code to
also load bcrypt and the local-user schema. Keeping the paths in
separate modules makes the dependency arrows obvious.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.auth.errors import (
    InvalidLocalCredentialsError,
    UserInactiveError,
)
from app.auth.passwords import verify_password
from app.auth.tokens import RefreshTokenService
from app.db.errors import NotFoundError
from app.db.schemas import User
from app.repositories.users import UserRepository
from app.security.jwt import mint_access_token_for_user
from app.settings import Settings

__all__ = [
    "LocalLoginService",
    "LoginResult",
]

# How much of `local_username` to echo back in `InvalidLocalCredentialsError.details`.
# Long enough to disambiguate common logins, short enough that it cannot be
# used as a fingerprint to enumerate the rest of the admin table.
_USERNAME_PREFIX_LEN: int = 4


# ---------------------------------------------------------------------------
# Login service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginResult:
    """What `LocalLoginService.login` returns to the caller.

    Field-for-field compatible with `LoginCompleteResult` from
    `app.auth.login` so the router can use one response shape for
    both SSO and local logins. Kept as a separate type alias to make
    the seam explicit; `LoginCompleteResult` and `LoginResult` carry
    the same payload but a future ticket (e.g. T11's `/me` response)
    can extend the local shape without touching OIDC.
    """

    user: User
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class LocalLoginService:
    """Authenticate an admin by username + password per ADR-0009 / ADR-0006.

    Holds references to the user repo, the refresh-token service, and
    the Settings (for the JWT TTL). Stateless beyond those references
    — one instance per process is fine.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        user_repository: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        self._settings = settings
        self._users = user_repository
        self._refresh = refresh_service

    async def login(self, *, username: str, password: str) -> LoginResult:
        """Authenticate `username` / `password` and mint a session.

        Sequence:

        1. Look up `users.local_username` (raises `NotFoundError` → we
           translate to `InvalidLocalCredentialsError`).
        2. `verify_password` against `password_hash`. Wrong / malformed
           hash → same `InvalidLocalCredentialsError`. Same envelope as
           the missing-user case so the wire response is
           indistinguishable and username enumeration is closed.
        3. `is_active` check — deactivated admins don't get a session,
           matching the SSO callback behaviour.
        4. Issue a fresh refresh token + mint the access JWT, mirroring
           `OIDCLoginService.complete_login`'s wire shape.

        Raises:
            InvalidLocalCredentialsError: username unknown OR password
                mismatch OR hash could not be verified. One envelope
                so the front-end cannot enumerate users.
            UserInactiveError: the local row is `is_active=False`.
        """
        try:
            row = await self._users.get_by_local_username_in_db(username)
        except NotFoundError as exc:
            # Translate to the credentials envelope so the wire
            # response is identical to "wrong password". Username
            # enumeration is the threat — the message must not leak
            # which side missed.
            raise InvalidLocalCredentialsError(
                details={"username_prefix": username[:_USERNAME_PREFIX_LEN]},
            ) from exc

        # `UserRepository.create` rejects `source="local"` rows without
        # a `password_hash`, so `row.password_hash` is guaranteed
        # non-None here. A missing value would be a repository
        # invariant violation and would surface as a 500 from the
        # generic handler — that's the right signal for a real bug,
        # not a 401 hiding the failure.
        assert row.password_hash is not None  # enforced by UserRepository.create
        if not verify_password(password, row.password_hash):
            raise InvalidLocalCredentialsError(
                details={"username_prefix": username[:_USERNAME_PREFIX_LEN]},
            )

        # `verify_password` is the only thing that touches `password_hash`
        # directly. Strip it before any further use.
        user = User.from_db(row)

        if not user.is_active:
            raise UserInactiveError(details={"user_id": user.id})

        refresh_raw, _refresh_row = await self._refresh.issue(user.id)
        access_token, expires_in = mint_access_token_for_user(
            user, settings=self._settings,
        )

        return LoginResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_raw,
            token_type="Bearer",
            expires_in=expires_in,
        )
