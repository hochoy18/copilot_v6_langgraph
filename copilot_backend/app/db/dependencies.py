"""FastAPI dependencies for the database layer.

These are the seams that route handlers reach for when they need a
repository. Production code calls `Depends(get_user_repository)` and
gets the real `UserRepository` wired against the per-process Motor
client opened in `app.main.lifespan`. Tests override these via
`app.dependency_overrides[...]` (the FastAPI standard pattern).

Why a dedicated module: any future ticket that adds a repository can
register its dependency here, and tests can swap them in one place.
Spreading `get_*` helpers across `main.py` and the repository modules
would create import cycles.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth.local import LocalLoginService
from app.auth.login import OIDCLoginService, OIDCStateStore
from app.auth.oidc import OIDCAdapter
from app.auth.tokens import RefreshTokenService
from app.conversations.service import ConversationService
from app.llm.prompts import PromptProvider
from app.llm.provider import build_chat_model
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.credentials import CredentialRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.roles import RoleRepository
from app.repositories.tool_groups import ToolGroupRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository
from app.repositories.users import UserRepository
from app.security.crypto import CredentialEncryptor
from app.settings import Settings, get_settings
from app.tools.description_generator import ToolDescriptionGenerator
from app.tools.openapi_parser import OpenAPIParser
from app.tools.service import ToolService


def get_database(request: Request) -> AsyncIOMotorDatabase[Any]:
    """FastAPI dependency: return the Motor database handle from `app.state`."""
    db: AsyncIOMotorDatabase[Any] = request.app.state.database
    return db


def get_credential_encryptor(request: Request) -> CredentialEncryptor:
    """FastAPI dependency: return the process-wide `CredentialEncryptor`.

    T05 (#6) stashes the encryptor on `app.state` during the lifespan;
    tests override this dependency with `dependency_overrides[...]` to
    inject a stub.
    """
    encryptor: CredentialEncryptor = request.app.state.credential_encryptor
    return encryptor


def get_user_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> UserRepository:
    """FastAPI dependency: build a `UserRepository` for this request."""
    return UserRepository(db)


def get_refresh_token_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> RefreshTokenRepository:
    """FastAPI dependency: build a `RefreshTokenRepository` for this request."""
    return RefreshTokenRepository(db)


def get_role_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> RoleRepository:
    """FastAPI dependency: build a `RoleRepository` for this request."""
    return RoleRepository(db)


def get_tool_group_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> ToolGroupRepository:
    """FastAPI dependency: build a `ToolGroupRepository` for this request."""
    return ToolGroupRepository(db)


def get_tool_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> ToolRepository:
    """FastAPI dependency: build a `ToolRepository` for this request."""
    return ToolRepository(db)


def get_credential_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
    encryptor: CredentialEncryptor = Depends(get_credential_encryptor),  # noqa: B008
) -> CredentialRepository:
    """FastAPI dependency: build a `CredentialRepository` for this request."""
    return CredentialRepository(db, encryptor)


def get_conversation_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> ConversationRepository:
    """FastAPI dependency: build a `ConversationRepository` for this request."""
    return ConversationRepository(db)


def get_turn_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> TurnRepository:
    """FastAPI dependency: build a `TurnRepository` for this request."""
    return TurnRepository(db)


def get_plan_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> PlanRepository:
    """FastAPI dependency: build a `PlanRepository` for this request."""
    return PlanRepository(db)


def get_plan_execution_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> PlanExecutionRepository:
    """FastAPI dependency: build a `PlanExecutionRepository` for this request."""
    return PlanExecutionRepository(db)


def get_audit_log_repository(
    db: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008  (FastAPI idiom)
) -> AuditLogRepository:
    """FastAPI dependency: build an `AuditLogRepository` for this request."""
    return AuditLogRepository(db)


# ---------------------------------------------------------------------------
# Auth / OIDC (T08 / #46)
# ---------------------------------------------------------------------------


def get_refresh_token_service(
    repo: RefreshTokenRepository = Depends(get_refresh_token_repository),  # noqa: B008
) -> RefreshTokenService:
    """FastAPI dependency: build a `RefreshTokenService` for this request."""
    return RefreshTokenService(repo)


def get_oidc_state_store(request: Request) -> OIDCStateStore:
    """FastAPI dependency: return the process-local `OIDCStateStore`.

    The store is constructed once per process in the lifespan and
    stashed on `app.state`. Tests inject a stub via the same
    `dependency_overrides[...]` pattern as the other deps.
    """
    store: OIDCStateStore = request.app.state.oidc_state_store
    return store


def get_oidc_adapter(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008  (FastAPI idiom)
) -> OIDCAdapter:
    """FastAPI dependency: return the process-wide `OIDCAdapter`.

    The adapter owns an `httpx.AsyncClient`; the lifespan closes it
    on shutdown so we don't leak sockets across a reload.
    """
    adapter: OIDCAdapter = request.app.state.oidc_adapter
    # Bind the settings in case the lifespan-built instance was
    # constructed against a different `Settings`. Tests override this
    # dependency entirely; production uses the lifespan one.
    if adapter is None:  # pragma: no cover — defensive
        adapter = OIDCAdapter(settings)
    return adapter


def get_oidc_login_service(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008  (FastAPI idiom)
    state_store: OIDCStateStore = Depends(get_oidc_state_store),  # noqa: B008
    user_repo: UserRepository = Depends(get_user_repository),  # noqa: B008
    refresh_service: RefreshTokenService = Depends(get_refresh_token_service),  # noqa: B008
) -> OIDCLoginService:
    """FastAPI dependency: return an `OIDCLoginService` for this request.

    Pulls each collaborator from `app.state` / other dependencies and
    wires them together. The login service itself is stateless beyond
    the collaborator references, so a fresh instance per request is
    safe.
    """
    adapter = get_oidc_adapter(request, settings)
    return OIDCLoginService(
        settings=settings,
        oidc_adapter=adapter,
        state_store=state_store,
        user_repository=user_repo,
        refresh_service=refresh_service,
    )


# ---------------------------------------------------------------------------
# Local admin login (T09 / #10)
# ---------------------------------------------------------------------------


def get_local_login_service(
    settings: Settings = Depends(get_settings),  # noqa: B008
    user_repo: UserRepository = Depends(get_user_repository),  # noqa: B008
    refresh_service: RefreshTokenService = Depends(get_refresh_token_service),  # noqa: B008
) -> LocalLoginService:
    """FastAPI dependency: build a `LocalLoginService` for this request.

    The service holds no network state; a fresh instance per request
    is the same cost as a singleton. Tests override this dependency
    to swap in a fixture-built instance (e.g. a stubbed password
    helper) without touching the lifespan.
    """
    return LocalLoginService(
        settings=settings,
        user_repository=user_repo,
        refresh_service=refresh_service,
    )


# ---------------------------------------------------------------------------
# Conversation service (T10 / #40)
# ---------------------------------------------------------------------------


def get_conversation_service(
    conversation_repo: ConversationRepository = Depends(get_conversation_repository),  # noqa: B008
    turn_repo: TurnRepository = Depends(get_turn_repository),  # noqa: B008
    plan_repo: PlanRepository = Depends(get_plan_repository),  # noqa: B008
) -> ConversationService:
    """FastAPI dependency: build a `ConversationService` for this request.

    Stateless beyond the three repository references; a fresh
    instance per request is the same cost as a singleton. Tests
    override this dependency to swap in a fixture-built instance
    without touching the lifespan.
    """
    return ConversationService(
        conversation_repository=conversation_repo,
        turn_repository=turn_repo,
        plan_repository=plan_repo,
    )


# ---------------------------------------------------------------------------
# Tool service (T12 / #11)
# ---------------------------------------------------------------------------


def get_tool_service(
    tool_repo: ToolRepository = Depends(get_tool_repository),  # noqa: B008
) -> ToolService:
    """FastAPI dependency: build a `ToolService` for this request.

    The service is stateless beyond the repository reference; a fresh
    instance per request is fine. Tests override this dependency to
    inject a stubbed service without touching the lifespan.
    """
    return ToolService(tool_repository=tool_repo)


def get_openapi_parser() -> OpenAPIParser:
    """FastAPI dependency: build a fresh `OpenAPIParser` per request (T14 / #12).

    The parser holds no I/O state — every parse call walks the
    in-memory spec dict — so a fresh instance is the same cost as a
    singleton and avoids cross-request bleed-through. Tests that
    want to stub the parser override this dependency rather than
    patching the class.
    """
    return OpenAPIParser()


# ---------------------------------------------------------------------------
# LLM / description generation (T16 / #14)
# ---------------------------------------------------------------------------


async def get_description_generator(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008  (FastAPI idiom)
) -> AsyncIterator[ToolDescriptionGenerator]:
    """FastAPI dependency: the shared `ToolDescriptionGenerator` (T16 / #14).

    Production reads the instance the lifespan stashed on `app.state`.
    The fallback branch exists for the test harness: fixtures that
    build `create_app()` without entering the lifespan (ASGITransport
    skips startup) still hit this dependency through the real router.
    It builds a throwaway generator — and closes its httpx client
    afterwards, so the fallback can't leak sockets — with the same
    settings the route sees. Tests that care about generation behaviour
    override this dependency outright (the canonical seam).
    """
    existing = getattr(request.app.state, "description_generator", None)
    if isinstance(existing, ToolDescriptionGenerator):
        yield existing
        return

    client = httpx.AsyncClient()
    try:
        yield ToolDescriptionGenerator(
            settings=settings,
            prompt_provider=PromptProvider(settings=settings, http_client=client),
            chat_model_factory=lambda: build_chat_model(settings),
        )
    finally:
        await client.aclose()
