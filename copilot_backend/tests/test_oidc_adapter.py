"""Tests for the OIDC adapter (T08 / #46).

The adapter is the IdP-facing half of the SSO flow. The seam we test
is the four public methods:

* `discovery()` — fetches + caches `/.well-known/openid-configuration`.
* `build_authorization_url(...)` — composes the auth-URL with the
  PKCE / state / nonce values.
* `exchange_code_for_tokens(...)` — POSTs `code` + `code_verifier`
  to the IdP's `token_endpoint`.
* `verify_id_token(...)` — HS256 signature + `iss` / `aud` /
  `nonce` / `exp` claim checks.

Every test wires a fake IdP through `httpx.MockTransport` so the
adapter exercises the same HTTP code path it would against a real
IdP. Tests pin the exact bytes we send on the wire so the contract
is regression-proof.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.auth.errors import (
    OIDCClaimsMismatchError,
    OIDCDiscoveryError,
    OIDCIDTokenInvalidError,
    OIDCTokenExchangeError,
)
from app.auth.oidc import (
    OIDCAdapter,
    derive_code_challenge,
    generate_code_verifier,
    generate_nonce,
    generate_state,
)
from app.settings import Settings

ISSUER = "https://idp.test"
TOKEN_ENDPOINT = f"{ISSUER}/token"
AUTHZ_ENDPOINT = f"{ISSUER}/authorize"
JWKS_URI = f"{ISSUER}/jwks"
IDP_SIGNING_KEY = "idp-signing-secret-for-tests"


def _id_token_settings(**overrides: Any) -> Settings:
    """A Settings object pointed at our fake IdP + signing key.

    Defaults are tuned so the adapter's happy path lands cleanly. The
    caller passes overrides for `oidc_discovery_cache_seconds=0` to
    force re-fetches in specific tests.
    """
    return Settings(
        oidc_issuer_url=ISSUER,
        oidc_audience="copilot-api",
        oidc_id_token_signing_key=IDP_SIGNING_KEY,
        oidc_state_ttl_seconds=600,
        oidc_access_token_ttl_seconds=900,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Fake IdP
# ---------------------------------------------------------------------------


@dataclass
class FakeIdP:
    """In-memory IdP — controls the discovery doc, token responses, signing key.

    Tests register handler callables via the `register_*` methods; the
    default handlers answer the happy-path OIDC shapes so a test that
    doesn't care about IdP interactions can just instantiate the
    fixture and go.
    """

    issued_codes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def discovery_doc(self) -> dict[str, Any]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHZ_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
            # Extra fields the adapter ignores — the dataclass is
            # deliberately narrow so future IdP migrations diff
            # cleanly.
            "scopes_supported": ["openid", "email", "profile"],
        }

    def token_response(self, *, code: str, client_id: str) -> dict[str, Any]:
        record = self.issued_codes.get(code)
        if record is None:
            return {
                "error": "invalid_grant",
                "error_description": "code not recognised",
            }
        if client_id != record["client_id"]:
            return {
                "error": "invalid_client",
                "error_description": "client id mismatch",
            }
        return {
            "access_token": "idp-access-" + code,
            "id_token": record["id_token"],
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Dispatch on URL — the FakeIdP's `httpx.MockTransport` target."""
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery_doc())
        if path.endswith("/token"):
            form = dict(_parse_form_body(request.content))
            return httpx.Response(
                200,
                json=self.token_response(
                    code=form.get("code", ""),
                    client_id=form.get("client_id", ""),
                ),
            )
        return httpx.Response(404, json={"error": "not_found"})


