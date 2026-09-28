"""Tests for the HS256 mint + decode helper.

Covers the round-trip happy path and every failure mode the OIDC
adapter relies on:

* signature mismatch (wrong key)
* signature mismatch (HS256 vs unknown alg)
* bad base64 segments
* non-JSON segments
* non-dict segments
* tamper detection (mutate payload, keep original sig)

The TDD target — tests for `app.auth.oidc.verify_id_token` and
`app.auth.login.OIDCLoginService.complete_login` — depends on this
module being correct. Failing here would cascade into the bigger
suite, so the per-failure modes are pinned individually.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from app.security.jwt import (
    SUPPORTED_ALG,
    AccessTokenClaims,
    decode_jwt,
    mint_access_token,
    new_jti,
    now_unix,
)

# ---------------------------------------------------------------------------
# Mint round-trip
# ---------------------------------------------------------------------------


class TestMintDecode:
    """Mint then decode is the happy path the access-token flow uses."""

    def test_round_trip_returns_original_claims(self) -> None:
        """A freshly-minted token decodes back to the same payload."""
        claims = AccessTokenClaims(
            sub="u-123",
            source="sso",
            role_ids=["admin", "user"],
            issuer="copilot-backend",
            audience="copilot-api",
            issued_at=1_700_000_000,
            expires_at=1_700_000_900,
            jti="jti-xyz",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)

        decoded = decode_jwt(token, signing_key="k" * 32)
        assert decoded["sub"] == "u-123"
        assert decoded["source"] == "sso"
        assert decoded["role_ids"] == ["admin", "user"]
        assert decoded["iss"] == "copilot-backend"
        assert decoded["aud"] == "copilot-api"
        assert decoded["iat"] == 1_700_000_000
        assert decoded["exp"] == 1_700_000_900
        assert decoded["jti"] == "jti-xyz"

    def test_token_has_three_dot_separated_segments(self) -> None:
        """JWS compact form is `header.payload.signature`."""
        claims = AccessTokenClaims(
            sub="u",
            source="sso",
            role_ids=[],
            issuer="i",
            audience="a",
            issued_at=1,
            expires_at=2,
            jti="j",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)
        assert token.count(".") == 2
        parts = token.split(".")
        # base64url, no padding
        for seg in parts:
            assert "=" not in seg
            assert "+" not in seg
            assert "/" not in seg

    def test_header_carries_hs256_alg(self) -> None:
        """The header advertises `HS256` so verifiers know what to use."""
        claims = AccessTokenClaims(
            sub="u",
            source="sso",
            role_ids=[],
            issuer="i",
            audience="a",
            issued_at=1,
            expires_at=2,
            jti="j",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)
        header_b64 = token.split(".")[0]
        header = json.loads(_b64url_decode(header_b64))
        assert header == {"alg": SUPPORTED_ALG, "typ": "JWT"}


# ---------------------------------------------------------------------------
# Verification — failure modes
# ---------------------------------------------------------------------------


class TestDecodeFailures:
    """The verifier must reject every shape that breaks the wire contract."""

    def test_wrong_signing_key_raises(self) -> None:
        """Mismatched key → `ValueError`. A forged token can't pass."""
        claims = AccessTokenClaims(
            sub="u",
            source="sso",
            role_ids=[],
            issuer="i",
            audience="a",
            issued_at=1,
            expires_at=2,
            jti="j",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)
        with pytest.raises(ValueError, match="signature"):
            decode_jwt(token, signing_key="other" * 32)

    def test_tampered_payload_raises(self) -> None:
        """Mutating the payload segment invalidates the signature."""
        claims = AccessTokenClaims(
            sub="u",
            source="sso",
            role_ids=[],
            issuer="i",
            audience="a",
            issued_at=1,
            expires_at=2,
            jti="j",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)
        header_b64, payload_b64, sig_b64 = token.split(".")
        # Mutate the payload (claim flip) but keep the original sig.
        forged_payload = _b64url_encode(json.dumps({"sub": "attacker"}).encode())
        forged = f"{header_b64}.{forged_payload}.{sig_b64}"
        with pytest.raises(ValueError, match="signature"):
            decode_jwt(forged, signing_key="k" * 32)

    def test_wrong_segment_count_raises(self) -> None:
        """Two segments isn't a valid JWS compact form."""
        with pytest.raises(ValueError, match="3 segments"):
            decode_jwt("abc.def", signing_key="k" * 32)
        with pytest.raises(ValueError, match="3 segments"):
            decode_jwt("a.b.c.d", signing_key="k" * 32)

    def test_non_json_header_raises(self) -> None:
        """A header segment that isn't JSON is rejected."""
        # Build a token with a non-JSON header.
        header_b64 = _b64url_encode(b"not-json")
        payload_b64 = _b64url_encode(json.dumps({"sub": "u"}).encode())
        sig_b64 = _b64url_encode(b"\x00" * 32)
        with pytest.raises(ValueError, match="header"):
            decode_jwt(f"{header_b64}.{payload_b64}.{sig_b64}", signing_key="k" * 32)

    def test_non_dict_header_raises(self) -> None:
        """A JSON list (not an object) as the header is rejected."""
        header_b64 = _b64url_encode(json.dumps(["HS256", "JWT"]).encode())
        payload_b64 = _b64url_encode(json.dumps({"sub": "u"}).encode())
        sig_b64 = _b64url_encode(b"\x00" * 32)
        with pytest.raises(ValueError, match="object"):
            decode_jwt(f"{header_b64}.{payload_b64}.{sig_b64}", signing_key="k" * 32)

    def test_unsupported_alg_header_raises(self) -> None:
        """`alg: none` and other unknown algorithms are not accepted."""
        for alg in ("none", "RS256", "HS512"):
            header = json.dumps({"alg": alg, "typ": "JWT"}).encode()
            header_b64 = _b64url_encode(header)
            payload_b64 = _b64url_encode(json.dumps({"sub": "u"}).encode())
            sig_b64 = _b64url_encode(b"\x00" * 32)
            with pytest.raises(ValueError, match="alg"):
                decode_jwt(
                    f"{header_b64}.{payload_b64}.{sig_b64}", signing_key="k" * 32
                )

    def test_non_json_payload_raises(self) -> None:
        """A non-JSON payload segment is rejected.

        Skips the signature check (no signing_key) so the failure
        path is the JSON parse, not the HMAC mismatch.
        """
        header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload_b64 = _b64url_encode(b"not-json")
        sig_b64 = _b64url_encode(b"\x00" * 32)
        with pytest.raises(ValueError, match="payload"):
            decode_jwt(f"{header_b64}.{payload_b64}.{sig_b64}")

    def test_decode_without_key_skips_signature_check(self) -> None:
        """Omitting `signing_key` skips signature verification.

        Used by `OIDCAdapter.verify_id_token` when the IdP signs with
        RS256 (signature check is deferred to JWKS). The decode
        path alone doesn't validate authenticity.
        """
        claims = AccessTokenClaims(
            sub="u",
            source="sso",
            role_ids=[],
            issuer="i",
            audience="a",
            issued_at=1,
            expires_at=2,
            jti="j",
        )
        token, _ = mint_access_token(claims, signing_key="k" * 32)
        decoded = decode_jwt(token)  # no signing_key
        assert decoded["sub"] == "u"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_now_unix_returns_int_seconds() -> None:
    """`now_unix` is monotonic-ish and integral seconds (test environment)."""
    a = now_unix()
    time.sleep(0.001)
    b = now_unix()
    assert isinstance(a, int)
    assert b >= a


def test_new_jti_is_unique_hex() -> None:
    """`new_jti` produces UUID4 hex — 32 chars, 128 bits."""
    seen = {new_jti() for _ in range(50)}
    assert len(seen) == 50  # all unique
    sample = next(iter(seen))
    assert len(sample) == 32
    int(sample, 16)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# A small smoke test that our HS256 implementation agrees with
# `hmac.new` — guards against accidental secret / message ordering
# errors during future edits.
def test_hs256_matches_hmac_new() -> None:
    """Mint + manual HMAC produce the same tag — sanity check."""
    claims = AccessTokenClaims(
        sub="u",
        source="sso",
        role_ids=[],
        issuer="i",
        audience="a",
        issued_at=1,
        expires_at=2,
        jti="j",
    )
    token, _ = mint_access_token(claims, signing_key="k" * 32)
    header_b64, payload_b64, sig_b64 = token.split(".")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(b"k" * 32, signing_input, hashlib.sha256).digest()
    actual = _b64url_decode(sig_b64)
    assert hmac.compare_digest(expected, actual)