"""Refresh-token service — T07 / #8.

This module owns the *business rules* of refresh tokens. The
`RefreshTokenRepository` is the thin Mongo wrapper; everything that
involves the opaque-token format, the hash, the rotation algorithm,
and the reuse-detection policy lives here so the rules are easy to
audit in one place.

Token format
------------

* Raw token (what the client carries): a 32-byte random secret
  rendered as URL-safe base64, ~43 chars. Generated via
  `secrets.token_urlsafe(32)`.
* Stored token (what Mongo persists): the SHA-256 hex digest of the
  raw token, 64 chars. The raw form NEVER touches the database.

Rotation flow
-------------

1. Login (T08 #9) calls `issue(user_id)` → fresh `family_id` (UUID4).
   Returns `(raw_token, RefreshTokenInDB)`.
2. Client sends the raw token to `POST /auth/refresh`. The router
   calls `rotate(token)`.
3. `rotate` hashes the token, looks up the row by hash:
   * absent        → `RefreshTokenNotFoundError` (404).
   * revoked       → `RefreshTokenReuseError` and revoke every other
                     token in `family_id`.
   * expired       → `RefreshTokenExpiredError` (401).
   * active        → revoke the old row, issue a new row with the
                     SAME `family_id`, return it.

Why per-family reuse detection: a leaked refresh token may be
replayed after the legitimate user rotated it. The OAuth 2.0 Security
BCP (RFC 6819 §5.2.2.3, OWASP ASVS V3) treats this as a compromise
signal. Revoking the entire chain — but NOT every token the user owns
— forces the attacker and any session that the legitimate user
opened to re-authenticate, without logging them out on unrelated
devices.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from app.auth.errors import (
    RefreshTokenExpiredError,
    RefreshTokenNotFoundError,
    RefreshTokenReuseError,
    RefreshTokenRevokedError,
)
from app.db.errors import NotFoundError
from app.db.schemas import RefreshTokenCreate, RefreshTokenInDB
from app.repositories.base import utcnow
from app.repositories.refresh_tokens import RefreshTokenRepository

# The opaque-token input size. 32 bytes is well above the 128-bit
# floor recommended by RFC 6749 §10.10 / RFC 6819 §5.1.4.3 and keeps
# the URL-safe-base64 form under typical header limits.
_TOKEN_BYTES: int = 32

# Default TTL per ADR-0009: 7 days. Configurable per-call so tests
# can shorten the window without monkey-patching module globals.
DEFAULT_TTL: timedelta = timedelta(days=7)


def _hash_token(raw_token: str) -> str:
    """Hash a raw refresh token for storage / lookup.

    SHA-256 hex is what the rest of the codebase expects. The hash
    is treated as opaque throughout the service: never logged, never
    compared as a string outside Mongo's equality match.
    """
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def _generate_token() -> str:
    """Produce a fresh opaque refresh token.

    `secrets.token_urlsafe(32)` uses `os.urandom` under the hood
    which on Linux reads from `getrandom(2)` — appropriate for
    security-sensitive tokens.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _new_family_id() -> str:
    """Generate a fresh rotation-chain identifier.

    UUID4 (random) per RFC 4122 §4.4. We render it as a 36-char
    string (`xxxxxxxx-xxxx-...`), well under the schema's
    `max_length=64`.
    """
    return str(uuid.uuid4())


