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
from typing import Annotated, Any, Literal

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


# ---------------------------------------------------------------------------
# tools  (T05 / #6)
# ---------------------------------------------------------------------------


# Per ADR-0004 / CONTEXT.md `risk_level` drives the HITL decision: `read`
# runs automatically, `write` / `destructive` pause for human approval.
# A new risk tier would extend this Literal — older tiers stay stable.
ToolRiskLevel = Literal["read", "write", "destructive"]

# Per ADR-0018 the Tool lifecycle is `draft` / `active` / `disabled`.
# The LLM Planner only ever sees Tools in `active` state; `draft` is
# admin-review-pending and `disabled` is admin-taken-offline.
ToolStatus = Literal["draft", "active", "disabled"]

# Per ADR-0003 Tools come in via two paths. The persisted `source_ref`
# points to the originating artefact (an OpenAPI operation or a manual
# draft blob) so admin tooling can re-derive the Tool if upstream
# changes.
ToolSource = Literal["openapi", "manual"]


class ToolBase(BaseModel):
    """Fields shared between create / read shapes for `tools`.

    `name` is the LLM-facing slug — what the Planner emits in a `tool_call`.
    `risk_level` and `status` together gate visibility (Planner sees only
    `active` Tools, regardless of risk). `parameters_schema` is the JSON
    Schema validated against by the Worker (ADR-0020).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=128,
        description="Slug surfaced to the LLM in `tool_call.name`. Indexed unique.",
    )
    description: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "LLM-friendly description. LLM-generated at create, "
            "admin-reviewed before activation (ADR-0018)."
        ),
    )
    risk_level: ToolRiskLevel = Field(
        description="Drives HITL: `read` runs unattended, others pause for approval (ADR-0004).",
    )
    status: ToolStatus = Field(
        default="draft",
        description=(
            "Tool lifecycle. `draft` = admin-review-pending, "
            "`active` = Planner-visible, `disabled` = taken offline (ADR-0018)."
        ),
    )
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "JSON Schema for the Tool's arguments. Validated against "
            "by the Worker before invocation (ADR-0020)."
        ),
    )
    http_method: str = Field(
        min_length=1,
        max_length=16,
        description="HTTP method for the upstream call (ADR-0003). E.g. `GET`, `POST`.",
    )
    http_url_template: str = Field(
        min_length=1,
        max_length=2048,
        description="URL template for the upstream call (ADR-0003). Supports `{var}` placeholders.",
    )
    http_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Static headers attached to every call.",
    )
    http_body_template: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON template for the request body. The Worker "
            "renders `parameters` into this template before sending."
        ),
    )
    source: ToolSource = Field(
        description="Originating path per ADR-0003: `openapi` import or `manual` registration.",
    )
    source_ref: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Opaque pointer to the source document (OpenAPI operationId "
            "or manual draft id). Lets admin tooling re-derive the Tool."
        ),
    )


class ToolCreate(ToolBase):
    """Input shape for creating a Tool.

    `credentials_ref` is a string `ObjectId` reference to `credentials._id`
    (not embedded — credentials are encrypted at rest and shared across
    Tools). Setting it to None means the Tool runs unauthenticated (rare;
    mainly internal health pings).
    """

    credentials_ref: str | None = Field(
        default=None,
        description="ObjectId of `credentials._id`. Indexed; null for unauthenticated Tools.",
    )


class ToolUpdate(BaseModel):
    """Partial update shape for `tools`.

    Every field is optional so PATCH semantics hold. `risk_level` is
    intentionally updatable: an admin promoting a Tool from `read` to
    `write` shouldn't have to delete-and-recreate the row (Plan
    snapshots preserve the previous `risk_level` for already-issued
    Plans — see ADR-0027).
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1, max_length=4096)
    risk_level: ToolRiskLevel | None = None
    status: ToolStatus | None = None
    parameters_schema: dict[str, Any] | None = None
    http_method: str | None = Field(default=None, min_length=1, max_length=16)
    http_url_template: str | None = Field(default=None, min_length=1, max_length=2048)
    http_headers: dict[str, str] | None = None
    http_body_template: dict[str, Any] | None = None
    credentials_ref: str | None = None


