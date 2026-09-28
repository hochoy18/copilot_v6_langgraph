"""OIDC login orchestrator — T08 / #46.

Glues three pieces together:

* `OIDCAdapter` — speaks OIDC to the IdP (discovery, code→tokens,
  id_token verification).
* `OIDCStateStore` — holds `state → (nonce, code_verifier)` between
  `GET /auth/sso/login` and `POST /auth/sso/callback` with a TTL
  matching `settings.oidc_state_ttl_seconds`.
* `UserRepository.upsert_sso_user` + `RefreshTokenService.issue` —
  the local side-effects per ADR-0006 / ADR-0009.

The orchestrator never calls `httpx` directly; that's the adapter's
job. It does not know about FastAPI either — the router in
`app.api.auth` owns the request / response shaping.

State store choice
------------------

In-memory with TTL is the right MVP choice: a single backend process
hosts the state store, and `state` round-trips are short-lived
(seconds, maybe a minute). If T40 / future tickets deploy multiple
replicas behind a load balancer, this swap is the lever — Redis is
the canonical next stop and the `OIDCStateStore` protocol can stay
unchanged because every interaction is `(state) -> entry` lookup
plus a single atomic `take` (consume-on-read) for the callback.

Why a separate state store file
-------------------------------

`OIDCAdapter` is IdP-only; `OIDCStateStore` is local-state-only.
Bundling them in one module would force every reader of the IdP
adapter to also load the state-store code, and every state-store
change would force a re-read of the IdP adapter. Keeping them in
separate modules makes the dependency arrow
`login -> {oidc, state_store}` obvious.
"""
from __future__ import annotations

import time
from collections.abc import MutableMapping
from dataclasses import dataclass

from app.auth.errors import (
    OIDCStateMismatchError,
    RefreshTokenNotFoundError,
    UserInactiveError,
)
from app.auth.oidc import (
    OIDCAdapter,
    VerifiedIDTokenClaims,
    derive_code_challenge,
    generate_code_verifier,
    generate_nonce,
    generate_state,
)
from app.auth.tokens import RefreshTokenService
from app.db.errors import NotFoundError
from app.db.schemas import User, UserCreate, UserUpdate
from app.repositories.users import UserRepository
from app.security.jwt import mint_access_token_for_user
from app.settings import Settings


@dataclass(frozen=True)
class OIDCLoginEntry:
    """The bundle we hold between login start and callback.

    `nonce` is bound to the `id_token` so a token from a *different*
    session can't be replayed against *this* session. `code_verifier`
    is consumed exactly once at token-exchange time. `created_at`
    is the wall-clock when the entry was minted — used for TTL
    sweeps; lazy cleanup is fine because reads happen anyway.
    """

    state: str
    nonce: str
    code_verifier: str
    created_at: float


class OIDCStateStore:
    """Single-process TTL map for OIDC `state` → login bundle.

    `put(...)` adds an entry; `take(state)` consumes the entry (one
    callback per state). The TTL is enforced lazily on `take` — a
    thread doesn't need to spin for the sweeper. For an MVP this is
    enough; a Redis-backed variant drops in here without changing
    callers.
    """

    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._entries: MutableMapping[str, OIDCLoginEntry] = {}

    async def put(self, entry: OIDCLoginEntry) -> None:
        """Save an entry, overwriting any pre-existing one for the same state."""
        self._entries[entry.state] = entry

    async def take(self, state: str) -> OIDCLoginEntry:
        """Consume the entry for `state`, raising on miss or TTL expiry.

        Raises:
            OIDCStateMismatchError: state is unknown OR expired OR the
                store is set to `ttl=0` (disabled). All three are the
                same external signal — "the client must restart the
                login" — so we don't bother distinguishing.
        """
        entry = self._entries.pop(state, None)
        if entry is None:
            raise OIDCStateMismatchError(
                details={"state_prefix": state[:8]},
            )
        if self._ttl <= 0 or (time.monotonic() - entry.created_at) >= self._ttl:
            raise OIDCStateMismatchError(
                details={"state_prefix": state[:8], "reason": "expired"},
            )
        return entry

    async def purge_expired(self) -> int:
        """Drop every entry past its TTL. Tests call this explicitly."""
        if self._ttl <= 0:
            return 0
        now = time.monotonic()
        expired = [
            s for s, e in self._entries.items() if (now - e.created_at) >= self._ttl
        ]
        for s in expired:
            self._entries.pop(s, None)
        return len(expired)

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Login service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginStartResult:
    """What `GET /auth/sso/login` returns to the front-end.

    The front-end redirects to `authorization_url` and stashes
    `state` for the callback. `code_verifier` / `nonce` stay on the
    server; the client doesn't need them.
    """

    authorization_url: str
    state: str


