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

from typing import Any

from fastapi import Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.repositories.credentials import CredentialRepository
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.roles import RoleRepository
from app.repositories.tool_groups import ToolGroupRepository
from app.repositories.tools import ToolRepository
from app.repositories.users import UserRepository
from app.security.crypto import CredentialEncryptor


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
