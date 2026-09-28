"""End-to-end tests for `POST /api/v1/auth/refresh` (T08b / #49).

The refresh route is a thin shell over two collaborators —
`RefreshTokenService.rotate` (T07 / #8) and the access-JWT mint
helpers (T08 / #46). We exercise it through the FastAPI test client
so the wire shapes (request body, response envelope, error codes)
are pinned alongside the service-level behaviour.

To keep the test surface small we skip the IdP dance entirely: the
`OIDCAdapter` is stubbed (it's not on the refresh path at all) and
we issue a refresh token directly via the service after planting
a `users` row. That gives us full control over expiry / revocation /
deactivation without standing up the fake IdP infrastructure.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.auth.login import OIDCLoginService, OIDCStateStore, build_state_store
from app.auth.oidc import OIDCAdapter
from app.auth.tokens import RefreshTokenService, hash_token
from app.db.init_db import init_database
from app.db.schemas import UserCreate
from app.repositories.base import utcnow
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.security.jwt import decode_jwt
from app.settings import Settings

ISSUER = "https://idp.test"
TOKEN_ENDPOINT = f"{ISSUER}/token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url=ISSUER,
        oidc_id_token_signing_key="idp-test-signing-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
    )


@pytest.fixture
async def state_store(settings: Settings) -> OIDCStateStore:
    """Stub state store — refresh path doesn't touch it but the
    `OIDCLoginService` ctor wants one."""
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
    """A minimal stand-in for `MongoClient` that the lifespan can close.

    mongomock_motor's client has no `close()` coroutine, so the
    lifespan's `await mongo.close()` would `AttributeError`. Wrapping
    in a no-op closer keeps the same shape as `test_sso_callback.py`.
    """

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_refresh_route_test"]

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
async def login_service(
    settings: Settings,
    state_store: OIDCStateStore,
    user_repo: UserRepository,
    refresh_service: RefreshTokenService,
) -> OIDCLoginService:
    """A login service whose `OIDCAdapter` is a stub.

    The refresh path never invokes the adapter, so a no-op stub is
    enough to satisfy the constructor. Tests that exercise `start_login`
    or `complete_login` shouldn't reuse this fixture — they need the
    real `FakeIdP` flow from `test_sso_callback.py`.
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
    login_service: OIDCLoginService,
) -> Generator[None, None, None]:
    from app.db.dependencies import get_oidc_login_service

    app.dependency_overrides[get_oidc_login_service] = lambda: login_service
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(user_repo: UserRepository, *, is_active: bool = True) -> str:
    """Insert a single `users` row and return its id.

    `source="local"` keeps the row simple — no `sso_subject` lookup
    is in the refresh path.
    """
    created = await user_repo.create(
        UserCreate(
            email=f"u-{ObjectId()}@example.com",
            display_name="Refresh Tester",
            source="local",
            local_username=f"local-{ObjectId()}",
            password_hash="x" * 60,  # bcrypt-shaped; never verified here
        ),
    )
    if not is_active:
        # T04's `update` path is the simplest way to flip `is_active`
        # without bypassing the repository invariants.
        from app.db.schemas import UserUpdate

        updated = await user_repo.update(created.id, UserUpdate(is_active=False))
        return updated.id
    return created.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRefreshHappyPath:
    """Successful refresh rotates the token + mints a fresh access JWT."""

    async def test_returns_new_tokens_and_user(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
        settings: Settings,
    ) -> None:
        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Wire shape mirrors SSO callback so the front-end can share parsers.
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == settings.oidc_access_token_ttl_seconds
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["refresh_token"] != raw, "rotation must yield a fresh opaque token"

        # User is the same row, with the canonical (no-hash) shape.
        assert body["user"]["id"] == user_id
        assert body["user"]["source"] == "local"
        assert body["user"]["is_active"] is True
        assert "password_hash" not in body["user"]

    async def test_access_token_decodes_with_expected_claims(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
        settings: Settings,
    ) -> None:
        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 200, resp.text
        access = resp.json()["access_token"]

        decoded = decode_jwt(access, signing_key=settings.oidc_jwt_signing_key)
        assert decoded["sub"] == user_id
        assert decoded["source"] == "local"
        assert decoded["iss"] == settings.oidc_jwt_issuer
        assert decoded["aud"] == settings.oidc_jwt_audience
        assert decoded["exp"] == decoded["iat"] + settings.oidc_access_token_ttl_seconds

    async def test_old_refresh_token_is_revoked_after_rotation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        """Presenting the same refresh token a second time lands in the
        reuse-detection branch — the family is burned.

        This is the OAuth 2.0 Security BCP invariant: rotation must
        make the predecessor unusable. The 401 envelope is the same
        one the T07 service tests pin.
        """
        user_id = await _seed_user(user_repo)
        raw, row = await refresh_service.issue(user_id)

        first = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert second.status_code == 401
        body = second.json()
        assert body["code"] == "refresh_token_reuse_detected"
        assert body["details"]["family_id"] == row.family_id

        # The DB row is revoked; the family-wide burn has run.
        db = app.state.database
        old = await db["refresh_tokens"].find_one({"_id": ObjectId(row.id)})
        assert old is not None
        assert old["revoked_at"] is not None

        family_active = await db["refresh_tokens"].count_documents(
            {"family_id": row.family_id, "revoked_at": None},
        )
        assert family_active == 0

    async def test_new_refresh_token_round_trips(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        """The refresh response's `refresh_token` is itself usable."""
        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(user_id)

        first = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert first.status_code == 200
        next_raw = first.json()["refresh_token"]

        second = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": next_raw},
        )
        assert second.status_code == 200, second.text


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestRefreshErrorPaths:
    """Each compromise-class signal lands on the right envelope."""

    async def test_unknown_token_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "never-issued-token"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "refresh_token_not_found"

    async def test_expired_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A token whose `expires_at` is in the past is rejected.

        Mirrors the strategy in `test_refresh_token_rotation.py`: we
        issue normally and freeze the service clock far ahead so
        `expires_at` is now behind "now".
        """
        from app.auth import tokens as tokens_module

        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(
            user_id, ttl=timedelta(seconds=60),
        )
        far_future = utcnow() + timedelta(days=30)
        monkeypatch.setattr(tokens_module, "utcnow", lambda: far_future)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "refresh_token_expired"

    async def test_revoked_token_becomes_reuse_detected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        """An explicitly-revoked token surfaces as `refresh_token_reuse_detected`.

        Refresh is a *rotation* endpoint, not a verify endpoint. Any
        token whose `revoked_at` is set (whether by explicit logout,
        admin force-logout, or a previous successful rotation) routes
        through `RefreshTokenService.rotate`'s claim-failed branch and
        gets the reuse-detection treatment — burning the family is the
        OAuth 2.0 Security BCP response to "someone presented a token
        that should no longer be valid". `refresh_token_revoked` is
        reserved for the verify path (T09 #47 / T-logout endpoint).
        """
        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(user_id)
        await refresh_service.revoke(raw)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "refresh_token_reuse_detected"

    async def test_revoked_all_for_user_becomes_reuse_detected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        """Admin force-logout burns every active refresh token for the user.

        Same reuse-detection treatment as above — the family-wide
        revoke flips `revoked_at` on the row, and the next refresh
        attempt on any of those tokens routes through the claim-failed
        branch. The blast radius is intentional: an attacker who
        captured one of the user's refresh tokens before the
        force-logout must not get a clean session afterwards.
        """
        user_id = await _seed_user(user_repo)
        raw, _row = await refresh_service.issue(user_id)
        await refresh_service.revoke_all_for_user(user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "refresh_token_reuse_detected"

    async def test_inactive_user_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        """Deactivated users don't get a new session even with a valid refresh.

        Mirrors the SSO-callback behaviour: an admin who offboards a
        user mid-session must boot them on the next refresh, not the
        next 15-minute JWT expiry.
        """
        user_id = await _seed_user(user_repo, is_active=False)
        raw, _row = await refresh_service.issue(user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "user_inactive"

    async def test_missing_body_field_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        """Pydantic surfaces a missing `refresh_token` as 422."""
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={},
        )
        assert resp.status_code == 422

    async def test_empty_refresh_token_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        """`min_length=1` rejects empty strings at the schema layer."""
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Audit invariants
# ---------------------------------------------------------------------------


class TestRefreshAuditInvariants:
    """Cross-checks the DB state matches the wire response."""

    async def test_refresh_persists_new_token_in_same_family(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        refresh_service: RefreshTokenService,
    ) -> None:
        user_id = await _seed_user(user_repo)
        raw, row = await refresh_service.issue(user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": raw},
        )
        assert resp.status_code == 200
        new_raw = resp.json()["refresh_token"]

        db = app.state.database
        new_doc = await db["refresh_tokens"].find_one(
            {"token_hash": hash_token(new_raw)},
        )
        assert new_doc is not None
        assert new_doc["family_id"] == row.family_id
        assert new_doc["user_id"] == user_id
        assert new_doc["revoked_at"] is None

        # And `replaced_by` was stamped on the OLD row.
        old_doc = await db["refresh_tokens"].find_one(
            {"_id": ObjectId(row.id)},
        )
        assert old_doc is not None
        assert old_doc["replaced_by"] == str(new_doc["_id"])