@dataclass(frozen=True)
class LoginCompleteResult:
    """What `POST /auth/sso/callback` returns to the front-end."""

    user: User
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class OIDCLoginService:
    """Orchestrate the OIDC code+PKCE flow per ADR-0009.

    Holds references to the IdP adapter, the state store, the user
    repo, and the refresh-token service. Stateless beyond those
    references — one instance per process is fine.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        oidc_adapter: OIDCAdapter,
        state_store: OIDCStateStore,
        user_repository: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        self._settings = settings
        self._oidc = oidc_adapter
        self._states = state_store
        self._users = user_repository
        self._refresh = refresh_service

    # ------------------------------------------------------------------
    # Start login
    # ------------------------------------------------------------------

    async def start_login(self) -> LoginStartResult:
        """Generate the PKCE bundle + authorization URL.

        The state store gets `state → (nonce, code_verifier)` keyed by
        a fresh `state`. The front-end redirects the browser to
        `authorization_url`; the IdP redirects back to
        `settings.oidc_redirect_uri` with `?code=…&state=…` which
        `complete_login` consumes.
        """
        state = generate_state()
        nonce = generate_nonce()
        code_verifier = generate_code_verifier()
        code_challenge = derive_code_challenge(code_verifier)
        entry = OIDCLoginEntry(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            created_at=time.monotonic(),
        )
        await self._states.put(entry)
        authorization_url = await self._oidc.build_authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
        )
        return LoginStartResult(authorization_url=authorization_url, state=state)

    # ------------------------------------------------------------------
    # Complete login
    # ------------------------------------------------------------------

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
    ) -> LoginCompleteResult:
        """Consume the IdP callback and return access + refresh tokens.

        Sequence:

        1. Look up `state` in the store (raises `OIDCStateMismatchError`
           on miss / expiry).
        2. Exchange `code` + `code_verifier` with the IdP
           (`OIDCAdapter.exchange_code_for_tokens`).
        3. Verify the returned `id_token` against expected
           `iss`/`aud`/`nonce`/`exp` (`OIDCAdapter.verify_id_token`).
        4. Upsert the `users` row keyed by `sub`
           (`UserRepository.upsert_sso_user`).
        5. Issue a fresh refresh token in the user's chain
           (`RefreshTokenService.issue`).
        6. Mint a short-lived access JWT and shape the wire response.

        Raises:
            OIDCStateMismatchError: state missing or expired.
            OIDCTokenExchangeError: IdP rejected the code.
            OIDCIDTokenInvalidError / OIDCClaimsMismatchError: id_token
                failed verification.
            Any repository-layer error: `DuplicateKeyError` on the
                race window described in `UserRepository.upsert_sso_user`,
                etc.
        """
        entry = await self._states.take(state)

        tokens = await self._oidc.exchange_code_for_tokens(
            code=code,
            code_verifier=entry.code_verifier,
        )

        # The IdP-side signing key is distinct from our access-token
        # signing key. RS256 IdPs leave it empty; HS256 IdPs (and
        # our test IdP mock) set it to a shared secret. The adapter
        # only consults it when the token's `alg` header is `HS256`.
        signing_key = self._settings.oidc_id_token_signing_key or None
        claims = self._oidc.verify_id_token(
            tokens.id_token,
            expected_nonce=entry.nonce,
            id_token_signing_key=signing_key,
        )

        user = await self._upsert_sso_user(claims)

        if not user.is_active:
            # The IdP can vouch for identity; it can't vouch for our
            # local deactivation (e.g. an admin offboarded them). A
            # deactivated user must not get a new session.
            raise UserInactiveError(details={"user_id": user.id})

        refresh_raw, _refresh_row = await self._refresh.issue(user.id)

        access_token = self._mint_access_token(user)
        expires_in = self._settings.oidc_access_token_ttl_seconds

        return LoginCompleteResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_raw,
            token_type="Bearer",
            expires_in=expires_in,
        )

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh(self, raw_refresh_token: str) -> LoginCompleteResult:
        """Rotate the refresh token + mint a fresh access JWT.

        Mirrors `complete_login`'s wire shape so the front-end can use
        one response parser for both endpoints (ADR-0009 / ADR-0032).
        The refresh-token rotation (T07 / #8) is the *only* stateful
        side effect; this method then re-fetches the user so an admin
        deactivation that landed between login and refresh bounces
        the request with `UserInactiveError`, matching the login path.

        Sequence:

        1. `RefreshTokenService.rotate` — atomic claim-or-reuse-detect.
           Raises `RefreshTokenNotFoundError` / `Expired` / `Revoked` /
           `ReuseError` for the router to render.
        2. `UserRepository.get` — read the canonical `User`. If the
           row disappeared between rotate and fetch, surface as 404.
        3. `is_active` check — deactivated users don't get a new
           session, even if they still hold a valid refresh token.
        4. Mint a fresh access JWT and shape the wire response.

        Raises:
            RefreshTokenNotFoundError: token absent from the database.
            RefreshTokenExpiredError: `expires_at` elapsed.
            RefreshTokenRevokedError: explicit revocation since issue
                (no concurrency; the "I lost the claim" race raises
                `RefreshTokenReuseError` instead).
            RefreshTokenReuseError: replay of a rotated token; the
                service has already burned the entire family.
            UserInactiveError: user deactivated since login.
        """
        new_refresh_raw, rotated_row = await self._refresh.rotate(raw_refresh_token)
        try:
            user = await self._users.get(rotated_row.user_id)
        except NotFoundError as exc:
            # The user row vanished mid-rotation (admin hard-delete).
            # Refresh tokens for a missing user are unusable by
            # definition — surface the same 404 the refresh-token
            # family already uses so the front-end has one envelope.
            raise RefreshTokenNotFoundError(
                details={"user_id": rotated_row.user_id},
            ) from exc

        if not user.is_active:
            raise UserInactiveError(details={"user_id": user.id})

        access_token = self._mint_access_token(user)
        expires_in = self._settings.oidc_access_token_ttl_seconds

        return LoginCompleteResult(
            user=user,
            access_token=access_token,
            refresh_token=new_refresh_raw,
            token_type="Bearer",
            expires_in=expires_in,
        )

    # ------------------------------------------------------------------
    # Internal — SSO upsert + JWT mint
    # ------------------------------------------------------------------

    async def _upsert_sso_user(self, claims: VerifiedIDTokenClaims) -> User:
        """Find-or-create the `users` row bound to `claims.sub`.

        Per ADR-0006 the IdP is the identity authority for SSO users;
        a subject that re-presents itself should resolve to the same
        `users._id` so audit trails and refresh-token families stay
        attached. On repeat logins we mirror IdP-side identity
        changes (`email` / `display_name`) onto the local row so
        admin tooling sees a current name without joining IdP.
        """
        try:
            existing = await self._users.get_by_sso_subject(claims.sub)
        except NotFoundError:
            existing = None

        if existing is None:
            return await self._users.create(
                UserCreate(
                    email=claims.email,
                    display_name=claims.name,
                    source="sso",
                    sso_subject=claims.sub,
                    role_ids=[],
                )
            )

        patch = UserUpdate()
        changed = False
        if existing.email != claims.email:
            patch.email = claims.email
            changed = True
        if existing.display_name != claims.name:
            patch.display_name = claims.name
            changed = True
        if changed:
            return await self._users.update(existing.id, patch)
        return existing

    def _mint_access_token(self, user: User) -> str:
        token, _ttl = mint_access_token_for_user(user, settings=self._settings)
        return token


def build_state_store(settings: Settings) -> OIDCStateStore:
    """Construct an `OIDCStateStore` sized to the configured TTL.

    Centralised so the FastAPI lifespan and the test suite agree on
    the construction shape. The store is process-local; sharing it
    across workers requires a different backend (e.g. Redis) that
    a future ticket will add.
    """
    return OIDCStateStore(ttl_seconds=settings.oidc_state_ttl_seconds)


__all__ = [
    "OIDCStateStore",
    "OIDCLoginEntry",
    "OIDCLoginService",
    "LoginStartResult",
    "LoginCompleteResult",
    "build_state_store",
]
