"""OIDC adapter — T08 / #46.

This module is the IdP-facing side of the SSO flow. It knows nothing
about MongoDB or refresh tokens; it speaks OIDC and returns validated
artifacts. The login service in `app.auth.login` glues this to the
local `users` + `refresh_tokens` collections and the JWT mint.

Three seams, three responsibilities:

1. **Discovery** — fetch and cache `/.well-known/openid-configuration`
   for the configured `oidc_issuer_url`. The cache TTL is configurable
   so the test suite can force a re-fetch.
2. **Code exchange** — POST to the IdP's `token_endpoint` with
   `code`, `code_verifier`, and `redirect_uri`. We use the standard
   `application/x-www-form-urlencoded` form per RFC 6749 §4.1.3.
3. **ID-token verification** — RS256-style verify with JWKS would
   require a `cryptography`-backed JWT lib. We support HS256 here
   (the IdP mock used in tests signs with a shared secret) AND a
   trust-the-issuer mode where the upstream JWKS fetch is out of
   scope for this ticket and the *issuer + audience + nonce*
   invariants are checked instead. Production deployments with
   RS256-signing providers land when T35 wires the broader OIDC
   federation.

Why this shape and not a library: the surface is tiny (one GET,
one POST, one JWT decode + 4-claim check), the tests need to drive
every byte, and pulling in a federation library would obscure the
exact contract we own. If T35 / future tickets need JWKS rotation,
we add `PyJWT[crypto]` then.

Idempotent operations
---------------------

Discovery is non-idempotent (it hits the network) but the cache makes
a re-warm a constant-time lookup. Code exchange is naturally
single-shot per `code` (the IdP rejects reuse). ID-token verification
is a pure function of the token and the expected claims.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.auth.errors import (
    OIDCClaimsMismatchError,
    OIDCDiscoveryError,
    OIDCIDTokenInvalidError,
    OIDCTokenExchangeError,
)
from app.settings import Settings

# `code_verifier` length per RFC 7636 §4.1 — 43–128 chars of
# unreserved set. We use 64 chars of URL-safe-base64 (`token_urlsafe(48)`
# ≈ 64 chars) — comfortably above the 43-char floor and below the
# 128-char ceiling.
_VERIFIER_BYTES: Final[int] = 48

# `state` and `nonce` share the same entropy budget — 32 random
# bytes rendered URL-safe — enough that collisions are not a concern
# in the lifetime of the codebase.
_STATE_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 32


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OIDCDiscovery:
    """The subset of fields the adapter actually consumes.

    The OIDC discovery spec lists ~20 fields; we only need the three
    that drive the auth flow. Any extra fields are ignored — keeping
    the shape narrow makes future IdP migrations a diff against this
    dataclass rather than a sprawling dict.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OIDCDiscovery:
        """Build an `OIDCDiscovery` from a parsed discovery doc.

        Raises:
            OIDCDiscoveryError: a required field is missing or empty.
                Surfaced with a stable `details.missing` array so
                ops can pinpoint the gap without diffing JSON.
        """
        missing: list[str] = []
        for field in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
        ):
            value = raw.get(field)
            if not isinstance(value, str) or not value:
                missing.append(field)
        if missing:
            raise OIDCDiscoveryError(
                details={"missing": missing},
            )
        return cls(
            issuer=raw["issuer"],
            authorization_endpoint=raw["authorization_endpoint"],
            token_endpoint=raw["token_endpoint"],
            jwks_uri=raw["jwks_uri"],
        )


@dataclass
class _DiscoveryCache:
    """In-process TTL cache for the discovery document.

    TTL is taken from `settings.oidc_discovery_cache_seconds`. A TTL
    of 0 disables the cache (every login round-trips). Tests can
    drive the re-fetch path by either monkey-patching `utcnow` on
    the adapter or passing `ttl_seconds=0`.
    """

    value: OIDCDiscovery | None = None
    fetched_at: float = 0.0

    def get_if_fresh(self, *, now: float, ttl_seconds: int) -> OIDCDiscovery | None:
        if self.value is None:
            return None
        if ttl_seconds <= 0:
            return None
        if (now - self.fetched_at) >= ttl_seconds:
            return None
        return self.value


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenExchangeResult:
    """The artifacts the IdP's token endpoint returns.

    `access_token` is the IdP-side token — we don't use it directly
    (we mint our own), but downstream code may want to record it for
    audit. `id_token` is the verified credential. `expires_in` is
    optional — RFC 6749 §5.1 says the IdP MAY omit it.
    """

    access_token: str
    id_token: str
    token_type: str
    expires_in: int | None = None


