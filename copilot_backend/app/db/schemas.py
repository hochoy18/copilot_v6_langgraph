"""Pydantic schemas for the four core MongoDB collections.

These are the wire shapes of `users`, `roles`, `refresh_tokens`, and
`tool_groups`. The acceptance criteria for T04 (#5) require the schemas
to be documented; Pydantic gives both documentation and runtime
validation in one artefact. Each model carries an `InDB` variant that
mirrors the persisted form (ObjectId-based `_id`, datetimes stored as
BSON datetimes) and a `Create` / `Update` variant for repository
inputs that should not carry an `_id`.

Naming convention:

* `<Entity>` — the canonical read shape (what callers get back from a
  repository).
* `<Entity>InDB` — the persisted shape, including `_id`. Repositories
  read with this and convert to the canonical shape on the way out.
* `<Entity>Create` — input shape for `create(...)`. No `_id`, no
  `created_at` / `updated_at` — the repository stamps those.

References between documents use `bson.ObjectId` strings so the models
stay JSON-serialisable for tests and API responses.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, EmailStr, Field

# A short alias so every `Field(default_factory=ObjectId, alias="_id")`
# reads the same. Centralising the alias keeps `model_dump(by_alias=True)`
# call sites uniform.
PyObjectId = Annotated[str, Field(default_factory=lambda: str(ObjectId()))]


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


# Allowed source values for a `User`. Mirrors ADR-0006: SSO or local
# admin. New sources (e.g. SAML) would extend this Literal.
UserSource = Literal["sso", "local"]


class UserBase(BaseModel):
    """Fields shared between create / read shapes for `users`.

    Email is required for both SSO and local paths so the user can be
    looked up by `find_one({"email": ...})` regardless of source.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="Unique login email. Indexed unique.")
    display_name: str = Field(
        min_length=1, max_length=128, description="Human-readable name shown in the UI."
    )
    source: UserSource = Field(
        description=(
            "Identity source. `sso` (ADR-0006 — federated via OIDC) or "
            "`local` (admin path with username + password)."
        )
    )
    is_active: bool = Field(
        default=True,
        description="Inactive users cannot obtain tokens but the row is kept for audit.",
    )


class UserCreate(UserBase):
    """Input shape for creating a new user.

    `sso_subject` and `local_username` / `password_hash` are source-
    specific — pass exactly one set based on `source`. Both being
    present is rejected by the partial-unique indexes defined in
    `indexes.py`.
    """

    sso_subject: str | None = Field(
        default=None,
        description="SSO `sub` claim. Indexed unique-sparse. Required iff `source='sso'`.",
    )
    local_username: str | None = Field(
        default=None,
        description="Admin local username. Indexed unique-sparse. Required iff `source='local'`.",
    )
    password_hash: str | None = Field(
        default=None,
        description=(
            "bcrypt/argon2 hash of the local password. Never persisted "
            "for SSO users. Never returned in API responses."
        ),
    )
    role_ids: list[str] = Field(
        default_factory=list,
        description="ObjectIds of `roles._id` granted to this user.",
    )


class UserUpdate(BaseModel):
    """Partial update shape for `users`.

    Every field is optional so `PATCH` semantics hold: an empty patch is
    a no-op. `role_ids` replaces the list atomically — partial diff
    semantics belong in the admin API layer, not the repository.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    role_ids: list[str] | None = None
    password_hash: str | None = None


class UserInDB(UserBase):
    """Persisted shape of a `users` document.

    `_id` is the canonical Mongo ObjectId (as a string). `created_at` /
    `updated_at` are stamped by the repository on insert and on each
    update. `password_hash` is in the persisted row but the
    repository's `get_*` methods strip it from responses — it should
    never reach the API surface.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    sso_subject: str | None = None
    local_username: str | None = None
    password_hash: str | None = None
    role_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class User(UserBase):
    """Canonical read shape — what API responses return.

    Drops `password_hash` so a leaky `find_one` cannot escalate to a
    credential disclosure. `id` is the string form of `_id`.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    sso_subject: str | None = None
    local_username: str | None = None
    role_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: UserInDB) -> User:
        """Strip `password_hash` and return the canonical shape."""
        return cls(
            _id=row.id,
            email=row.email,
            display_name=row.display_name,
            source=row.source,
            is_active=row.is_active,
            sso_subject=row.sso_subject,
            local_username=row.local_username,
            role_ids=list(row.role_ids),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


class RoleBase(BaseModel):
    """Fields shared between create / read shapes for `roles`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Role slug. Indexed unique. Conventional values: `admin`, "
            "`user`, and one role per business group (e.g. `finance_user`)."
        ),
    )
    description: str = Field(
        default="", max_length=512, description="Human-readable description."
    )


