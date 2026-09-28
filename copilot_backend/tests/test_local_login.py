"""End-to-end tests for the admin local-login flow (T09 / #10).

T09 layers three new things on top of T07 / T08 / T08b:

* `app.auth.passwords` — bcrypt hash + verify helpers, used by the
  seed path and the login service.
* `app.auth.local.LocalLoginService` — `login(username, password)` →
  canonical login result. Composes `UserRepository.get_by_local_username`
  with `RefreshTokenService.issue` + the JWT mint that T08 already
  uses for SSO. The wire shape mirrors SSO so the front-end can use
  one response parser for both paths (ADR-0009 / ADR-0032).
* Three new HTTP routes: `POST /auth/login`, `POST /auth/logout`,
  `GET /admin/me`. The router is intentionally thin — every byte of
  business logic lives in `LocalLoginService` and
  `RefreshTokenService.revoke` / `revoke_all_for_user`.

Acceptance criteria pinned here:

* correct password → tokens + canonical user shape
* wrong password → 401
* unknown user → 401 (the same envelope — never disclose existence)
* inactive user → 403
* `/admin/me` returns the authenticated admin's username
* `/admin/me` rejects unauthenticated requests
* `/auth/logout` revokes the refresh token
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.auth.errors import InvalidLocalCredentialsError
from app.auth.local import LocalLoginService
from app.auth.login import (
    OIDCLoginService,
    OIDCStateStore,
    build_state_store,
)
from app.auth.oidc import OIDCAdapter
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import RefreshTokenService
from app.db.init_db import init_database
from app.db.schemas import UserCreate
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.security.jwt import decode_jwt
from app.settings import Settings

# ---------------------------------------------------------------------------
# Settings + fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url="https://test.example.com",
        oidc_id_token_signing_key="test-idp-hs256-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
    )


@pytest.fixture
async def state_store(settings: Settings) -> OIDCStateStore:
    return build_state_store(settings)


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    """Fresh app per test with a hermetic in-memory Mongo."""
    from app.main import create_app

    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None  # bypass the lifespan adapter fetch
    await init_database(app.state.database)
    return app


class _AsyncMongoMockForLifespan:
    """A minimal stand-in for `MongoClient` that the lifespan can close."""

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_local_login_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def user_repo(app: FastAPI) -> UserRepository:
    db = app.state.database
    await db["users"].delete_many({})
    await db["refresh_tokens"].delete_many({})
    return UserRepository(db)


@pytest.fixture
async def refresh_service(app: FastAPI) -> RefreshTokenService:
    return RefreshTokenService(RefreshTokenRepository(app.state.database))


@pytest.fixture
async def local_service(
    settings: Settings,
    user_repo: UserRepository,
    refresh_service: RefreshTokenService,
) -> LocalLoginService:
    return LocalLoginService(
        settings=settings,
        user_repository=user_repo,
        refresh_service=refresh_service,
    )


@pytest.fixture
async def stub_login_service(
    settings: Settings,
    state_store: OIDCStateStore,
    user_repo: UserRepository,
    refresh_service: RefreshTokenService,
) -> OIDCLoginService:
    """A login service whose `OIDCAdapter` is a stub — keeps the OIDC
    route inert without standing up a fake IdP.
    """
    adapter = OIDCAdapter(settings, http_client=httpx.AsyncClient(timeout=1.0))
    return OIDCLoginService(
        settings=settings,
        oidc_adapter=adapter,
        state_store=state_store,
        user_repository=user_repo,
        refresh_service=refresh_service,
    )


@pytest.fixture(autouse=True)
def _override_dependencies(
    app: FastAPI,
    settings: Settings,
    local_service: LocalLoginService,
    stub_login_service: OIDCLoginService,
) -> Generator[None, None, None]:
    from app.db.dependencies import (
        get_local_login_service,
        get_oidc_login_service,
    )
    from app.settings import get_settings

    # `get_settings` is `lru_cache`'d — without this override, the
    # `get_current_user` dependency would decode the access JWT with
    # the production-default signing key rather than the test's,
    # causing every authenticated route to 401.
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oidc_login_service] = lambda: stub_login_service
    app.dependency_overrides[get_local_login_service] = lambda: local_service
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_admin(
    user_repo: UserRepository,
    *,
    username: str = "admin",
    password: str = "correct horse battery staple",
    is_active: bool = True,
    display_name: str = "Admin User",
) -> str:
    """Plant a single `source=local` user and return its id."""
    created = await user_repo.create(
        UserCreate(
            email=f"{username}@example.com",
            display_name=display_name,
            source="local",
            local_username=username,
            password_hash=hash_password(password),
            is_active=is_active,
        ),
    )
    return created.id


def _bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


class TestPasswordHelpers:
    """`hash_password` / `verify_password` are the seam the seed and
    the login service share. They MUST be stable across processes and
    bcrypt-shaped (so a future migration can decode hashes from prod).
    """

    def test_verify_accepts_correct_password(self) -> None:
        h = hash_password("hello world")
        assert verify_password("hello world", h) is True

    def test_verify_rejects_wrong_password(self) -> None:
        h = hash_password("hello world")
        assert verify_password("goodbye world", h) is False

    def test_hash_starts_with_bcrypt_marker(self) -> None:
        h = hash_password("hello world")
        # bcrypt's `$2b$` / `$2a$` / `$2y$` prefixes are all acceptable;
        # we pin the family so a future migration is one decision.
        assert h.startswith(("$2b$", "$2a$", "$2y$")), h

    def test_hash_is_unique_per_call(self) -> None:
        """Salt is random — two hashes of the same password differ."""
        a = hash_password("hello world")
        b = hash_password("hello world")
        assert a != b


# ---------------------------------------------------------------------------
# LocalLoginService
# ---------------------------------------------------------------------------


class TestLocalLoginService:
    """Service-layer coverage of the username + password path."""

    async def test_login_returns_tokens_and_user(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_admin(user_repo, username="alice", password="hunter2")

        result = await local_service.login(username="alice", password="hunter2")

        assert result.user.id == user_id
        assert result.user.local_username == "alice"
        assert result.user.source == "local"
        assert result.user.is_active is True
        assert "password_hash" not in result.user.model_dump()
        assert result.token_type == "Bearer"
        assert result.expires_in == settings.oidc_access_token_ttl_seconds
        assert result.access_token
        assert result.refresh_token

    async def test_login_access_token_decodes_with_expected_claims(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_admin(user_repo, username="bob", password="sekrit!")
        result = await local_service.login(username="bob", password="sekrit!")
        decoded = decode_jwt(result.access_token, signing_key=settings.oidc_jwt_signing_key)
        assert decoded["sub"] == user_id
        assert decoded["source"] == "local"
        assert decoded["iss"] == settings.oidc_jwt_issuer
        assert decoded["aud"] == settings.oidc_jwt_audience
        assert decoded["role_ids"] == []

    async def test_login_wrong_password_raises(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(user_repo, username="alice", password="hunter2")

        with pytest.raises(InvalidLocalCredentialsError):
            await local_service.login(username="alice", password="WRONG")

    async def test_login_unknown_user_raises(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
    ) -> None:
        """Username enumeration is closed — the unknown case reuses the
        same `InvalidLocalCredentialsError` envelope as wrong-password."""
        with pytest.raises(InvalidLocalCredentialsError):
            await local_service.login(username="ghost", password="anything")

    async def test_login_inactive_user_raises_user_inactive(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
    ) -> None:
        """Inactive admins must not get a session — distinct from the
        credentials envelope so the front-end can show "account disabled".
        """
        from app.auth.errors import UserInactiveError

        await _seed_admin(user_repo, username="alice", password="hunter2", is_active=False)

        with pytest.raises(UserInactiveError):
            await local_service.login(username="alice", password="hunter2")

    async def test_login_persists_refresh_token(
        self,
        local_service: LocalLoginService,
        user_repo: UserRepository,
        app: FastAPI,
    ) -> None:
        user_id = await _seed_admin(user_repo, username="alice", password="hunter2")
        await local_service.login(username="alice", password="hunter2")

        db = app.state.database
        doc = await db["refresh_tokens"].find_one({"user_id": user_id})
        assert doc is not None
        assert doc["revoked_at"] is None
        assert doc["expires_at"] is not None


# ---------------------------------------------------------------------------
# /auth/login
# ---------------------------------------------------------------------------


class TestLocalLoginRoute:
    """`POST /api/v1/auth/login` — the wire shape mirror of SSO callback."""

    async def test_login_happy_path_returns_tokens(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(user_repo, username="alice", password="hunter2")

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "hunter2"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["user"]["local_username"] == "alice"
        assert body["user"]["source"] == "local"

    async def test_login_wrong_password_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(user_repo, username="alice", password="hunter2")

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "WRONG"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_local_credentials"

    async def test_login_unknown_user_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "ghost", "password": "anything"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_local_credentials"

    async def test_login_inactive_user_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(user_repo, username="alice", password="hunter2", is_active=False)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "hunter2"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "user_inactive"

    async def test_login_missing_password_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post("/api/v1/auth/login", json={"username": "alice"})
        assert resp.status_code == 422

    async def test_login_empty_password_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /admin/me
# ---------------------------------------------------------------------------


class TestAdminMe:
    """`GET /api/v1/admin/me` — returns the authenticated admin's username."""

    async def test_returns_username_and_display_name(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(
            user_repo,
            username="alice",
            password="hunter2",
            display_name="Alice the Admin",
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "hunter2"},
        )
        access = login.json()["access_token"]

        resp = await client.get("/api/v1/admin/me", headers=_bearer(access))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["username"] == "alice"
        assert body["display_name"] == "Alice the Admin"
        assert body["id"]

    async def test_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/admin/me")
        assert resp.status_code == 401

    async def test_bad_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get(
            "/api/v1/admin/me",
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert resp.status_code == 401

    async def test_wrong_signing_key_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        """A JWT signed with a key other than `oidc_jwt_signing_key` is rejected."""
        from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix

        await _seed_admin(user_repo, username="alice", password="hunter2")
        forged = AccessTokenClaims(
            sub="any",
            source="local",
            role_ids=[],
            issuer="copilot-backend",
            audience="copilot-api",
            issued_at=now_unix(),
            expires_at=now_unix() + 900,
            jti="forged",
        )
        token, _ = mint_access_token(forged, signing_key="the-wrong-key")

        resp = await client.get("/api/v1/admin/me", headers=_bearer(token))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /auth/logout
# ---------------------------------------------------------------------------


class TestLogout:
    """`POST /api/v1/auth/logout` — revoke the refresh token (T09 acceptance criterion)."""

    async def test_logout_revokes_refresh_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
    ) -> None:
        await _seed_admin(user_repo, username="alice", password="hunter2")
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "hunter2"},
        )
        refresh = login.json()["refresh_token"]

        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh},
        )
        assert resp.status_code == 200, resp.text

        # Subsequent refresh with the same token must NOT yield a session.
        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        # The token is revoked → reuse-detection burns the family.
        assert refresh_resp.status_code == 401
        assert refresh_resp.json()["code"] == "refresh_token_reuse_detected"

    async def test_logout_unknown_token_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "never-issued-token"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "refresh_token_not_found"

    async def test_logout_empty_token_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": ""},
        )
        assert resp.status_code == 422


__all__: list[Any] = []