class ToolInDB(ToolBase):
    """Persisted shape of a `tools` document.

    `_id` is the canonical Mongo ObjectId (as a string). `created_at` /
    `updated_at` are stamped by the repository. `credentials_ref` is a
    foreign-key style pointer to `credentials._id`; the repository
    keeps it as a string so the persisted doc stays JSON-friendly for
    tests.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    credentials_ref: str | None = None
    created_at: datetime
    updated_at: datetime


class Tool(ToolInDB):
    """Canonical read shape — what API responses return.

    Inherits every persisted field directly. Unlike `User.from_db`
    there's no secret to redact here: the encryption layer keeps the
    credential bytes out of the Tool document entirely (they live in
    `credentials`). What `Tool` carries is just the foreign-key
    pointer.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# credentials  (T05 / #6)
# ---------------------------------------------------------------------------


# Per ADR-0002 the Credential type is whatever the upstream API needs.
# The four values here cover the common enterprise auth schemes; a
# new type (e.g. `oauth2_client_credentials`) extends this Literal.
CredentialAuthType = Literal["api_key", "bearer", "basic", "mtls"]


class CredentialBase(BaseModel):
    """Fields shared between create / read shapes for `credentials`.

    The encrypted payload is opaque to every layer except the Worker
    that injects it at call time. `auth_type` tells the Worker how to
    shape the bytes into the right HTTP header / TLS context.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Admin-facing label. Indexed unique. Conventional: "
            "`<system>-<env>`, e.g. `salesforce-prod`."
        ),
    )
    auth_type: Literal["api_key", "bearer", "basic", "mtls"] = Field(
        description=(
            "How the Worker should shape the decrypted bytes: "
            "`api_key` → `X-Api-Key`, `bearer` → `Authorization: Bearer …`, "
            "`basic` → `Authorization: Basic …`, `mtls` → client cert."
        ),
    )


class CredentialCreate(CredentialBase):
    """Input shape for creating a Credential.

    `plaintext_payload` is the unencrypted bytes (or JSON object) the
    repository seals before persisting. Repositories accept `dict`
    here for the common `{"key": "...", "secret": "..."}` shape and
    serialise to JSON internally; tests can pass `bytes` directly for
    arbitrary binary credentials.
    """

    plaintext_payload: dict[str, Any] | bytes = Field(
        description=(
            "Unencrypted credential material. Dicts are JSON-encoded "
            "with sorted keys before encryption; bytes pass through "
            "verbatim. The repository rejects any other type via "
            "`_serialise_plaintext` before the encryptor sees it — "
            "never log this field."
        ),
    )


class CredentialUpdate(BaseModel):
    """Partial update shape for `credentials`.

    Only `name` is updatable through this model; rotating the credential
    bytes is a separate `rotate_payload` call (see ADR-0024) that
    stamps `last_rotated_at`. Letting `update` touch the bytes would
    make rotation indistinguishable from generic edits in audit logs.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    auth_type: CredentialAuthType | None = None


class CredentialInDB(CredentialBase):
    """Persisted shape of a `credentials` document.

    `payload` / `nonce` are `bytes` (BSON `Binary` on the wire) and are
    NEVER returned by the canonical read shape — they're the at-rest
    encrypted bytes (ADR-0002). `key_id` records which master-key
    sealed the row so a future multi-key rotation can re-open older
    ciphertexts.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    payload: bytes = Field(description="AES-256-GCM ciphertext. Decrypt via the encryptor.")
    nonce: bytes = Field(description="12-byte GCM nonce. Distinct per row.")
    key_id: str = Field(description="Identifier of the master key that sealed this row.")
    created_at: datetime
    updated_at: datetime
    last_rotated_at: datetime | None = Field(
        default=None,
        description="Stamp from the most recent `rotate_payload` call (ADR-0024).",
    )


class Credential(CredentialBase):
    """Canonical read shape — what API responses return.

    Strips `payload` / `nonce` so a future Tool by ID endpoint cannot
    accidentally surface ciphertext bytes (decryption happens only in
    the Worker). The repository's `get_in_db` variant returns the
    persisted row with the bytes intact.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    updated_at: datetime
    last_rotated_at: datetime | None = None

    @classmethod
    def from_db(cls, row: CredentialInDB) -> Credential:
        """Strip the encrypted bytes + nonce and return the canonical shape."""
        return cls(
            _id=row.id,
            name=row.name,
            auth_type=row.auth_type,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_rotated_at=row.last_rotated_at,
        )
