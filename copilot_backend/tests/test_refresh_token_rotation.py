"""Tests for the refresh-token rotation flow (T07 / #8).

The repository layer is covered by `tests/test_credential_repository.py`
shape tests elsewhere — here we focus on the *service-layer rules*:

* Opaque token generation, hash-only persistence.
* `issue` mints a fresh `family_id` per call.
* `verify` distinguishes "missing" / "revoked" / "expired" / "active".
* `rotate` reuses the family, marks `replaced_by`, and revokes the
  old row in a single call.
* `rotate` raises `RefreshTokenReuseError` when a previously-rotated
  token is presented again, and revokes the entire family.
* `revoke` and `revoke_family` have the idempotency / scope they
  advertise.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.auth.errors import (
    RefreshTokenExpiredError,
    RefreshTokenNotFoundError,
    RefreshTokenReuseError,
    RefreshTokenRevokedError,
)
from app.auth.tokens import (
    DEFAULT_TTL,
    RefreshTokenService,
    _generate_token,
    _hash_token,
    _new_family_id,
)
from app.db.init_db import init_database
from app.repositories.base import utcnow
from app.repositories.refresh_tokens import RefreshTokenRepository


@pytest.fixture
async def svc() -> AsyncIterator[RefreshTokenService]:
    """A service backed by an isolated in-memory Mongo instance."""
    db = AsyncMongoMockClient()["copilot_refresh_test"]
    await init_database(db)
    yield RefreshTokenService(RefreshTokenRepository(db))


# A stand-in for a real `users._id`; the auth flow doesn't depend on
# a Users row existing in the test database.
USER_ID = str(ObjectId())
OTHER_USER_ID = str(ObjectId())


# ---------------------------------------------------------------------------
# Token-format primitives
# ---------------------------------------------------------------------------


class TestTokenFormatPrimitives:
    """The low-level helpers that produce opaque tokens and ids."""

    def test_generate_token_is_url_safe_and_long_enough(self) -> None:
        """`token_urlsafe(32)` produces a 43-char URL-safe string.

        URL-safe base64 omits `+`, `/`, `=`; the test pins the format
        so a future swap to e.g. hex tokens is a deliberate, single
        change here.
        """
        for _ in range(50):
            tok = _generate_token()
            assert len(tok) >= 40, "RFC 6749 §10.10 calls for >= 128 bits of entropy"
            assert re.fullmatch(r"[A-Za-z0-9_\-]+", tok), "must be URL-safe"

    def test_hash_token_is_sha256_hex(self) -> None:
        """Hash output is a 64-char hex string matching `hashlib.sha256`."""
        tok = _generate_token()
        hashed = _hash_token(tok)
        assert len(hashed) == 64
        assert all(c in "0123456789abcdef" for c in hashed)
        # Cross-check against the stdlib.
        assert hashed == hashlib.sha256(tok.encode("ascii")).hexdigest()

    def test_hash_is_stable_for_same_input(self) -> None:
        """Same token → same hash. Pure function, no salt."""
        tok = _generate_token()
        assert _hash_token(tok) == _hash_token(tok)

    def test_hash_differs_for_different_input(self) -> None:
        """Two distinct tokens never collide (sanity sample)."""
        a, b = _generate_token(), _generate_token()
        assert _hash_token(a) != _hash_token(b)

    def test_new_family_id_is_uuid4_format(self) -> None:
        """Family ids are UUID4 strings (random 8-4-4-4-12)."""
        for _ in range(20):
            fid = _new_family_id()
            assert re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                fid,
            ), fid


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


class TestRefreshTokenIssue:
    """`issue` mints a fresh token + persists its hash."""

    @pytest.mark.asyncio
    async def test_returns_raw_token_and_persisted_row(self, svc: RefreshTokenService) -> None:
        raw, row = await svc.issue(USER_ID)

        # The raw form is what the client sees — it's an opaque string.
        assert isinstance(raw, str)
        assert len(raw) >= 40
        # The persisted row carries ONLY the hash (raw never reaches DB).
        assert row.token_hash == _hash_token(raw)
        assert row.user_id == USER_ID
        assert row.family_id
        assert row.revoked_at is None
        # TTL roughly matches the default (we don't assert exact ms).
        now = utcnow()
        assert row.expires_at - now > DEFAULT_TTL - timedelta(seconds=2)
        assert row.expires_at - now < DEFAULT_TTL + timedelta(seconds=2)
        assert row.created_at is not None

    @pytest.mark.asyncio
    async def test_issue_two_tokens_get_distinct_families(self, svc: RefreshTokenService) -> None:
        """Two independent logins must NOT share a `family_id`.

        Independent families are the foundation for reuse detection:
        if a leaked token is replayed, only its chain is revoked.
        """
        _raw_a, row_a = await svc.issue(USER_ID)
        _raw_b, row_b = await svc.issue(USER_ID)
        assert row_a.family_id != row_b.family_id
        assert row_a.id != row_b.id

    @pytest.mark.asyncio
    async def test_issue_respects_explicit_family_id(self, svc: RefreshTokenService) -> None:
        """`issue(family_id=...)` lets tests / OIDC issuance control the chain."""
        family = "0c0a4f48-dead-beef-cafe-000000000001"
        _raw, row = await svc.issue(USER_ID, family_id=family)
        assert row.family_id == family

    @pytest.mark.asyncio
    async def test_issue_rejects_non_positive_ttl(self, svc: RefreshTokenService) -> None:
        """A non-positive TTL is rejected before hitting the database."""
        with pytest.raises(ValueError):
            await svc.issue(USER_ID, ttl=timedelta(seconds=0))
        with pytest.raises(ValueError):
            await svc.issue(USER_ID, ttl=timedelta(seconds=-10))

    @pytest.mark.asyncio
    async def test_issue_records_short_ttl(self, svc: RefreshTokenService) -> None:
        """A 1-second TTL stamps `expires_at` close to now."""
        before = utcnow()
        _raw, row = await svc.issue(USER_ID, ttl=timedelta(seconds=1))
        after = utcnow()
        # `expires_at` is `now + 1s`; loose bounds so test isn't flaky.
        assert before + timedelta(seconds=1) <= row.expires_at
        assert row.expires_at <= after + timedelta(seconds=2)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestRefreshTokenVerify:
    """`verify` resolves a raw token, discriminating active vs not."""

    @pytest.mark.asyncio
    async def test_returns_row_for_active_token(self, svc: RefreshTokenService) -> None:
        raw, _row = await svc.issue(USER_ID)
        verified = await svc.verify(raw)
        assert verified.user_id == USER_ID
        assert verified.revoked_at is None

    @pytest.mark.asyncio
    async def test_unknown_token_raises_not_found(self, svc: RefreshTokenService) -> None:
        with pytest.raises(RefreshTokenNotFoundError) as exc:
            await svc.verify("definitely-not-a-real-token")
        assert exc.value.code == "refresh_token_not_found"

    @pytest.mark.asyncio
    async def test_revoked_token_raises_revoked(self, svc: RefreshTokenService) -> None:
        raw, _row = await svc.issue(USER_ID)
        await svc.revoke(raw)
        with pytest.raises(RefreshTokenRevokedError):
            await svc.verify(raw)

    @pytest.mark.asyncio
    async def test_expired_token_raises_expired(
        self, svc: RefreshTokenService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token whose `expires_at` is in the past is rejected.

        Driving past-expiry through the public `issue(...)` path is
        impossible (it requires a positive TTL) and writing a past
        row directly hits the test DB's TTL index — which deletes
        the row on the spot. Instead we issue normally and freeze
        the service's clock far in the future so `expires_at` is
        now behind "now".
        """
        from app.auth import tokens as tokens_module

        raw, _row = await svc.issue(USER_ID)
        far_future = utcnow() + timedelta(days=30)
        monkeypatch.setattr(tokens_module, "utcnow", lambda: far_future)

        with pytest.raises(RefreshTokenExpiredError) as exc:
            await svc.verify(raw)
        assert exc.value.code == "refresh_token_expired"


