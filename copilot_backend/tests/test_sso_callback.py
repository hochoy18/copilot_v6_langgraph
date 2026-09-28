"""End-to-end tests for the SSO callback (T08 / #46).

These walk the full `OIDCLoginService` stack (state store → OIDC
adapter → user upsert → refresh issue → JWT mint) with a fake IdP
so the orchestration is verified as one piece. The HTTP router
(`/api/v1/auth/sso/callback`) is exercised through the FastAPI test
client so the wire shapes are pinned too.

We isolate the state store and OIDC adapter per test using
`dependency_overrides` so the global app stays clean.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.auth.login import (
    OIDCLoginEntry,
    OIDCLoginService,
    OIDCStateStore,
    build_state_store,
)
from app.auth.oidc import OIDCAdapter, derive_code_challenge, generate_code_verifier
from app.auth.tokens import RefreshTokenService, hash_token
from app.db.init_db import init_database
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.security.jwt import decode_jwt
from app.settings import Settings

ISSUER = "https://idp.test"
TOKEN_ENDPOINT = f"{ISSUER}/token"
AUTHZ_ENDPOINT = f"{ISSUER}/authorize"
JWKS_URI = f"{ISSUER}/jwks"
IDP_KEY = "idp-e2e-signing-key"


# ---------------------------------------------------------------------------
# Fake IdP
# ---------------------------------------------------------------------------


class FakeIdP:
    """In-memory IdP — issues `id_token`s with the configured claims."""

    def __init__(self, signing_key: str = IDP_KEY) -> None:
        self.signing_key = signing_key
        self.issued_codes: dict[str, dict[str, Any]] = {}

    def discovery_doc(self) -> dict[str, Any]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHZ_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
        }

    def token_response(self, *, code: str) -> dict[str, Any]:
        record = self.issued_codes.get(code)
        if record is None:
            return {"error": "invalid_grant"}
        if record.get("reject"):
            return {"error": "invalid_grant", "error_description": "verifier mismatch"}
        return {
            "access_token": "idp-access-" + code,
            "id_token": record["id_token"],
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery_doc())
        if path.endswith("/token"):
            form: dict[str, str] = {}
            for chunk in request.content.decode("utf-8").split("&"):
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    import urllib.parse
                    form[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
            return httpx.Response(200, json=self.token_response(code=form.get("code", "")))
        return httpx.Response(404, json={"error": "not_found"})

    def issue_id_token(
        self,
        *,
        code: str,
        sub: str = "user-1",
        email: str = "alice@example.com",
        email_verified: bool = True,
        name: str = "Alice",
        nonce: str = "nonce-1",
        audience: str = "copilot-api",
        issuer: str = ISSUER,
        expires_in: int = 3600,
        extra_claims: dict[str, Any] | None = None,
    ) -> None:
        """Pre-register the IdP response for `code`."""
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

        header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload_b64 = _b64url(json.dumps(payload).encode())
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        sig = hmac.new(
            self.signing_key.encode(), signing_input, hashlib.sha256
        ).digest()
        self.issued_codes[code] = {
            "id_token": f"{header_b64}.{payload_b64}.{_b64url(sig)}",
        }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url=ISSUER,
        oidc_id_token_signing_key=IDP_KEY,
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
    )


@pytest.fixture
async def state_store(settings: Settings) -> OIDCStateStore:
    return build_state_store(settings)


@pytest.fixture
async def user_repo(app: FastAPI) -> UserRepository:
    db = app.state.database
    # Wipe users/refresh tokens so test ordering doesn't matter.
    await db["users"].delete_many({})
    await db["refresh_tokens"].delete_many({})
    return UserRepository(db)


@pytest.fixture
async def refresh_service(app: FastAPI) -> RefreshTokenService:
    db = app.state.database
    return RefreshTokenService(RefreshTokenRepository(db))


@pytest.fixture
async def login_service(
    settings: Settings,
    idp: FakeIdP,
    state_store: OIDCStateStore,
    user_repo: UserRepository,
    refresh_service: RefreshTokenService,
) -> OIDCLoginService:
    """A login service with an in-process adapter wired against the fake IdP."""
    transport = httpx.MockTransport(idp.handler)
    http_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    adapter = OIDCAdapter(settings, http_client=http_client)
    return OIDCLoginService(
        settings=settings,
        oidc_adapter=adapter,
        state_store=state_store,
        user_repository=user_repo,
        refresh_service=refresh_service,
    )


# Override the FastAPI dependencies so requests hit our test instances.
@pytest.fixture(autouse=True)
def _override_dependencies(
    app: FastAPI,
    settings: Settings,
    state_store: OIDCStateStore,
    login_service: OIDCLoginService,
) -> Generator[None, None, None]:
    from app.db.dependencies import (
        get_oidc_login_service,
        get_oidc_state_store,
    )

    app.dependency_overrides[get_oidc_state_store] = lambda: state_store
    app.dependency_overrides[get_oidc_login_service] = lambda: login_service
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    """Fresh app per test with a hermetic in-memory Mongo."""
    from app.main import create_app

    app = create_app(settings=settings)
    # Replace the real MongoClient with an in-memory one so init_db
    # and the repository can run. mongomock-motor + the lifespan's
    # `_probe_dependencies` don't play nicely together; skip the
    # probe by stashing our own DB on app.state.
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None  # bypass the lifespan adapter fetch
    await init_database(app.state.database)
    return app


class _AsyncMongoMockForLifespan:
    """A minimal stand-in for `MongoClient` that the lifespan can close.

    The lifespan calls `await mongo.close()`. `mongomock_motor`'s
    client has no such method, so we wrap it in a no-op closer.
    """

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_e2e_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


# ---------------------------------------------------------------------------
# End-to-end — happy path
# ---------------------------------------------------------------------------


class TestSSOCallbackHappyPath:
    """`complete_login` round-trips state → tokens → DB rows."""

    async def test_callback_returns_access_and_refresh_tokens(
        self,
        app: FastAPI,
        settings: Settings,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        # Pre-register the code + id_token at the IdP.
        idp.issue_id_token(code="auth-code-1", sub="u-1", email="alice@x.com", nonce="nonce-1")

        # Plant a state entry — simulating what `start_login` would
        # have written.
        verifier = generate_code_verifier()
        entry = OIDCLoginEntry(
            state="state-1",
            nonce="nonce-1",
            code_verifier=verifier,
            created_at=time.monotonic(),
        )
        await state_store.put(entry)

        # Now POST the callback.
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "auth-code-1", "state": "state-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == settings.oidc_access_token_ttl_seconds
        assert body["refresh_token"]
        assert body["access_token"]
        # User is mirrored.
        assert body["user"]["email"] == "alice@x.com"
        assert body["user"]["source"] == "sso"
        assert body["user"]["sso_subject"] == "u-1"
        assert body["user"]["is_active"] is True

    async def test_callback_access_token_decodes_with_expected_claims(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings: Settings,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """The minted access JWT carries the user id, source, roles, exp."""
        idp.issue_id_token(code="code-2", sub="u-2", email="bob@x.com", nonce="nonce-2")

        entry = OIDCLoginEntry(
            state="state-2",
            nonce="nonce-2",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)

        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-2", "state": "state-2"},
        )
        assert resp.status_code == 200, resp.text
        access = resp.json()["access_token"]

        decoded = decode_jwt(access, signing_key=settings.oidc_jwt_signing_key)
        assert decoded["source"] == "sso"
        assert decoded["iss"] == settings.oidc_jwt_issuer
        assert decoded["aud"] == settings.oidc_jwt_audience
        assert decoded["role_ids"] == []
        assert decoded["sub"] == resp.json()["user"]["id"]
        assert decoded["exp"] == decoded["iat"] + settings.oidc_access_token_ttl_seconds

    async def test_callback_persists_user_and_refresh_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """The DB rows land: one `users` row + one active `refresh_tokens` row."""
        idp.issue_id_token(code="code-3", sub="u-3", email="c@x.com", nonce="nonce-3")
        entry = OIDCLoginEntry(
            state="state-3",
            nonce="nonce-3",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)

        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-3", "state": "state-3"},
        )
        assert resp.status_code == 200, resp.text
        user_id = resp.json()["user"]["id"]

        db = app.state.database
        user = await db["users"].find_one({"sso_subject": "u-3"})
        assert user is not None
        assert user["source"] == "sso"
        assert user["is_active"] is True

        refresh = await db["refresh_tokens"].find_one({"user_id": user_id})
        assert refresh is not None
        assert refresh["revoked_at"] is None
        # The refresh token's hash is SHA-256 of the raw token we got.
        assert refresh["token_hash"] == hash_token(resp.json()["refresh_token"])

    async def test_repeat_login_returns_same_user_id(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """Two callbacks with the same `sub` produce one user."""
        idp.issue_id_token(code="code-a", sub="u-shared", email="d@x.com", nonce="nonce-a")
        entry_a = OIDCLoginEntry(
            state="state-a",
            nonce="nonce-a",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry_a)
        resp_a = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-a", "state": "state-a"},
        )
        assert resp_a.status_code == 200, resp_a.text

        # Second login: same sub, same email.
        idp.issue_id_token(code="code-b", sub="u-shared", email="d@x.com", nonce="nonce-b")
        entry_b = OIDCLoginEntry(
            state="state-b",
            nonce="nonce-b",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry_b)
        resp_b = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-b", "state": "state-b"},
        )
        assert resp_b.status_code == 200, resp_b.text
        assert resp_a.json()["user"]["id"] == resp_b.json()["user"]["id"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestSSOCallbackErrorPaths:
    """Each compromise-class signal lands on the right envelope."""

    async def test_unknown_state_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code", "state": "never-issued"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "oidc_state_mismatch"

    async def test_unknown_code_returns_502(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """IdP rejecting the code surfaces as 502 (the IdP is at fault)."""
        entry = OIDCLoginEntry(
            state="s-1",
            nonce="n-1",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "never-issued", "state": "s-1"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "oidc_token_exchange_failed"

    async def test_wrong_nonce_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """A nonce mismatch (the id_token was minted for another session)
        is a compromise signal — 401.
        """
        # We planted nonce="expected", but the IdP signed "different".
        idp.issue_id_token(code="code-n1", nonce="different")
        entry = OIDCLoginEntry(
            state="s-n1",
            nonce="expected",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-n1", "state": "s-n1"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "oidc_claims_mismatch"

    async def test_wrong_issuer_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        idp.issue_id_token(code="code-i1", issuer="https://attacker.example.com")
        entry = OIDCLoginEntry(
            state="s-i1",
            nonce="n-1",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-i1", "state": "s-i1"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "oidc_claims_mismatch"
        assert body["details"]["claim"] == "iss"

    async def test_idp_signature_mismatch_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """The IdP signs with a different key than our config → 401."""
        # Issue a token signed with a key we don't trust.
        idp.signing_key = "wrong-key"
        idp.issue_id_token(code="code-s1")
        entry = OIDCLoginEntry(
            state="s-s1",
            nonce="nonce-1",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-s1", "state": "s-s1"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "oidc_id_token_invalid"

    async def test_state_is_single_use(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """Re-presenting the same `state` returns 401 — no replay."""
        idp.issue_id_token(code="code-r1", nonce="nonce-1")
        entry = OIDCLoginEntry(
            state="s-r1",
            nonce="nonce-1",
            code_verifier=generate_code_verifier(),
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        # First call succeeds.
        first = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-r1", "state": "s-r1"},
        )
        assert first.status_code == 200, first.text
        # Second call with the same state fails — entry was consumed.
        second = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-r1", "state": "s-r1"},
        )
        assert second.status_code == 401
        assert second.json()["code"] == "oidc_state_mismatch"

    async def test_pkce_verifier_mismatch_returns_502(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        state_store: OIDCStateStore,
        idp: FakeIdP,
    ) -> None:
        """If the verifier we send doesn't match the IdP-registered one,
        the IdP rejects the exchange.
        """
        idp.issued_codes["code-p1"] = {
            "id_token": "h.p.s",  # not decoded — IdP rejects pre-claim
            "reject": True,
        }
        entry = OIDCLoginEntry(
            state="s-p1",
            nonce="n-1",
            code_verifier="this-doesnt-match-what-the-idp-knows",
            created_at=time.monotonic(),
        )
        await state_store.put(entry)
        resp = await client.post(
            "/api/v1/auth/sso/callback",
            json={"code": "code-p1", "state": "s-p1"},
        )
        # The IdP's contract here is "invalid_grant" — surfaced as 502.
        assert resp.status_code == 502
        assert resp.json()["code"] == "oidc_token_exchange_failed"


# ---------------------------------------------------------------------------
# /auth/sso/login
# ---------------------------------------------------------------------------


class TestSSOLoginRoute:
    """`GET /auth/sso/login` mints state + PKCE + nonce."""

    async def test_login_returns_auth_url_and_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/auth/sso/login")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authorization_url"].startswith(AUTHZ_ENDPOINT + "?")
        assert body["state"]

        # PKCE params are present in the URL.
        for needle in (
            "code_challenge=",
            "code_challenge_method=S256",
            "response_type=code",
            "state=" + body["state"],
        ):
            assert needle in body["authorization_url"]


# ---------------------------------------------------------------------------
# Auth-URL PKCE consistency
# ---------------------------------------------------------------------------


class TestPKCERoundTrip:
    """PKCE challenge is `b64url(SHA256(verifier))` — both layers do the same."""

    def test_derive_code_challenge_matches_rfc(self) -> None:
        """The challenge derivation matches the RFC 7636 example value."""
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected = (
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )
        assert derive_code_challenge(verifier) == expected

    def test_random_verifier_challenge_is_consistent(self) -> None:
        """Two calls with the same verifier return the same challenge."""
        verifier = generate_code_verifier()
        a = derive_code_challenge(verifier)
        b = derive_code_challenge(verifier)
        assert a == b
        assert len(a) >= 43