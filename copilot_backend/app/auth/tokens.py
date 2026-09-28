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
   * active        → claim the old row atomically (`{token_hash,
                     revoked_at: None}` filter), insert the successor
                     in the same family, return it. If the claim
                     loses to a concurrent rotation (matched_count
                     == 0), the loser treats this as reuse and burns
                     the family.

Why per-family reuse detection: a leaked refresh token may be
replayed after the legitimate user rotated it. The OAuth 2.0 Security
BCP (RFC 6819 §5.2.2.3, OWASP ASVS V3) treats this as a compromise
signal. Revoking the entire chain — but NOT every token the user owns
— forces the attacker and any session that the legitimate user
opened to re-authenticate, without logging them out on unrelated
devices.

Concurrency note
----------------

The rotate path is *not* safe with read-then-write. Two concurrent
`/auth/refresh` calls presenting the same raw token would both pass a
snapshot check and both insert a successor, breaking the family
invariant "at most one active token per chain". `rotate` instead
delegates to `RefreshTokenRepository.claim_for_rotation`, which is
a single conditional `update_one` — Mongo's per-document atomicity
makes the claim step mutually exclusive across callers, and any
loser cleanly falls into the reuse-detected branch.
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


def hash_token(raw_token: str) -> str:
    """Hash a raw refresh token for storage / lookup.

    SHA-256 hex is what the rest of the codebase expects. The hash
    is treated as opaque throughout the service: never logged, never
    compared as a string outside Mongo's equality match. Public so
    tests can verify the storage representation; production callers
    stay inside the service.
    """
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


# Backwards-compat shim — kept so internal callers / older tests
# don't break. New code should use the public `hash_token` above.
_hash_token = hash_token


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
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_public(token_hash: str) -> str:
        """Clip a token hash for safe inclusion in error payloads.

        8 hex chars (32 bits) is more than enough entropy for log
        triage and keeps the schema's `details` envelope small.
        Centralised here so audit / router code doesn't have to know
        the truncation length.
        """
        return token_hash[:8]

    async def _fetch_row(self, token_hash: str) -> RefreshTokenInDB:
        """Resolve `token_hash` to a row, raising the auth-specific 404.

        Translates `NotFoundError` from the repository into
        `RefreshTokenNotFoundError` so the global error handler can
        render the right envelope.
        """
        try:
            return await self._repo.get_by_hash(token_hash)
        except NotFoundError as exc:
            raise RefreshTokenNotFoundError(
                details={"token_hash_prefix": self._hash_public(token_hash)},
            ) from exc

    @staticmethod
    def _assert_active(row: RefreshTokenInDB) -> None:
        """Raise the right error if `row` is no longer usable.

        Order matters: a *revoked-then-expired* row raises
        `RefreshTokenRevokedError` — the cause trumps the
        consequence. We mirror this in the test suite.
        """
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
        row = await self._fetch_row(_hash_token(raw_token))
        self._assert_active(row)
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
        `revoked_at` is stamped by the atomic claim. The new row is
        only inserted if THIS caller won the claim; concurrent
        rotations of the same raw token are detected as reuse and
        land in the `RefreshTokenReuseError` path.

        Raises:
            RefreshTokenNotFoundError: token hash not present.
            RefreshTokenExpiredError: `expires_at` elapsed.
            RefreshTokenRevokedError: token was revoked before this
                call (no concurrency; the "I lost the claim" case
                raises `RefreshTokenReuseError` instead).
            RefreshTokenReuseError: claim lost OR token already
                revoked; the entire family has now been revoked.
        """
        token_hash = _hash_token(raw_token)
        # Step 1 — snapshot read. We use the snapshot for the family
        # id and a fail-fast on expired tokens. *Revoked* tokens are
        # NOT failed-fast here — see step 2.
        row = await self._fetch_row(token_hash)
        if row.expires_at < utcnow():
            raise RefreshTokenExpiredError(
                details={
                    "user_id": row.user_id,
                    "expires_at": row.expires_at.isoformat(),
                },
            )

        # Step 2 — atomic claim. The conditional `update_one` in
        # `claim_for_rotation` is the mutual-exclusion gate that
        # closes the read-then-write race. Two ways it can fail:
        #
        #   (a) the row was already revoked before this call — the
        #       snapshot read happened to win the `revoked_at is
        #       None` window of an already-completed rotation or an
        #       explicit `revoke(...)`. Pure reuse.
        #   (b) a concurrent caller won the claim between our
        #       snapshot and our write. Concurrent reuse.
        #
        # Both are compromise signals: the OAuth 2.0 Security BCP
        # says burn the family. We treat them identically because
        # the cause is indistinguishable from the caller's view.
        claimed = await self._repo.claim_for_rotation(token_hash)
        if not claimed:
            await self._handle_reuse(row)
            # `_handle_reuse` always raises; defensive only.
            raise RuntimeError("unreachable")

        # Step 3 — claim won: now safe to mint and insert the
        # successor. If the insert itself fails the old row stays
        # revoked; the user must re-authenticate. That's the right
        # failure mode — auth-bound writes should never silently
        # leak a slot for a successor that didn't land.
        new_raw = _generate_token()
        new_row = await self._repo.create(
            RefreshTokenCreate(
                token_hash=_hash_token(new_raw),
                user_id=row.user_id,
                family_id=row.family_id,
                expires_at=utcnow() + ttl,
            )
        )
        # Best-effort chain-audit stamp on the OLD row. Losing this
        # write doesn't invalidate the rotation — `replaced_by` is a
        # reconstruction aid (ADR-0009 audit chain), not a gate.
        await self._repo.set_replaced_by(old_id=row.id, new_id=new_row.id)
        return new_raw, new_row

    async def _handle_reuse(self, row: RefreshTokenInDB) -> None:
        """Reuse-detection side effect + raise.

        Shared between the "row already revoked" path and the "claim
        lost to a concurrent rotate" path. Burns the family so the
        legitimate user gets bounced to re-auth, then raises
        `RefreshTokenReuseError` for the router.
        """
        # Even if the row state was clean at snapshot time, a
        # concurrent claim means the row is now revoked; we don't
        # care to fetch it again. Just burn everything else in the
        # family and propagate.
        revoked_count = await self._repo.revoke_family(row.family_id)
        raise RefreshTokenReuseError(
            details={
                "user_id": row.user_id,
                "family_id": row.family_id,
                "revoked_additional_count": revoked_count,
            },
        )

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
                details={"token_hash_prefix": self._hash_public(token_hash)},
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