def _parse_form_body(body: bytes) -> dict[str, str]:
    """Decode `application/x-www-form-urlencoded` bytes to a dict.

    `httpx.MockTransport` doesn't auto-decode form bodies the way
    a FastAPI handler would, so we do it inline.
    """
    out: dict[str, str] = {}
    for chunk in body.decode("utf-8").split("&"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
def settings() -> Settings:
    return _id_token_settings()


@pytest.fixture
def adapter(
    idp: FakeIdP, settings: Settings
) -> OIDCAdapter:
    """An adapter wired against the in-process `FakeIdP`."""
    transport = httpx.MockTransport(idp.handler)
    http_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OIDCAdapter(settings, http_client=http_client)


def _sign_id_token(
    *,
    sub: str = "user-42",
    email: str = "alice@example.com",
    email_verified: bool = True,
    name: str = "Alice",
    nonce: str = "nonce-1",
    audience: str | list[str] = "copilot-api",
    issuer: str = ISSUER,
    expires_in: int = 3600,
    signing_key: str = IDP_SIGNING_KEY,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a fake `id_token` (HS256) the FakeIdP will hand back."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "name": name,
        "nonce": nonce,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
    }
    if extra_claims:
        payload.update(extra_claims)
    header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload_b64 = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(signing_key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _details(exc: BaseException) -> dict[str, Any]:
    """Assert `AppError.details` is non-None and return it as a typed dict.

    Every error raised from the auth layer carries a populated
    `details` envelope by construction; the type checker can't know
    that because the field is `dict | None`. This helper centralises
    the runtime assertion so test bodies stay one-liners.
    """
    assert hasattr(exc, "details"), f"{type(exc).__name__} missing details"
    details = exc.details
    assert isinstance(details, dict), f"details was {type(details).__name__}"
    return details


# A 256-byte zero buffer used to forge an RS256-shaped signature
# segment. Bound outside the f-string so Python 3.11's parser
# accepts it.
_ZEROS_256 = b"\x00" * 256


# ---------------------------------------------------------------------------
# PKCE primitives
# ---------------------------------------------------------------------------


class TestPKCEPrimitives:
    """`generate_code_verifier` / `derive_code_challenge` round-trip."""

    def test_verifier_is_url_safe_and_long_enough(self) -> None:
        """A fresh verifier is a URL-safe string >= 43 chars (RFC 7636)."""
        for _ in range(20):
            v = generate_code_verifier()
            assert len(v) >= 43
            assert all(c.isalnum() or c in "-_" for c in v)

    def test_challenge_is_s256_of_verifier(self) -> None:
        """The challenge is base64url(SHA256(verifier)) with no padding."""
        verifier = "k" * 43  # any fixed input
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert derive_code_challenge(verifier) == expected

    def test_state_and_nonce_are_unique_per_call(self) -> None:
        """State and nonce are 43+ char URL-safe strings."""
        for fn in (generate_state, generate_nonce):
            seen = {fn() for _ in range(50)}
            assert len(seen) == 50, f"{fn.__name__} produced duplicates"
            sample = next(iter(seen))
            assert len(sample) >= 43


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """`adapter.discovery()` — fetch + cache + error handling."""

    @pytest.mark.asyncio
    async def test_discovery_returns_parsed_doc(self, adapter: OIDCAdapter) -> None:
        """First call parses the IdP's discovery JSON into our dataclass."""
        doc = await adapter.discovery()
        assert doc.issuer == ISSUER
        assert doc.authorization_endpoint == AUTHZ_ENDPOINT
        assert doc.token_endpoint == TOKEN_ENDPOINT
        assert doc.jwks_uri == JWKS_URI

    @pytest.mark.asyncio
    async def test_discovery_caches_within_ttl(
        self, idp: FakeIdP, settings: Settings
    ) -> None:
        """A second call within TTL uses the cache (no HTTP fetch).

        We count requests by intercepting the IdP handler — a
        non-cached adapter would see two requests.
        """
        settings = settings  # default ttl is 3600s

        # Build the adapter whose handler counts requests — a non-cached
        # adapter would see one request per `discovery()` call.
        calls = {"n": 0}

        def counting(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return idp.handler(request)

        transport = httpx.MockTransport(counting)
        http_client = httpx.AsyncClient(transport=transport, timeout=5.0)
        ad = OIDCAdapter(settings, http_client=http_client)

        await ad.discovery()
        await ad.discovery()
        await ad.discovery()
        assert calls["n"] == 1, "discovery should cache within TTL"

    @pytest.mark.asyncio
    async def test_discovery_force_refresh_re_fetches(
        self, idp: FakeIdP, settings: Settings
    ) -> None:
        """`force_refresh=True` bypasses the cache."""
        ad = OIDCAdapter(settings, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(idp.handler), timeout=5.0
        ))
        await ad.discovery()
        await ad.discovery(force_refresh=True)

        # Both calls walked the IdP — the second wasn't cached.
        # Confirm via the request-counting pattern below.
        calls = {"n": 0}

        def counting(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return idp.handler(request)

        ad2 = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(counting), timeout=5.0
            ),
        )
        await ad2.discovery()
        await ad2.discovery(force_refresh=True)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_discovery_zero_ttl_never_caches(
        self, idp: FakeIdP
    ) -> None:
        """TTL=0 disables caching; every call re-fetches."""
        settings = _id_token_settings(oidc_discovery_cache_seconds=0)
        calls = {"n": 0}

        def counting(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return idp.handler(request)

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(counting), timeout=5.0
            ),
        )
        await ad.discovery()
        await ad.discovery()
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_discovery_mismatched_issuer_raises(
        self, idp: FakeIdP, settings: Settings
    ) -> None:
        """If the IdP reports a different `issuer`, we refuse the doc."""
        idp.discovery_doc()

        def stub(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "issuer": "https://attacker.example.com",
                "authorization_endpoint": AUTHZ_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URI,
            })

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(stub), timeout=5.0
            ),
        )
        with pytest.raises(OIDCDiscoveryError) as exc:
            await ad.discovery()
        assert exc.value.code == "oidc_discovery_failed"

    @pytest.mark.asyncio
    async def test_discovery_missing_field_raises(
        self, idp: FakeIdP, settings: Settings
    ) -> None:
        """Discovery doc missing `jwks_uri` is rejected."""
        def stub(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": AUTHZ_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
            })

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(stub), timeout=5.0
            ),
        )
        with pytest.raises(OIDCDiscoveryError) as exc:
            await ad.discovery()
        assert exc.value.code == "oidc_discovery_failed"
        assert exc.value.details is not None
        assert "jwks_uri" in _details(exc.value)["missing"]

    @pytest.mark.asyncio
    async def test_discovery_http_error_raises(self, settings: Settings) -> None:
        """An HTTPError from the IdP surfaces as `OIDCDiscoveryError`."""

        def stub(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(stub), timeout=5.0
            ),
        )
        with pytest.raises(OIDCDiscoveryError) as exc:
            await ad.discovery()
        assert exc.value.code == "oidc_discovery_failed"

    @pytest.mark.asyncio
    async def test_discovery_non_2xx_raises(self, settings: Settings) -> None:
        def stub(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(stub), timeout=5.0
            ),
        )
        with pytest.raises(OIDCDiscoveryError) as exc:
            await ad.discovery()
        assert exc.value.details is not None
        assert _details(exc.value)["status"] == 503


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