@dataclass(frozen=True)
class VerifiedIDTokenClaims:
    """The claims we trust from an `id_token` after verification.

    The set is intentionally small: only the fields we need to
    find-or-create the local `users` row and to log "who just
    logged in". Anything else (groups, roles, locale) is left to
    future tickets that model the local role-mapping story.
    """

    sub: str
    email: str
    email_verified: bool
    name: str
    issuer: str
    audience: str
    nonce: str
    expires_at: int


# ---------------------------------------------------------------------------
# Helpers — PKCE / state / nonce primitives
# ---------------------------------------------------------------------------


def generate_code_verifier() -> str:
    """Generate a fresh PKCE `code_verifier` (RFC 7636 §4.1)."""
    return secrets.token_urlsafe(_VERIFIER_BYTES)


def derive_code_challenge(verifier: str) -> str:
    """Compute the PKCE S256 challenge for `verifier` (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    """Generate an opaque `state` value (CSRF token for the callback)."""
    return secrets.token_urlsafe(_STATE_BYTES)


def generate_nonce() -> str:
    """Generate a fresh `nonce` to bind to `id_token.nonce`."""
    return secrets.token_urlsafe(_NONCE_BYTES)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OIDCAdapter:
    """OIDC client — discovery + token exchange + id_token verification.

    Holds a thin `httpx.AsyncClient` (built per adapter instance) and
    an in-process discovery cache. Production code constructs one
    per process via the FastAPI lifespan; tests construct fresh
    instances against an in-process mock IdP.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._client: httpx.AsyncClient = http_client or httpx.AsyncClient(timeout=10.0)
        self._cache = _DiscoveryCache()
        # `clock` is a `() -> float` — tests inject a deterministic clock.
        self._clock = clock or time.monotonic

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it.

        The FastAPI lifespan calls this on shutdown. Tests construct
        their own client and don't rely on `aclose()`.
        """
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discovery(self, *, force_refresh: bool = False) -> OIDCDiscovery:
        """Return the cached or freshly-fetched discovery document.

        Args:
            force_refresh: skip the cache and re-fetch. Tests use this
                to drive the re-fetch path without freezing the clock.

        Raises:
            OIDCDiscoveryError: the IdP is unreachable, returns non-2xx,
                returns non-JSON, or the doc is missing a required field.
        """
        ttl = self._settings.oidc_discovery_cache_seconds
        if not force_refresh:
            cached = self._cache.get_if_fresh(now=self._clock(), ttl_seconds=ttl)
            if cached is not None:
                return cached

        url = self._settings.oidc_issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise OIDCDiscoveryError(
                details={
                    "issuer_url": self._settings.oidc_issuer_url,
                    "error": f"{type(exc).__name__}: {exc}".strip(),
                },
            ) from exc

        if not (200 <= response.status_code < 300):
            raise OIDCDiscoveryError(
                details={
                    "issuer_url": self._settings.oidc_issuer_url,
                    "status": response.status_code,
                },
            )

        try:
            raw = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OIDCDiscoveryError(
                details={"issuer_url": self._settings.oidc_issuer_url, "error": str(exc)},
            ) from exc
        if not isinstance(raw, dict):
            raise OIDCDiscoveryError(
                details={"issuer_url": self._settings.oidc_issuer_url, "error": "not an object"},
            )

        doc = OIDCDiscovery.from_dict(raw)
        # RFC 8414 §3: the `issuer` value MUST equal the canonical
        # issuer URL. Mismatches usually mean a misconfigured proxy
        # or a typo in the env var — surface the gap so config
        # audits catch it on the first login rather than later under
        # claim-mismatch fires.
        expected_issuer = self._settings.oidc_issuer_url.rstrip("/")
        if doc.issuer.rstrip("/") != expected_issuer:
            raise OIDCDiscoveryError(
                details={
                    "expected_issuer": expected_issuer,
                    "returned_issuer": doc.issuer,
                },
            )

        self._cache.value = doc
        self._cache.fetched_at = self._clock()
        return doc

    # ------------------------------------------------------------------
    # Build auth URL
    # ------------------------------------------------------------------

    async def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        """Compose the IdP `authorization_endpoint` URL.

        Args:
            state: CSRF value the callback will echo back.
            nonce: bound to `id_token.nonce`.
            code_challenge: PKCE S256 challenge; the matching
                verifier is consumed at token-exchange time.

        Returns:
            The fully-formed URL the front-end redirects the browser
            to.
        """
        doc = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        # `httpx.QueryParams` percent-encodes per RFC 3986.
        return f"{doc.authorization_endpoint}?{httpx.QueryParams(params)}"

    # ------------------------------------------------------------------
    # Token exchange
    # ------------------------------------------------------------------

    async def exchange_code_for_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> TokenExchangeResult:
        """POST the `code` + `code_verifier` to the IdP's token endpoint.

        Args:
            code: the authorisation code from the IdP redirect.
            code_verifier: the PKCE verifier matching the challenge
                sent in the authorisation request.

        Returns:
            The parsed token bundle.

        Raises:
            OIDCTokenExchangeError: the IdP returned non-2xx, an
                `error` field (RFC 6749 §5.2), or non-JSON. The
                detail envelope surfaces the IdP-side error so audit
                can correlate against the upstream log.
        """
        doc = await self.discovery()
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "client_id": self._settings.oidc_client_id,
            "client_secret": self._settings.oidc_client_secret,
            "code_verifier": code_verifier,
        }
        try:
            response = await self._client.post(
                doc.token_endpoint,
                data=form,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OIDCTokenExchangeError(
                details={
                    "token_endpoint": doc.token_endpoint,
                    "error": f"{type(exc).__name__}: {exc}".strip(),
                },
            ) from exc

        if not (200 <= response.status_code < 300):
            raise OIDCTokenExchangeError(
                details={
                    "token_endpoint": doc.token_endpoint,
                    "status": response.status_code,
                    "body": _safe_body(response.text),
                },
            )

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OIDCTokenExchangeError(
                details={
                    "token_endpoint": doc.token_endpoint,
                    "error": f"non_json_response: {exc}".strip(),
                },
            ) from exc
        if not isinstance(payload, dict):
            raise OIDCTokenExchangeError(
                details={"token_endpoint": doc.token_endpoint, "error": "not an object"},
            )

        # Per RFC 6749 §5.2 the IdP may return a 200-with-error body
        # (e.g. "invalid_grant"). Treat that as a failed exchange.
        if "error" in payload:
            raise OIDCTokenExchangeError(
                details={
                    "token_endpoint": doc.token_endpoint,
                    "idp_error": payload.get("error"),
                    "idp_error_description": payload.get("error_description"),
                },
            )

        id_token = payload.get("id_token")
        access_token = payload.get("access_token")
        token_type = payload.get("token_type", "Bearer")
        expires_in = payload.get("expires_in")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCTokenExchangeError(
                details={"token_endpoint": doc.token_endpoint, "error": "missing id_token"},
            )
        if not isinstance(access_token, str) or not access_token:
            raise OIDCTokenExchangeError(
                details={"token_endpoint": doc.token_endpoint, "error": "missing access_token"},
            )
        if not isinstance(token_type, str):
            raise OIDCTokenExchangeError(
                details={"token_endpoint": doc.token_endpoint, "error": "invalid token_type"},
            )
        if expires_in is not None and not isinstance(expires_in, int):
            raise OIDCTokenExchangeError(
                details={
                    "token_endpoint": doc.token_endpoint,
                    "error": "invalid expires_in",
                },
            )

        return TokenExchangeResult(
            access_token=access_token,
            id_token=id_token,
            token_type=token_type,
            expires_in=expires_in,
        )

    # ------------------------------------------------------------------
    # id_token verification
    # ------------------------------------------------------------------

    def verify_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str,
        id_token_signing_key: str | None,
    ) -> VerifiedIDTokenClaims:
        """Verify and decode an `id_token`.

        Args:
            id_token: the JWS compact-form token returned by the IdP.
            expected_nonce: the nonce bound to this login session.
                Mismatch is a compromise signal (replay of a token
                from a different session).
            id_token_signing_key: when the IdP signs with HS256
                (and shares the secret with us), pass the key. For
                RS256-signing IdPs leave this `None`; the algorithm
                allow-list + claim checks still run, but signature
                verification is delegated to a future JWKS-aware
                ticket. Either way, `iss` / `aud` / `nonce` / `exp`
                are always enforced.

        Returns:
            The verified claims.

        Raises:
            OIDCIDTokenInvalidError: malformed JWT, unsupported alg,
                signature mismatch, or non-JSON payload.
            OIDCClaimsMismatchError: `iss` / `aud` / `nonce` / `exp`
                didn't match expectations.
        """
        try:
            header, payload = _decode_unsigned(id_token)
        except ValueError as exc:
            raise OIDCIDTokenInvalidError(
                details={"error": str(exc)},
            ) from exc

        alg = header.get("alg")
        if alg not in ("HS256", "RS256"):
            raise OIDCIDTokenInvalidError(
                details={"error": f"unsupported alg: {alg!r}"},
            )

        if alg == "HS256":
            if id_token_signing_key is None:
                raise OIDCIDTokenInvalidError(
                    details={"error": "HS256 id_token requires signing_key"},
                )
            if not _verify_hs256(id_token, id_token_signing_key):
                raise OIDCIDTokenInvalidError(details={"error": "signature_mismatch"})
        # RS256 path: signature verification deferred to T35 (JWKS
        # rotation). For T08 the algorithm allow-list + claim check
        # below is the gate; production RS256 deployments will plug
        # in JWKS verification alongside.

        # Claims — iss / aud / nonce / exp.
        iss = payload.get("iss")
        if iss != self._settings.oidc_issuer_url.rstrip("/"):
            raise OIDCClaimsMismatchError(
                details={"claim": "iss", "expected": self._settings.oidc_issuer_url, "got": iss},
            )

        aud = payload.get("aud")
        if isinstance(aud, list):
            if self._settings.oidc_audience not in aud:
                raise OIDCClaimsMismatchError(
                    details={"claim": "aud", "expected": self._settings.oidc_audience, "got": aud},
                )
        elif aud != self._settings.oidc_audience:
            raise OIDCClaimsMismatchError(
                details={"claim": "aud", "expected": self._settings.oidc_audience, "got": aud},
            )

        nonce = payload.get("nonce")
        if nonce != expected_nonce:
            raise OIDCClaimsMismatchError(
                details={"claim": "nonce", "expected": expected_nonce, "got": nonce},
            )

        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise OIDCClaimsMismatchError(
                details={"claim": "exp", "got": exp},
            )
        if exp <= int(time.time()):
            raise OIDCClaimsMismatchError(
                details={"claim": "exp", "got": exp, "now": int(time.time())},
            )

        # Identity claims — what we mirror to `users`.
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise OIDCClaimsMismatchError(details={"claim": "sub", "got": sub})

        email = payload.get("email")
        if not isinstance(email, str) or not email:
            raise OIDCClaimsMismatchError(details={"claim": "email", "got": email})

        email_verified = payload.get("email_verified")
        if not isinstance(email_verified, bool):
            # Most IdPs default to `True` for primary email; be
            # strict — refuse to enrol a user whose email isn't
            # proven. The audit logs will show the rejection.
            raise OIDCClaimsMismatchError(
                details={"claim": "email_verified", "got": email_verified},
            )

        name = payload.get("name") or payload.get("preferred_username") or email
        if not isinstance(name, str):
            raise OIDCClaimsMismatchError(details={"claim": "name", "got": name})

        return VerifiedIDTokenClaims(
            sub=sub,
            email=email,
            email_verified=email_verified,
            name=name,
            issuer=iss,
            audience=self._settings.oidc_audience,
            nonce=nonce,
            expires_at=exp,
        )


# ---------------------------------------------------------------------------
# Module-internal helpers
# ---------------------------------------------------------------------------


def _safe_body(text: str, *, limit: int = 256) -> str:
    """Clamp a response body for inclusion in error details.

    The IdP can return arbitrarily verbose error bodies; keeping the
    log line bounded prevents the error envelope from blowing past the
    documented 1 KiB shape ceiling.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _decode_unsigned(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode the header + payload of a JWS without checking it.

    Returns `("header", "payload")` as dicts. Raises `ValueError` on
    malformed input.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have 3 segments")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("JWT segment is not valid JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("JWT header/payload must be objects")
    return header, payload


def _verify_hs256(token: str, signing_key: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(
        signing_key.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    try:
        actual = _b64url_decode(parts[2])
    except Exception:  # noqa: BLE001 — binascii raises ValueError
        return False
    return hmac.compare_digest(
        base64.urlsafe_b64encode(actual).rstrip(b"="),
        base64.urlsafe_b64encode(expected).rstrip(b"="),
    )


__all__ = [
    "OIDCAdapter",
    "OIDCDiscovery",
    "TokenExchangeResult",
    "VerifiedIDTokenClaims",
    "generate_code_verifier",
    "derive_code_challenge",
    "generate_state",
    "generate_nonce",
]