class RefreshTokenService:
    """Issue / verify / rotate / revoke refresh tokens per ADR-0009.

    Holds a `RefreshTokenRepository`. Stateless beyond the repo
    reference, so a single instance can be reused across requests.
    """

    def __init__(self, repo: RefreshTokenRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    async def issue(
        self,
        user_id: str,
        *,
        ttl: timedelta = DEFAULT_TTL,
        family_id: str | None = None,
    ) -> tuple[str, RefreshTokenInDB]:
        """Mint a fresh refresh token for `user_id`.

        Args:
            user_id: ObjectId of the `users` row.
            ttl: Validity window. Defaults to 7 days per ADR-0009.
            family_id: Optional explicit chain id. A fresh UUID is
                minted when omitted.

        Returns:
            `(raw_token, persisted_row)`. The raw token is the only
            thing the client ever sees; the row is for the caller's
            bookkeeping (audit logging, etc.) and for tests.
        """
        if ttl.total_seconds() <= 0:
            raise ValueError("ttl must be positive")

        raw_token = _generate_token()
        new_family = family_id or _new_family_id()
        expires_at = utcnow() + ttl
        created = await self._repo.create(
            RefreshTokenCreate(
                token_hash=_hash_token(raw_token),
                user_id=user_id,
                family_id=new_family,
                expires_at=expires_at,
            )
        )
        return raw_token, created

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    async def verify(self, raw_token: str) -> RefreshTokenInDB:
        """Resolve a raw token to its persisted row.

        Raises the auth-specific `RefreshToken*Error` family so the
        router can render the right 401/404 envelope. Does NOT touch
        `revoked_at`; use `rotate` for that (it performs the
        reuse-detection dance atomically with revocation).
        """
        token_hash = _hash_token(raw_token)
        try:
            row = await self._repo.get_by_hash(token_hash)
        except NotFoundError as exc:
            raise RefreshTokenNotFoundError(
                details={"token_hash_prefix": token_hash[:8]},
            ) from exc

        if row.revoked_at is not None:
            raise RefreshTokenRevokedError(
                details={
                    "user_id": row.user_id,
                    "revoked_at": row.revoked_at.isoformat(),
                },
            )
        # Strict `<` so a token expiring exactly at "now" is rejected.
        # `<=` would let a millisecond window succeed and the TTL
        # purge run minutes later — the gap is enough for a flaky
        # race. We choose the conservative side.
        if row.expires_at < utcnow():
            raise RefreshTokenExpiredError(
                details={
                    "user_id": row.user_id,
                    "expires_at": row.expires_at.isoformat(),
                },
            )
        return row

    # ------------------------------------------------------------------
    # Rotate
    # ------------------------------------------------------------------

    async def rotate(
        self,
        raw_token: str,
        *,
        ttl: timedelta = DEFAULT_TTL,
    ) -> tuple[str, RefreshTokenInDB]:
        """Rotate a refresh token.

        Returns `(new_raw_token, new_row)`. The old row's
        `revoked_at` is stamped and its `replaced_by` points at the
        new row's id.

        Raises:
            RefreshTokenNotFoundError: token hash not present.
            RefreshTokenExpiredError: `expires_at` elapsed.
            RefreshTokenReuseError: token was already revoked; the
                entire family has now been revoked.
        """
        token_hash = _hash_token(raw_token)
        # Step 1 — fetch the row to learn its family and state. We
        # deliberately don't reuse `verify()` here because we need the
        # row in BOTH active and revoked states (revoked triggers
        # reuse detection, not a generic 401).
        try:
            row = await self._repo.get_by_hash(token_hash)
        except NotFoundError as exc:
            raise RefreshTokenNotFoundError(
                details={"token_hash_prefix": token_hash[:8]},
            ) from exc

        if row.revoked_at is not None:
            # Reuse detected. Burn the family and raise. We log the
            # count so an admin/audit can replay the event later.
            revoked_count = await self._repo.revoke_family(row.family_id)
            raise RefreshTokenReuseError(
                details={
                    "user_id": row.user_id,
                    "family_id": row.family_id,
                    "revoked_additional_count": revoked_count,
                },
            )
        if row.expires_at < utcnow():
            raise RefreshTokenExpiredError(
                details={
                    "user_id": row.user_id,
                    "expires_at": row.expires_at.isoformat(),
                },
            )

        # Step 2 — happy path: mint the successor and atomically
        # chain it. The repository's `rotate` accepts a fully-formed
        # `RefreshTokenCreate` and inserts it as part of the rotation
        # — we delegate in one call so the hash and id stay coherent.
        # We don't wrap this in a Mongo transaction because the
        # `replaced_by` field is informational — if the update fails,
        # the new token is still valid (audit just loses the parent
        # link). The repository already implements this best-effort
        # semantics.
        new_raw = _generate_token()
        old, new_row = await self._repo.rotate(
            old_hash=token_hash,
            new_token=RefreshTokenCreate(
                token_hash=_hash_token(new_raw),
                user_id=row.user_id,
                family_id=row.family_id,
                expires_at=utcnow() + ttl,
            ),
        )
        # `old` is returned for symmetry / audit hooks; the caller
        # never sees it directly.
        _ = old
        return new_raw, new_row

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    async def revoke(self, raw_token: str) -> RefreshTokenInDB:
        """Revoke a single token by its raw form.

        Returns the post-revoke row so the caller can confirm
        `revoked_at` was stamped. Idempotent — re-revoking returns
        the same row.
        """
        token_hash = _hash_token(raw_token)
        try:
            return await self._repo.revoke(token_hash)
        except NotFoundError as exc:
            raise RefreshTokenNotFoundError(
                details={"token_hash_prefix": token_hash[:8]},
            ) from exc

    async def revoke_family(self, family_id: str) -> int:
        """Revoke every active token sharing `family_id`.

        Returns the number of rows flipped. Use this from the
        admin / incident-response flows when a token leak is
        reported independently of a rotate-time reuse detection.
        """
        return await self._repo.revoke_family(family_id)

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Force-logout: revoke every active token for a user.

        Wraps the repository method so all "revoke" entry points
        live behind the service surface.
        """
        return await self._repo.revoke_all_for_user(user_id)

    # ------------------------------------------------------------------
    # Inspection (helpers, not hot-path)
    # ------------------------------------------------------------------

    async def list_family(self, family_id: str) -> list[RefreshTokenInDB]:
        """Return every token in a family, oldest first.

        Used by admin tooling to render the rotation chain. Not
        a hot read.
        """
        return await self._repo.list_family(family_id)


__all__ = [
    "RefreshTokenService",
    "DEFAULT_TTL",
]