class RoleCreate(RoleBase):
    """Input shape for creating a new role.

    `tool_group_ids` is empty by default — assigning tool groups is a
    separate admin operation (see ADR-0031 `PUT /admin/users/{id}/roles`).
    """

    tool_group_ids: list[str] = Field(
        default_factory=list,
        description="ObjectIds of `tool_groups._id` this role grants access to.",
    )


class RoleUpdate(BaseModel):
    """Partial update shape for `roles`."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=512)
    tool_group_ids: list[str] | None = None


class RoleInDB(RoleBase):
    """Persisted shape of a `roles` document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    tool_group_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Role(RoleBase):
    """Canonical read shape."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    tool_group_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# refresh_tokens
# ---------------------------------------------------------------------------


class RefreshTokenBase(BaseModel):
    """Fields shared between create / read shapes for `refresh_tokens`.

    The token itself is never persisted in cleartext: only `token_hash`
    is stored. Callers compare incoming tokens against `token_hash` (a
    SHA-256 over the raw token) to verify. Per ADR-0009 the raw token
    is short-lived (7 days) and is rotated on every refresh.
    """

    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 hex digest of the opaque token. Indexed unique.",
    )
    user_id: str = Field(
        description="ObjectId of `users._id`. Indexed.",
    )
    expires_at: datetime = Field(
        description="Token expiry. A TTL index purges rows past this instant."
    )


class RefreshTokenCreate(RefreshTokenBase):
    """Input shape for creating a new refresh token."""

    replaced_by: str | None = Field(
        default=None,
        description=(
            "When this token is rotated, the new token's `_id` is recorded here "
            "for audit / chain reconstruction."
        ),
    )


class RefreshTokenInDB(RefreshTokenBase):
    """Persisted shape of a `refresh_tokens` document.

    `revoked_at` is the cornerstone of the rotation flow: setting it
    renders the token unusable even if `expires_at` has not elapsed,
    which is how the backend implements "admin force-logout".
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    revoked_at: datetime | None = None
    replaced_by: str | None = None


# ---------------------------------------------------------------------------
# tool_groups
# ---------------------------------------------------------------------------


class ToolGroupBase(BaseModel):
    """Fields shared between create / read shapes for `tool_groups`.

    `tool_groups` is the permission-granting bridge between `roles` and
    `tools`: a role grants its user every Tool in the listed groups.
    See ADR-0002 + ADR-0003.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="Group slug. Indexed unique. Conventional: `finance`, `sales`, etc.",
    )
    description: str = Field(
        default="", max_length=512, description="Human-readable description."
    )


class ToolGroupCreate(ToolGroupBase):
    """Input shape for creating a new tool group.

    `tool_ids` is empty at creation time — Tools land in T05 (#6) and
    the group→tool membership is a separate admin operation.
    """

    tool_ids: list[str] = Field(
        default_factory=list,
        description="ObjectIds of `tools._id` belonging to this group.",
    )


class ToolGroupUpdate(BaseModel):
    """Partial update shape for `tool_groups`."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=512)
    tool_ids: list[str] | None = None


class ToolGroupInDB(ToolGroupBase):
    """Persisted shape of a `tool_groups` document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    tool_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ToolGroup(ToolGroupBase):
    """Canonical read shape."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    tool_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