# ---------------------------------------------------------------------------
# Rotate — happy path
# ---------------------------------------------------------------------------


class TestRefreshTokenRotateHappyPath:
    """`rotate` issues a successor in the same family."""

    @pytest.mark.asyncio
    async def test_rotate_preserves_family_and_user(self, svc: RefreshTokenService) -> None:
        raw_a, row_a = await svc.issue(USER_ID)
        raw_b, row_b = await svc.rotate(raw_a)

        assert raw_b != raw_a, "rotation must produce a fresh opaque token"
        assert row_b.family_id == row_a.family_id, "family is the rotation chain"
        assert row_b.user_id == USER_ID
        assert row_b.revoked_at is None

    @pytest.mark.asyncio
    async def test_old_token_is_revoked_after_rotation(self, svc: RefreshTokenService) -> None:
        raw_a, row_a = await svc.issue(USER_ID)
        _raw_b, row_b = await svc.rotate(raw_a)

        # The old hash now has a non-null `revoked_at`. `verify`
        # raises `RefreshTokenRevokedError` because the old token was
        # already rotated.
        with pytest.raises(RefreshTokenRevokedError):
            await svc.verify(raw_a)

        # Inspect the row directly through the repo to confirm timing
        # and the chain link.
        old = await svc._repo.get_by_hash(_hash_token(raw_a))
        assert old.revoked_at is not None
        # `replaced_by` on the OLD row points at the NEW row's id.
        assert old.replaced_by == row_b.id

    @pytest.mark.asyncio
    async def test_rotation_supports_unlimited_chain_length(self, svc: RefreshTokenService) -> None:
        """Repeated rotations accumulate a chain under one family."""
        raw, _ = await svc.issue(USER_ID)
        family = (await svc.verify(raw)).family_id
        for _ in range(5):
            raw, row = await svc.rotate(raw)
            assert row.family_id == family
        # Family now has 6 rows: the original + 5 successors.
        rows = await svc.list_family(family)
        assert len(rows) == 6
        assert {r.revoked_at is not None for r in rows} == {True, False}

    @pytest.mark.asyncio
    async def test_rotation_unknown_token_raises_not_found(self, svc: RefreshTokenService) -> None:
        with pytest.raises(RefreshTokenNotFoundError):
            await svc.rotate("never-was-issued")


