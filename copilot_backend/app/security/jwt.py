"""Minimal HS256 JWT mint + verify for access tokens.

T08 (#46) mints the short-lived access tokens we return alongside the
refresh token from the OIDC callback; T09 (#47) wires the matching
verification middleware. Both sides need to agree on the wire shape
and the algorithm — keeping them in one module makes that contract
visibly obvious.

Why HS256 and not RS256
-----------------------

ADR-0009 says "签名密钥保管在后端". For a single backend process the
HMAC variant is the simpler match — there's no need for a JWKS
round-trip and no rotation overhead. RS256 becomes worthwhile when
multiple backend replicas each need to verify without sharing a secret,
which is the scaling story for later tickets.

Why a hand-rolled HS256 instead of `PyJWT`
------------------------------------------

The backend deps (see `pyproject.toml`) don't include a JWT library
yet, and the surface we need is small: encode three base64url
segments with one HMAC over `header.payload`. Adding a dep for ~30
lines of stdlib is the wrong trade. If T09 / future tickets grow the
shape (RS256, JWKS, audience binding beyond a fixed string, key
rotation), swap to PyJWT in one commit.

Wire shape
----------

The token is the standard JWS compact form::

    base64url(header).base64url(payload).base64url(HMAC-SHA256(key, "header.payload"))

* `header` carries `{"alg": "HS256", "typ": "JWT"}`.
* `payload` carries the access-token claims (see `AccessTokenClaims`).
* The signature segment is the 32-byte HMAC tag, base64url-no-pad.

`verify_id_token` (in `app.auth.oidc`) consumes the IdP-issued
`id_token` and re-uses the same decoder path here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

# The only algorithm we mint + verify. Listing it as a Final keeps the
# verifier's `alg` allow-list explicit; unknown algorithms are
# rejected outright (no `alg: none` fallback, no algorithm
# confusion).
SUPPORTED_ALG: Final[str] = "HS256"

# Constant-time-compare: the standard idiom to avoid timing-leak
# comparisons of HMAC tags. Wrapped as a function rather than inlined so
# tests can target it.
def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    """RFC 7515 §2 base64url, no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Inverse of `_b64url_encode`. Pads the input to a multiple of 4."""
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


@dataclass(frozen=True)
class AccessTokenClaims:
    """The claims we put into our short-lived access token.

    The dataclass is the *input* to `mint_access_token`; the encoded
    JWT carries the same fields on the wire. T09 reads them back to
    authorise the request.
    """

    sub: str
    source: str
    role_ids: list[str]
    issuer: str
    audience: str
    issued_at: int
    expires_at: int
    jti: str

    def to_payload(self) -> dict[str, Any]:
        """Serialise the claims to a JSON-friendly dict."""
        return {
            "sub": self.sub,
            "source": self.source,
            "role_ids": list(self.role_ids),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "jti": self.jti,
        }


def mint_access_token(
    claims: AccessTokenClaims,
    *,
    signing_key: str,
) -> tuple[str, AccessTokenClaims]:
    """Encode `claims` as a JWS compact-form JWT.

    The encoded string is the only thing the client sees; the returned
    dataclass is the same one passed in (returned for ergonomic
    callers — see `OIDCLoginService.complete_login`).

    Args:
        claims: populated claim set. `iat` / `exp` / `jti` are
            stamped by the caller so they can pick the exact TTL.
        signing_key: HMAC secret. Must be at least 16 bytes by the
            settings guard; the function itself doesn't enforce a
            minimum because tests may pass shorter stubs.

    Returns:
        `(encoded_jwt, claims)` — `encoded_jwt` is the wire form.

    Raises:
        TypeError: claims are not JSON-serialisable (Pydantic v2
            coerces lazily; an exotic value type surfaces here).
    """
    header = {"alg": SUPPORTED_ALG, "typ": "JWT"}
    payload = claims.to_payload()

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        signing_key.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}", claims


def decode_jwt(token: str, *, signing_key: str | None = None) -> dict[str, Any]:
    """Verify and decode a JWS compact-form JWT.

    Args:
        token: three-segment dot-separated JWS string.
        signing_key: when provided, verify the HMAC tag. Omit to skip
            signature verification — useful for decoding an IdP-issued
            `id_token` whose signature is checked separately by
            `OIDCAdapter.verify_id_token` via JWKS. Callers that don't
            pass `signing_key` MUST NOT trust the returned payload as
            authenticated.

    Returns:
        The decoded JSON payload as a plain dict.

    Raises:
        ValueError: malformed token, bad segment count, unsupported
            algorithm, signature mismatch, or un-parseable JSON.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have 3 segments")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header_b = _b64url_decode(header_b64)
    except Exception as exc:  # noqa: BLE001 — binascii raises binascii.Error
        raise ValueError("JWT header is not valid base64url") from exc
    try:
        header = json.loads(header_b)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("JWT header is not valid JSON") from exc

    if not isinstance(header, dict):
        raise ValueError("JWT header must be a JSON object")
    alg = header.get("alg")
    if alg != SUPPORTED_ALG:
        raise ValueError(f"unsupported alg: {alg!r} (only {SUPPORTED_ALG})")

    if signing_key is not None:
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(
            signing_key.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        try:
            actual = _b64url_decode(signature_b64)
        except Exception as exc:  # noqa: BLE001 — binascii raises ValueError
            raise ValueError("JWT signature segment is not valid base64url") from exc
        if not _constant_time_eq(_b64url_encode(actual), _b64url_encode(expected)):
            raise ValueError("JWT signature does not match")

    try:
        payload_b = _b64url_decode(payload_b64)
    except Exception as exc:  # noqa: BLE001 — binascii raises ValueError
        raise ValueError("JWT payload is not valid base64url") from exc
    try:
        payload = json.loads(payload_b)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("JWT payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("JWT payload must be a JSON object")
    return payload


def now_unix() -> int:
    """Return the current Unix time as an int (seconds)."""
    return int(time.time())


def new_jti() -> str:
    """Return a fresh random `jti` (UUID4)."""
    return uuid.uuid4().hex


__all__ = [
    "AccessTokenClaims",
    "SUPPORTED_ALG",
    "mint_access_token",
    "decode_jwt",
    "now_unix",
    "new_jti",
]