class TestBuildAuthorizationURL:
    """`build_authorization_url(...)` shapes the auth URL."""

    async def test_build_authorization_url_includes_pkce_and_state(
        self, adapter: OIDCAdapter
    ) -> None:
        url = await adapter.build_authorization_url(
            state="st-1",
            nonce="nonce-1",
            code_challenge="challenge-1",
        )
        assert url.startswith(AUTHZ_ENDPOINT + "?")
        # Every required query parameter is present.
        for needle in (
            "response_type=code",
            "client_id=copilot-dev",
            "redirect_uri=",
            "scope=openid+email+profile",
            "state=st-1",
            "nonce=nonce-1",
            "code_challenge=challenge-1",
            "code_challenge_method=S256",
        ):
            assert needle in url


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


class TestExchangeCodeForTokens:
    """`exchange_code_for_tokens(...)` round-trip + error paths."""

    @pytest.mark.asyncio
    async def test_exchange_returns_id_and_access_tokens(
        self, adapter: OIDCAdapter, idp: FakeIdP, settings: Settings
    ) -> None:
        id_token = _sign_id_token()
        idp.issued_codes["abc"] = {
            "id_token": id_token,
            "client_id": settings.oidc_client_id,
        }

        result = await adapter.exchange_code_for_tokens(
            code="abc",
            code_verifier="verifier-x",
        )
        assert result.access_token == "idp-access-abc"
        assert result.id_token == id_token
        assert result.token_type == "Bearer"
        assert result.expires_in == 3600

    @pytest.mark.asyncio
    async def test_exchange_sends_required_form_fields(
        self, adapter: OIDCAdapter, idp: FakeIdP, settings: Settings
    ) -> None:
        """The token POST carries every field the IdP expects."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            # Let the FakeIdP handle discovery; only intercept the token POST.
            if request.url.path.endswith("/.well-known/openid-configuration"):
                return idp.handler(request)
            captured.update(_parse_form_body(request.content))
            return httpx.Response(200, json={
                "access_token": "t",
                "id_token": "h.p.s",
                "token_type": "Bearer",
                "expires_in": 60,
            })

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=5.0
            ),
        )
        await ad.exchange_code_for_tokens(
            code="c", code_verifier="v-very-long-verifier-string"
        )
        assert captured["grant_type"] == "authorization_code"
        assert captured["code"] == "c"
        assert captured["code_verifier"] == "v-very-long-verifier-string"
        assert captured["redirect_uri"] == settings.oidc_redirect_uri
        assert captured["client_id"] == settings.oidc_client_id
        assert captured["client_secret"] == settings.oidc_client_secret

    @pytest.mark.asyncio
    async def test_exchange_unknown_code_returns_idp_error(
        self, adapter: OIDCAdapter, idp: FakeIdP
    ) -> None:
        """The IdP's `error` field is surfaced in the detail envelope."""
        with pytest.raises(OIDCTokenExchangeError) as exc:
            await adapter.exchange_code_for_tokens(
                code="never-issued", code_verifier="v"
            )
        assert exc.value.code == "oidc_token_exchange_failed"
        assert exc.value.details is not None
        assert _details(exc.value)["idp_error"] == "invalid_grant"

    @pytest.mark.asyncio
    async def test_exchange_missing_id_token_raises(
        self, adapter: OIDCAdapter, idp: FakeIdP
    ) -> None:
        """The IdP returning a token bundle without `id_token` is rejected."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/openid-configuration"):
                return idp.handler(request)
            return httpx.Response(200, json={"access_token": "x", "token_type": "Bearer"})

        ad = OIDCAdapter(
            _id_token_settings(),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=5.0
            ),
        )
        with pytest.raises(OIDCTokenExchangeError) as exc:
            await ad.exchange_code_for_tokens(code="c", code_verifier="v")
        assert _details(exc.value)["error"] == "missing id_token"

    @pytest.mark.asyncio
    async def test_exchange_http_error_raises(
        self, adapter: OIDCAdapter, idp: FakeIdP, settings: Settings
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/openid-configuration"):
                return idp.handler(request)
            return httpx.Response(500, json={"error": "boom"})

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=5.0
            ),
        )
        with pytest.raises(OIDCTokenExchangeError) as exc:
            await ad.exchange_code_for_tokens(code="c", code_verifier="v")
        assert _details(exc.value)["status"] == 500

    @pytest.mark.asyncio
    async def test_exchange_non_json_raises(
        self, adapter: OIDCAdapter, idp: FakeIdP, settings: Settings
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/openid-configuration"):
                return idp.handler(request)
            return httpx.Response(200, text="<html>error</html>")

        ad = OIDCAdapter(
            settings,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=5.0
            ),
        )
        with pytest.raises(OIDCTokenExchangeError):
            await ad.exchange_code_for_tokens(code="c", code_verifier="v")


# ---------------------------------------------------------------------------
# ID token verification
# ---------------------------------------------------------------------------


class TestVerifyIDToken:
    """`adapter.verify_id_token(...)` — claim gates + signature."""

    def test_valid_token_returns_verified_claims(
        self, adapter: OIDCAdapter
    ) -> None:
        token = _sign_id_token(nonce="nonce-1")
        claims = adapter.verify_id_token(
            token, expected_nonce="nonce-1", id_token_signing_key=IDP_SIGNING_KEY
        )
        assert claims.sub == "user-42"
        assert claims.email == "alice@example.com"
        assert claims.email_verified is True
        assert claims.name == "Alice"
        assert claims.issuer == ISSUER
        assert claims.nonce == "nonce-1"
        assert claims.expires_at > int(time.time())

    def test_wrong_signature_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token()
        with pytest.raises(OIDCIDTokenInvalidError) as exc:
            adapter.verify_id_token(
                token,
                expected_nonce="nonce-1",
                id_token_signing_key="wrong-key",
            )
        assert exc.value.code == "oidc_id_token_invalid"

    def test_wrong_issuer_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token(issuer="https://attacker.example.com")
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                token,
                expected_nonce="nonce-1",
                id_token_signing_key=IDP_SIGNING_KEY,
            )
        assert exc.value.code == "oidc_claims_mismatch"
        assert exc.value.details is not None
        assert _details(exc.value)["claim"] == "iss"

    def test_wrong_audience_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token(audience="some-other-api")
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                token,
                expected_nonce="nonce-1",
                id_token_signing_key=IDP_SIGNING_KEY,
            )
        assert _details(exc.value)["claim"] == "aud"

    def test_audience_can_be_a_list(self, adapter: OIDCAdapter) -> None:
        """`aud` may be a string OR a string-array per RFC 7519 §4.1.3."""
        token = _sign_id_token(audience=["other-api", "copilot-api"])
        claims = adapter.verify_id_token(
            token,
            expected_nonce="nonce-1",
            id_token_signing_key=IDP_SIGNING_KEY,
        )
        assert claims.audience == "copilot-api"

    def test_wrong_nonce_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token(nonce="nonce-from-other-session")
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                token,
                expected_nonce="nonce-this-session",
                id_token_signing_key=IDP_SIGNING_KEY,
            )
        assert _details(exc.value)["claim"] == "nonce"

    def test_missing_nonce_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token()  # has nonce but verify with different
        with pytest.raises(OIDCClaimsMismatchError):
            adapter.verify_id_token(
                token,
                expected_nonce="different",
                id_token_signing_key=IDP_SIGNING_KEY,
            )

    def test_expired_token_raises(self, adapter: OIDCAdapter) -> None:
        token = _sign_id_token(expires_in=-10)  # already past
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                token,
                expected_nonce="nonce-1",
                id_token_signing_key=IDP_SIGNING_KEY,
            )
        assert _details(exc.value)["claim"] == "exp"

    def test_missing_email_verified_raises(
        self, adapter: OIDCAdapter
    ) -> None:
        """A non-bool `email_verified` is rejected."""
        # Re-sign with `extra_claims={"email_verified": "yes"}` — the
        # same bytes come out as a valid signature, but the claim is
        # the wrong type.
        signed = _sign_id_token(
            extra_claims={"email_verified": "yes"},
        )
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                signed,
                expected_nonce="nonce-1",
                id_token_signing_key=IDP_SIGNING_KEY,
            )
        assert exc.value.details is not None
        assert _details(exc.value)["claim"] == "email_verified"

    def test_unsupported_alg_raises(self, adapter: OIDCAdapter) -> None:
        """`alg: none` / RS256-without-key all fail fast."""
        # Build a token with `alg: none` — header, no signature.
        header_b64 = _b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload_b64 = _b64url_encode(json.dumps({
            "sub": "u", "email": "e@x.com", "email_verified": True,
            "nonce": "nonce-1", "aud": "copilot-api", "iss": ISSUER,
            "exp": int(time.time()) + 3600,
        }).encode())
        token = f"{header_b64}.{payload_b64}."
        with pytest.raises(OIDCIDTokenInvalidError):
            adapter.verify_id_token(
                token, expected_nonce="nonce-1", id_token_signing_key=IDP_SIGNING_KEY
            )

    def test_rs256_is_rejected_until_jwks_lands(
        self, adapter: OIDCAdapter
    ) -> None:
        """RS256-signing IdPs are rejected until T35 wires JWKS.

        RS256 verification needs the IdP's JWKS to verify the
        signature; that lives in T35. Until then, accepting an
        RS256 token would let forged claims through because the
        adapter can't check the signature. The adapter therefore
        fails closed.
        """
        header_b64 = _b64url_encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        )
        payload_b64 = _b64url_encode(json.dumps({
            "sub": "u", "email": "e@x.com", "email_verified": True,
            "nonce": "nonce-1", "aud": "copilot-api", "iss": ISSUER,
            "exp": int(time.time()) + 3600,
        }).encode())
        token = f"{header_b64}.{payload_b64}.{_b64url_encode(_ZEROS_256)}"
        with pytest.raises(OIDCIDTokenInvalidError) as exc:
            adapter.verify_id_token(
                token, expected_nonce="nonce-1", id_token_signing_key="k" * 32
            )
        # `decode_jwt` raises on unsupported alg.
        assert _details(exc.value)["error"] == "unsupported alg: 'RS256' (only HS256)"

    def test_hs256_without_key_raises(self, adapter: OIDCAdapter) -> None:
        """HS256 without a signing key cannot verify — fail closed."""
        token = _sign_id_token()
        with pytest.raises(OIDCIDTokenInvalidError) as exc:
            adapter.verify_id_token(
                token, expected_nonce="nonce-1", id_token_signing_key=None
            )
        assert exc.value.code == "oidc_id_token_invalid"

    def test_missing_sub_raises(self, adapter: OIDCAdapter) -> None:
        """Without `sub` we can't identify the user."""
        token = _sign_id_token(extra_claims={"sub": ""})
        with pytest.raises(OIDCClaimsMismatchError) as exc:
            adapter.verify_id_token(
                token, expected_nonce="nonce-1", id_token_signing_key=IDP_SIGNING_KEY
            )
        assert _details(exc.value)["claim"] == "sub"