# ---------------------------------------------------------------------------
# Rotate — reuse detection (the security headline of T07)
# ---------------------------------------------------------------------------


class TestRefreshTokenRotateReuseDetection:
    """Re-presenting a rotated token revokes the whole family."""

    @pytest.mark.asyncio
    async def test_reuse_revokes_whole_family_and_raises(self, svc: RefreshTokenService) -> None:
        """A leaked-then-rotated token triggers family revocation.

        Setup: A token T0 is issued. The legitimate client rotates it
        to T1, which is now the active token. The attacker replays T0.
        """
        raw_old, row_old = await svc.issue(USER_ID)
        family = row_old.family_id
        # Legitimate user rotates (T0 → T1).
        await svc.rotate(raw_old)

        with pytest.raises(RefreshTokenReuseError) as exc:
            await svc.rotate(raw_old)
        assert exc.value.code == "refresh_token_reuse_detected"
        # Every active token in the family must now be revoked.
        rows = await svc.list_family(family)
        assert len(rows) >= 2
        assert all(r.revoked_at is not None for r in rows)

    @pytest.mark.asyncio
    async def test_reuse_does_not_touch_unrelated_family(self, svc: RefreshTokenService) -> None:
        """A reuse event revokes the offending family only.

        Confirming the "don't log the user out elsewhere" invariant
        called out in the service docstring.
        """
        # Family A: will be the reuse victim.
        raw_a, row_a = await svc.issue(USER_ID)
        family_a = row_a.family_id
        await svc.rotate(raw_a)  # A0 → A1

        # Family B: independent login, untouched.
        _raw_b, row_b = await svc.issue(USER_ID)
        family_b = row_b.family_id

        # Replay A0 → A1 already revoked; reuse must burn only A.
        with pytest.raises(RefreshTokenReuseError):
            await svc.rotate(raw_a)

        family_a_rows = await svc.list_family(family_a)
        family_b_rows = await svc.list_family(family_b)
        assert all(r.revoked_at is not None for r in family_a_rows)
        assert all(r.revoked_at is None for r in family_b_rows), (
            "reuse detection must not touch an unrelated family"
        )

    @pytest.mark.asyncio
    async def test_reuse_after_force_revoke_also_burns_family(
        self, svc: RefreshTokenService
    ) -> None:
        """A rotated token's `revoke(...)` outcome is the same as reuse.

        We rotate T0 → T1, then explicitly revoke T0. Replaying T0
        now triggers the same family-burn.
        """
        raw_old, _ = await svc.issue(USER_ID)
        family = (await svc.verify(raw_old)).family_id
        await svc.rotate(raw_old)
        # Re-revoke is idempotent; no exception expected.
        await svc.revoke(raw_old)

        with pytest.raises(RefreshTokenReuseError):
            await svc.rotate(raw_old)
        # All rows in family should now carry `revoked_at`.
        rows = await svc.list_family(family)
        assert all(r.revoked_at is not None for r in rows)


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRefreshTokenRevoke:
    """Single-token + family-wide revocation."""

    @pytest.mark.asyncio
    async def test_revoke_stamps_revoked_at(self, svc: RefreshTokenService) -> None:
        raw, _ = await svc.issue(USER_ID)
        revoked = await svc.revoke(raw)
        assert isinstance(revoked.revoked_at, datetime)

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent(self, svc: RefreshTokenService) -> None:
        """Re-revoking the same token leaves the original `revoked_at`."""
        raw, _ = await svc.issue(USER_ID)
        first = await svc.revoke(raw)
        second = await svc.revoke(raw)
        assert first.revoked_at == second.revoked_at

    @pytest.mark.asyncio
    async def test_revoke_unknown_token_raises(self, svc: RefreshTokenService) -> None:
        with pytest.raises(RefreshTokenNotFoundError):
            await svc.revoke("never-issued")

    @pytest.mark.asyncio
    async def test_revoke_family_flips_active_token(self, svc: RefreshTokenService) -> None:
        """`revoke_family` flips every active row in the family to revoked.

        Per the rotation invariant, a family has AT MOST one active
        token at a time (rotation revokes the old). We exercise both
        shapes: pre-rotation (single active token) and after-rotation
        (the active one becomes the only flipped row).
        """
        raw, row = await svc.issue(USER_ID)
        # Pre-rotation: one active row in the family.
        assert await svc.revoke_family(row.family_id) == 1
        # Idempotent re-run — already-revoked rows stay put.
        assert await svc.revoke_family(row.family_id) == 0

        rows = await svc.list_family(row.family_id)
        assert rows and all(r.revoked_at is not None for r in rows)

    @pytest.mark.asyncio
    async def test_revoke_family_leaves_already_revoked_alone(
        self, svc: RefreshTokenService
    ) -> None:
        """Already-revoked rows are not re-stamped by `revoke_family`."""
        raw, row = await svc.issue(USER_ID)
        _new_raw, _new = await svc.rotate(raw)  # old row now revoked
        # Only the new (active) row is flipped.
        flipped = await svc.revoke_family(row.family_id)
        assert flipped == 1

    @pytest.mark.asyncio
    async def test_revoke_all_for_user_spans_families(self, svc: RefreshTokenService) -> None:
        """Force-logout revokes tokens across every family for the user."""
        # Two independent logins for the same user → two families.
        _raw_a, row_a = await svc.issue(USER_ID)
        _raw_b, row_b = await svc.issue(USER_ID)
        assert row_a.family_id != row_b.family_id

        # Also issue one for a different user → must not be touched.
        _raw_other, row_other = await svc.issue(OTHER_USER_ID)

        count = await svc.revoke_all_for_user(USER_ID)
        # At least 2 from `USER_ID`; the exact number depends on TTL.
        assert count >= 2

        rows_user = await svc.list_family(row_a.family_id) + await svc.list_family(
            row_b.family_id
        )
        assert all(r.revoked_at is not None for r in rows_user)

        other = await svc.list_family(row_other.family_id)
        assert all(r.revoked_at is None for r in other), (
            "force-logout must not span users"
        )


# ---------------------------------------------------------------------------
# Family inspection helper
# ---------------------------------------------------------------------------


class TestRefreshTokenListFamily:
    """`list_family` returns the rotation chain oldest-first."""

    @pytest.mark.asyncio
    async def test_list_family_orders_by_created_at(self, svc: RefreshTokenService) -> None:
        raw, row = await svc.issue(USER_ID)
        for _ in range(3):
            raw, _ = await svc.rotate(raw)

        rows = await svc.list_family(row.family_id)
        assert len(rows) == 4
        # Oldest first.
        timestamps = [r.created_at for r in rows]
        assert timestamps == sorted(timestamps)

    @pytest.mark.asyncio
    async def test_list_family_unknown_returns_empty(self, svc: RefreshTokenService) -> None:
        rows = await svc.list_family("nonexistent-family")
        assert rows == []
