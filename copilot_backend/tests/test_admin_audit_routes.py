"""End-to-end tests for the admin Audit Log API (T42 / #37 + T43 / #38).

T42 layers two endpoints on top of the audit retention surface
(T06's repository + T42's `AuditRetentionService`):

* `GET  /api/v1/admin/audit-logs`               — list with filters
                                                    and cursor
                                                    pagination.
* `POST /api/v1/admin/audit-logs/{id}/recall`   — trigger cold-
                                                    storage hydration
                                                    (ADR-0028's 5-min
                                                    SLO).

Auth is `require_admin_user` (T12); non-admin callers hit 403.

Acceptance criteria pinned here:

* Filters compose (`tool_name`, `actor_id`, `lifecycle_status`,
  `time_from`, `time_to`).
* Cursor pagination is stable — chaining pages after the last
  row returns `next_cursor=null` / `has_more=false`.
* Recall happy path: archive + recall restores full payload,
  returns 200 with the recalled row.
* Recall rejects `active` rows with the documented 409 envelope.
* Recall rejects malformed ids with the documented 404 envelope.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import datetime
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.audit.cold_storage import FileAuditColdStorage
from app.audit.retention import AuditRetentionService
from app.db.init_db import init_database
from app.db.schemas import AuditLogCreate, RoleCreate, ToolSnapshot, UserCreate
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.roles import RoleRepository
from app.repositories.users import UserRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings


# ---------------------------------------------------------------------------
# Settings + lifespan stand-ins
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
        audit_cold_storage_dir="./data/audit_cold_routes_test",
        audit_hot_retention_seconds=86_400,
    )


class _AsyncMongoMockForLifespan:
    """Stand-in `MongoClient` the lifespan can `close()` cleanly."""

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_admin_audit_routes_test"]

    async def close(self) -> None:
        pass


@pytest.fixture
async def app(
    settings: Settings,
    tmp_path_factory: pytest.TempPathFactory,
) -> FastAPI:
    """Fresh app per test with hermetic Mongo + cold-storage path."""
    from app.main import create_app

    tmp = tmp_path_factory.mktemp("audit-cold-routes")
    settings = settings.model_copy(
        update={"audit_cold_storage_dir": str(tmp / "cold")},
    )

    app = create_app(settings=settings)
    mongo = _AsyncMongoMockForLifespan()
    app.state.mongo = mongo
    app.state.database = mongo.database
    app.state.oidc_adapter = None
    encryptor = AesGcmEncryptor(
        MasterKey(key_bytes=bytes(range(32)), key_id="test-primary"),
    )
    cold_storage = FileAuditColdStorage(settings.audit_cold_storage_dir)
    repo = AuditLogRepository(mongo.database)
    service = AuditRetentionService(
        audit_repository=repo,
        cold_storage=cold_storage,
        encryptor=encryptor,
        hot_retention_seconds=settings.audit_hot_retention_seconds,
    )
    app.state.credential_encryptor = encryptor
    app.state.audit_cold_storage = cold_storage
    app.state.audit_retention_service = service
    await init_database(mongo.database)
    return app


@pytest.fixture(autouse=True)
def _override_settings(
    app: FastAPI, settings: Settings
) -> Generator[None, None, None]:
    from app.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Repository / HTTP fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_repo(app: FastAPI) -> UserRepository:
    return UserRepository(app.state.database)


@pytest.fixture
def role_repo(app: FastAPI) -> RoleRepository:
    return RoleRepository(app.state.database)


@pytest.fixture
def audit_repo(app: FastAPI) -> AuditLogRepository:
    return AuditLogRepository(app.state.database)


@pytest.fixture
async def client(app: FastAPI) -> Any:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Auth helpers (mirrors test_admin_tools_routes.py)
# ---------------------------------------------------------------------------


async def _seed_role(role_repo: RoleRepository, *, name: str = "admin") -> str:
    created = await role_repo.create(
        RoleCreate(name=name, description=f"{name} role"),
    )
    return created.id


async def _seed_admin(
    user_repo: UserRepository,
    *,
    role_ids: list[str] | None = None,
) -> str:
    from app.auth.passwords import hash_password

    created = await user_repo.create(
        UserCreate(
            email="admin@example.com",
            display_name="Admin User",
            source="local",
            local_username="admin",
            password_hash=hash_password("correct horse battery staple"),
            role_ids=list(role_ids or []),
        ),
    )
    return created.id


async def _seed_non_admin(user_repo: UserRepository) -> str:
    created = await user_repo.create(
        UserCreate(
            email=f"user-{ObjectId()}@example.com",
            display_name="Regular User",
            source="sso",
            sso_subject=f"sub-{ObjectId()}",
            role_ids=[],
        ),
    )
    return created.id


def _bearer(user_id: str, *, settings: Settings, role_ids: list[str]) -> str:
    claims = AccessTokenClaims(
        sub=user_id,
        source="local",
        role_ids=list(role_ids),
        issuer=settings.oidc_jwt_issuer,
        audience=settings.oidc_jwt_audience,
        issued_at=now_unix(),
        expires_at=now_unix() + settings.oidc_access_token_ttl_seconds,
        jti="test-jti",
    )
    token, _ = mint_access_token(claims, signing_key=settings.oidc_jwt_signing_key)
    return token


async def _admin_bearer(
    *,
    user_repo: UserRepository,
    role_repo: RoleRepository,
    settings: Settings,
) -> dict[str, str]:
    role_id = await _seed_role(role_repo)
    user_id = await _seed_admin(user_repo, role_ids=[role_id])
    token = _bearer(user_id, settings=settings, role_ids=[role_id])
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Audit-row factory
# ---------------------------------------------------------------------------


def _snapshot() -> ToolSnapshot:
    return ToolSnapshot(
        name="list_customers",
        description="List customers by region.",
        risk_level="read",
        parameters_schema={
            "type": "object",
            "properties": {"region": {"type": "string"}},
        },
        http_method="GET",
        http_url_template="https://api.example.com/customers?region={region}",
        http_headers={},
        http_body_template=None,
    )


async def _seed_audit(
    audit_repo: AuditLogRepository,
    **overrides: object,
) -> str:
    base: dict[str, object] = {
        "actor_id": str(ObjectId()),
        "conversation_id": str(ObjectId()),
        "turn_id": str(ObjectId()),
        "plan_id": str(ObjectId()),
        "plan_execution_id": str(ObjectId()),
        "tool_name": "list_customers",
        "tool_snapshot": _snapshot(),
        "parameters": {"region": "emea"},
        "response": {"data": [{"id": "c1"}]},
        "status": "succeeded",
        "error": None,
        "risk_level": "read",
        "retry_count": 0,
    }
    base.update(overrides)
    created = await audit_repo.create(AuditLogCreate(**base))  # type: ignore[arg-type]
    return created.id


def _decode_cursor(cursor: str) -> tuple[str, str]:
    padding = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(cursor + padding)
    data = json.loads(raw.decode("utf-8"))
    return data["o"], data["i"]


# ---------------------------------------------------------------------------
# GET /api/v1/admin/audit-logs
# ---------------------------------------------------------------------------


class TestListAuditLogs:
    """`GET /api/v1/admin/audit-logs` — filterable list."""

    async def test_list_returns_empty_when_no_rows(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        resp = await client.get("/api/v1/admin/audit-logs", headers=bearer)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"logs": [], "next_cursor": None, "has_more": False}

    async def test_list_returns_newest_first(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        ids = [
            await _seed_audit(audit_repo, tool_name="list_customers"),
            await _seed_audit(audit_repo, tool_name="send_email"),
        ]
        resp = await client.get("/api/v1/admin/audit-logs", headers=bearer)
        assert resp.status_code == 200
        body = resp.json()
        assert [r["id"] for r in body["logs"]] == [ids[1], ids[0]]
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    async def test_filter_by_tool_name(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        ours = await _seed_audit(audit_repo, tool_name="list_customers")
        await _seed_audit(audit_repo, tool_name="send_email")
        resp = await client.get(
            "/api/v1/admin/audit-logs?tool_name=list_customers",
            headers=bearer,
        )
        body = resp.json()
        assert [r["id"] for r in body["logs"]] == [ours]
        assert all(r["tool_name"] == "list_customers" for r in body["logs"])

    async def test_filter_by_lifecycle_status(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        active = await _seed_audit(audit_repo)
        archived = await _seed_audit(audit_repo)
        await audit_repo.mark_archived(archived, "cold/path")
        resp = await client.get(
            "/api/v1/admin/audit-logs?lifecycle_status=archived",
            headers=bearer,
        )
        body = resp.json()
        assert [r["id"] for r in body["logs"]] == [archived]
        # The active row is excluded.
        assert active not in [r["id"] for r in body["logs"]]

    async def test_cursor_pagination_chains_through_full_set(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        ids = [
            await _seed_audit(audit_repo, tool_name="t") for _ in range(3)
        ]
        first = await client.get(
            "/api/v1/admin/audit-logs?limit=2", headers=bearer,
        )
        body = first.json()
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert [r["id"] for r in body["logs"]] == [ids[2], ids[1]]

        second = await client.get(
            f"/api/v1/admin/audit-logs?limit=2&cursor={body['next_cursor']}",
            headers=bearer,
        )
        body2 = second.json()
        assert body2["has_more"] is False
        assert body2["next_cursor"] is None
        assert [r["id"] for r in body2["logs"]] == [ids[0]]

    async def test_invalid_cursor_returns_400(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        resp = await client.get(
            "/api/v1/admin/audit-logs?cursor=not-base64",
            headers=bearer,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    async def test_invalid_time_from_returns_400(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        resp = await client.get(
            "/api/v1/admin/audit-logs?time_from=not-a-date",
            headers=bearer,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    async def test_non_admin_caller_gets_403(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_non_admin(user_repo)
        token = _bearer(user_id, settings=settings, role_ids=[])
        resp = await client.get(
            "/api/v1/admin/audit-logs",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_missing_token_returns_401(
        self, client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/admin/audit-logs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/admin/audit-logs/{id}/recall
# ---------------------------------------------------------------------------


class TestRecallAuditLog:
    """`POST /api/v1/admin/audit-logs/{id}/recall` — cold-storage hydration."""

    async def test_recall_happy_path_restores_payload(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        app: FastAPI,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        row_id = await _seed_audit(audit_repo)
        # Backdate so the 1-day hot window sweeps it on the next tick.
        await audit_repo._collection.update_one(
            {"_id": ObjectId(row_id)},
            {"$set": {"occurred_at": datetime(2024, 1, 1)}},
        )
        service: AuditRetentionService = app.state.audit_retention_service
        result = await service.run_once()  # archive
        assert result.flipped == 1, "setup failed — row was not archived"

        resp = await client.post(
            f"/api/v1/admin/audit-logs/{row_id}/recall",
            headers=bearer,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == row_id
        assert body["lifecycle_status"] == "recalled"
        assert body["parameters"] == {"region": "emea"}
        assert body["response"] == {"data": [{"id": "c1"}]}

    async def test_recall_active_row_returns_409(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        row_id = await _seed_audit(audit_repo)
        resp = await client.post(
            f"/api/v1/admin/audit-logs/{row_id}/recall",
            headers=bearer,
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "audit_log_not_archived"

    async def test_recall_unknown_id_returns_404(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        resp = await client.post(
            f"/api/v1/admin/audit-logs/{ObjectId()}/recall",
            headers=bearer,
        )
        assert resp.status_code == 404

    async def test_recall_malformed_id_returns_404(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        bearer = await _admin_bearer(
            user_repo=user_repo, role_repo=role_repo, settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/audit-logs/not-an-oid/recall",
            headers=bearer,
        )
        assert resp.status_code == 404

    async def test_recall_non_admin_caller_gets_403(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_non_admin(user_repo)
        token = _bearer(user_id, settings=settings, role_ids=[])
        row_id = await _seed_audit(audit_repo)
        resp = await client.post(
            f"/api/v1/admin/audit-logs/{row_id}/recall",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403