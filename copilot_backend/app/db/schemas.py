"""Pydantic schemas for the MongoDB collections.

These are the wire shapes of every collection the backend owns:

* `users`, `roles`, `refresh_tokens`, `tool_groups` — T04 (#5).
* `tools`, `credentials` — T05 (#6).
* `conversations`, `turns`, `plans`, `plan_executions`, `audit_logs`
  — T06 (#7).
* `plans` restructured to the `nodes` / `edges` / `tool_snapshots`
  trio — T17 (#15), per SPEC §Data model and ADR-0027.
* `refresh_tokens.family_id` — T07 (#8); the rotation flow attaches a
  fresh `family_id` on every login and inherits it on rotate so reuse
  of a revoked token can revoke the entire chain.

The acceptance criteria for T04 require the schemas to be documented;
Pydantic gives both documentation and runtime validation in one
artefact. Each model carries an `InDB` variant that mirrors the
persisted form (ObjectId-based `_id`, datetimes stored as BSON
datetimes) and a `Create` / `Update` variant for repository inputs
that should not carry an `_id`.

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
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

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

    `email` (T08 / #46) lets the OIDC login flow mirror an IdP-side
    email change onto an existing `sso` user without forcing a
    delete-and-recreate (which would orphan audit trails).
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    role_ids: list[str] | None = None
    password_hash: str | None = None
    email: EmailStr | None = None


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

    `family_id` (T07 / #8) groups every token issued against one login
    into a chain. Rotation reuses the same `family_id`; reuse of an
    already-revoked token within a family triggers a whole-family
    revoke (OAuth 2.0 Security BCP, "Refresh Token Protection").
    Independent logins get distinct `family_id`s so a stolen token on
    one device does not log the user out everywhere.
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
    family_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Rotation-chain identifier. A login issues a fresh UUID; "
            "rotations inherit it. Indexed non-unique so family-wide "
            "revocations stay one query."
        ),
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


# ---------------------------------------------------------------------------
# conversation-domain enums  (T06 / #7)
# ---------------------------------------------------------------------------


# Per ADR-0011 the conversation lifecycle is `active` / `idle` /
# `archived`. A new state (e.g. a "pinned" flag) extends this Literal.
ConversationStatus = Literal["active", "idle", "archived"]

# Per ADR-0005 a Turn is one message inside a conversation. The
# `assistant` role covers both the LLM's text reply and the SSE-
# streamed final answer — `tool` is intentionally absent: tool-side
# events live in `audit_logs`, not the chat transcript.
TurnRole = Literal["user", "assistant", "system"]

# Per ADR-0004 / ADR-0019 a Plan moves through:
#   pending    — Planner produced, awaiting HITL review (mandatory)
#   approved   — business user approved the Plan as-is
#   modified   — business user edited parameters (ADR-0019) and we
#                captured the diff for audit
#   rejected   — business user rejected; Turn stays, no execution
#   executing  — Worker is running the Plan (DAG)
#   succeeded  — every node finished without error
#   failed     — at least one node hit an unrecoverable error
#   aborted    — business user cancelled mid-execution
PlanStatus = Literal[
    "pending",
    "approved",
    "modified",
    "rejected",
    "executing",
    "succeeded",
    "failed",
    "aborted",
]

# Per ADR-0004 + ADR-0017 a node's runtime outcome is finer-grained:
#   pending     — not yet picked up by the Worker
#   running     — Worker is mid-execution
#   succeeded   — upstream returned 2xx / success body
#   failed      — upstream 4xx/5xx, schema violation, or transport error
#                that won't be auto-retried (write/destructive path)
#   skipped     — business user chose "skip this node" mid-Plan
#   cancelled   — execution aborted before this node started
PlanNodeStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]

# Per ADR-0028 audit log status is about lifecycle + archival:
#   active    — hot-stored, queryable via MongoDB
#   archived  — moved to cold storage; row remains as a tombstone
#               pointing at the cold-storage location
#   recalled  — temporarily restored from cold storage (T42)
AuditLogStatus = Literal["active", "archived", "recalled"]


# ---------------------------------------------------------------------------
# conversations  (T06 / #7)
# ---------------------------------------------------------------------------


class ConversationBase(BaseModel):
    """Fields shared between create / read shapes for `conversations`.

    Per ADR-0005 the conversation is the audit / replay unit of work —
    not the Turn, not the Plan. Per ADR-0011 lifecycle is one of three
    states; admins and end users both move the row across them.

    `user_id` identifies the business user who owns the conversation.
    The role list is resolved at request time from the user's role
    grants — we deliberately don't embed it here to avoid a
    write-skew with `users.role_ids` (ADR-0006).
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        description=(
            "ObjectId of `users._id`. "
            "Compound index with `last_activity_at` for the list view."
        ),
    )
    title: str = Field(
        default="",
        max_length=256,
        description=(
            "Display name. Empty until the Planner or the user picks a title; "
            "frontend uses a truncated slice of the first user message by default."
        ),
    )
    status: ConversationStatus = Field(
        default="active",
        description="Lifecycle per ADR-0011: `active` / `idle` / `archived`.",
    )


class ConversationCreate(ConversationBase):
    """Input shape for creating a new conversation.

    `last_activity_at` is set to `created_at` by the repository so
    any "active in last 15 min" filter (ADR-0011) works the moment
    the row lands.
    """


class ConversationUpdate(BaseModel):
    """Partial update shape for `conversations`.

    `title` is the only user-facing field set by PATCH; lifecycle
    transitions have their own repository methods (`set_status`,
    `touch_activity`) so audit hooks see them as discrete events.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=256)


class ConversationInDB(ConversationBase):
    """Persisted shape of a `conversations` document.

    `last_activity_at` is the gate for ADR-0011's idle transition:
    a scheduled job (T39) flips active conversations to `idle` once
    this timestamp is older than 15 minutes.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime


class Conversation(ConversationInDB):
    """Canonical read shape — what API responses return.

    Inherits every persisted field. Unlike `User` there's no secret
    to redact: conversation metadata is safe to surface.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# turns  (T06 / #7)
# ---------------------------------------------------------------------------


class TurnBase(BaseModel):
    """Fields shared between create / read shapes for `turns`.

    Per ADR-0005 a Turn is one message inside a conversation. The
    `user` role carries the user's natural-language instruction; the
    `assistant` role holds the final LLM answer (or a streaming-
    aggregated snapshot at read time). `system` is reserved for
    internal messages the runtime inserts (e.g. "Plan generated").

    `plan_id` is optional because some Turns (smalltalk, chit-chat)
    don't need a Plan — per ADR-0004 the Planner is allowed to skip
    Plan generation when no Tool is involved.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(
        description="ObjectId of `conversations._id`. Indexed.",
    )
    role: TurnRole = Field(
        description="Who produced the message — `user` / `assistant` / `system`.",
    )
    content: str = Field(
        description="Markdown-rendered message body. May be empty for tool-only turns.",
    )
    plan_id: str | None = Field(
        default=None,
        description=(
            "ObjectId of `plans._id` attached to this Turn. Indexed. "
            "`None` for Turns that bypass the Planner (smalltalk). "
            "Per ADR-0019 an edited Plan keeps a single `plan_id`; the diff "
            "lives on the Plan document, not as a separate Turn."
        ),
    )


class TurnCreate(TurnBase):
    """Input shape for creating a Turn.

    `extra` is the seam for tool-only metadata (e.g. the SSE
    completion time, the model name used, etc.) that the chat UI
    doesn't render but audit / Langfuse traces (T40) do.
    """

    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Open-ended metadata for forensic / trace use. Not rendered in chat UI.",
    )


class TurnInDB(TurnBase):
    """Persisted shape of a `turns` document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    extra: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Turn(TurnInDB):
    """Canonical read shape — what API responses return.

    Inherits every persisted field; nothing redacted.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# plans  (T06 / #7; restructured to nodes / edges / tool_snapshots by
# T17 / #15)
#
# Documented Plan shape (SPEC §Data model, ADR-0027):
#
#   {
#     "conversation_id": "<users conversation oid>",
#     "turn_id":         "<turns oid that triggered this Plan>",
#     "status":          "pending | approved | modified | ... (PlanStatus)",
#     "nodes": [
#       {"node_id": "n1", "tool": "list_customers",
#        "parameters": {"region": "emea"}, "notes": "..."},
#       ...
#     ],
#     "edges": [{"source": "n1", "target": "n2"}, ...],   # ADR-0012 DAG
#     "tool_snapshots": [                                   # ADR-0027 freeze
#       {"tool_id": "<tools oid>", "name": "list_customers",
#        "description": "...", "risk_level": "read",
#        "parameters_schema": {...}, "http_method": "GET",
#        "http_url_template": "...", "http_headers": {...},
#        "http_body_template": null},
#       ...
#     ],
#     "edited_diff": null,   # set by ADR-0019 edits: {"by_node_id": {...}}
#     "created_at": ..., "updated_at": ...
#   }
#
# Structural invariants (enforced by `PlanBase._validate_structure`):
# unique node_ids / snapshot names, every node.tool bound to a frozen
# snapshot, edges reference existing nodes without self-loops or
# duplicates, and the graph is acyclic.
# ---------------------------------------------------------------------------


class ToolSnapshot(BaseModel):
    """Frozen Tool definition embedded in a Plan (or one audit row).

    Per ADR-0027 a Plan carries snapshots of every Tool it references
    so audits / replays see "which Tool was actually invoked" rather
    than "the Tool's current state". The fields mirror the persisted
    `tools` document minus `_id`, `created_at`, `updated_at`,
    `status`, `source`, `source_ref`, and `credentials_ref` (source
    metadata is admin provenance, not execution input; credentials
    are an FK pointer resolved at call time, ADR-0002). `tool_id`
    keeps the pointer to the live row so
    Plan-Tool binding validation (T44) can compare snapshot-vs-latest
    and warn on material drift.

    The Worker's execution contract (ADR-0027 §3) reads this model
    directly: `parameters_schema` validates arguments (ADR-0020),
    `risk_level` drives HITL (ADR-0004), and the `http_*` fields are
    the request template.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str | None = Field(
        default=None,
        description=(
            "ObjectId of `tools._id` at freeze time. Optional so "
            "hand-built / older snapshots still parse; T44's drift "
            "check needs it whenever the Planner generates a Plan."
        ),
    )
    name: str = Field(
        min_length=1,
        max_length=128,
        description="Tool slug at Plan-generation time. Mirrors `ToolBase.name` bounds.",
    )
    description: str = Field(description="LLM-facing description at freeze time.")
    risk_level: ToolRiskLevel = Field(
        description="Risk tier at freeze time. Drives per-node HITL re-confirmation.",
    )
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the Tool's arguments (T44 reads this).",
    )
    http_method: str = Field(description="HTTP method at freeze time.")
    http_url_template: str = Field(description="URL template at freeze time.")
    http_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Static headers at freeze time.",
    )
    http_body_template: dict[str, Any] | None = Field(
        default=None,
        description="Optional JSON body template at freeze time.",
    )


class PlanEdge(BaseModel):
    """One data-dependency edge inside a Plan DAG (CONTEXT.md 术语 "Plan").

    `source -> target` means "target runs after source succeeds"
    (ADR-0012). The edge carries the ordering guarantee only — no
    payload rides it: where a downstream `parameters` value depends
    on an upstream result, the Worker binds it at execution time
    (T21 / T25). React Flow (T19) consumes this pair directly as
    `{ source, target }`.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        max_length=64,
        description="`node_id` of the predecessor node.",
    )
    target: str = Field(
        max_length=64,
        description="`node_id` of the successor node.",
    )


class PlanNode(BaseModel):
    """One Tool invocation inside a Plan DAG.

    T17 (#15) split the T06 shape: a node is pure invocation —
    which Tool (`tool`, referencing `plan.tool_snapshots[].name`),
    with what `parameters`, and the Planner's `notes`. The frozen
    definition lives on the Plan (`tool_snapshots`, ADR-0027) and
    the topology lives in `plan.edges` — no per-node duplication of
    either.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(
        max_length=64,
        description=(
            "Stable per-Plan identifier — distinct from Mongo `_id`. "
            "Edges and `edited_diff` keys reference this string. "
            "Conventional: `n1`, `n2`, …"
        ),
    )
    tool: str = Field(
        max_length=128,
        description=(
            "Slug of the Tool invoked. Must match exactly one "
            "`plan.tool_snapshots[].name` (ADR-0027 binding, "
            "enforced by the `PlanBase` validator)."
        ),
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Resolved Tool arguments at Plan generation. Validated "
            "against the referenced snapshot's `parameters_schema` "
            "by the Worker (ADR-0020). Business user may edit "
            "(ADR-0019)."
        ),
    )
    notes: str = Field(
        default="",
        max_length=512,
        description="Free-form semantic note the Planner attaches for the LLM-facing description.",
    )


def _unique_names(values: list[str], label: str) -> set[str]:
    """Collect `values` into a set, rejecting duplicates under `label`.

    Shared by the node-id and snapshot-name checks in
    `PlanBase._validate_structure` — both are "this array's key must
    be a set" invariants with the same error shape.
    """
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"Plan has duplicate {label} '{value}'")
        seen.add(value)
    return seen


class PlanBase(BaseModel):
    """Fields shared between create / read shapes for `plans`.

    The T17 (#15) document shape, mirroring SPEC §Data model —
    `conversation_id / turn_id / nodes / edges / tool_snapshots /
    status`:

    * ``nodes`` — the Tool invocations (id, Tool slug, parameters,
      notes). List order is not significant; `edges` carries
      topology.
    * ``edges`` — `source -> target` dependency pairs (ADR-0012).
    * ``tool_snapshots`` — one frozen Tool definition per distinct
      Tool referenced (ADR-0027). Nodes bind by `name`.

    The model validator enforces the structural contract every
    downstream consumer leans on — see `_validate_structure`:

    1. `node_id` unique; snapshot `name` unique.
    2. Every node's `tool` references a frozen snapshot (ADR-0027 —
       a Plan must be executable and replayable from its own doc).
    3. Edge endpoints reference existing nodes; no self-loops or
       duplicate edges.
    4. The graph is acyclic (the ADR-0012 executor topologically
       sorts it; a cycle would hang the run).

    Per ADR-0019 the `edited_diff` field (on `PlanInDB`) is populated
    when the business user edits parameters; it's a `before → after`
    JSON diff keyed by `node_id`.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(
        description="ObjectId of `conversations._id`. Indexed.",
    )
    turn_id: str = Field(
        description="ObjectId of `turns._id` that triggered this Plan. Indexed.",
    )
    status: PlanStatus = Field(
        default="pending",
        description="Lifecycle (see `PlanStatus` enum docstring).",
    )
    nodes: list[PlanNode] = Field(
        description=(
            "Tool invocations in the Plan. Each `node.tool` must "
            "match one `tool_snapshots[].name` (ADR-0027)."
        ),
    )
    edges: list[PlanEdge] = Field(
        default_factory=list,
        description=(
            "Dependency edges (`source` runs before `target`). "
            "A node with no incoming edge is a root and may run in "
            "parallel with other roots (ADR-0012)."
        ),
    )
    tool_snapshots: list[ToolSnapshot] = Field(
        description=(
            "Tool definitions frozen at Plan-generation time, one per "
            "distinct referenced Tool (ADR-0027). Required: a Plan "
            "with nodes but no snapshots cannot bind them. "
            "Unreferenced snapshots are tolerated (harmless Planner "
            "noise); unfrozen references are rejected."
        ),
    )

    @model_validator(mode="after")
    def _validate_structure(self) -> PlanBase:
        """Enforce the structural invariants documented on `PlanBase`."""
        seen_node_ids = _unique_names(
            [node.node_id for node in self.nodes], "node_id"
        )
        snapshot_names = _unique_names(
            [snap.name for snap in self.tool_snapshots], "tool_snapshot name"
        )

        for node in self.nodes:
            if node.tool not in snapshot_names:
                raise ValueError(
                    f"node '{node.node_id}' references unknown tool snapshot "
                    f"'{node.tool}' — every invocation must bind to a frozen "
                    "snapshot (ADR-0027)"
                )

        seen_edges: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source == edge.target:
                raise ValueError(
                    f"edge {edge.source} -> {edge.target} is self-referencing"
                )
            for endpoint in (edge.source, edge.target):
                if endpoint not in seen_node_ids:
                    raise ValueError(
                        f"edge {edge.source} -> {edge.target} references "
                        f"unknown node '{endpoint}'"
                    )
            key = (edge.source, edge.target)
            if key in seen_edges:
                raise ValueError(f"Plan has duplicate edge {edge.source} -> {edge.target}")
            seen_edges.add(key)

        # Kahn's algorithm: whatever survives the leaf-peeling sits on
        # (or feeds) a cycle.
        indegree = {node.node_id: 0 for node in self.nodes}
        outgoing: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            indegree[edge.target] += 1
            outgoing[edge.source].append(edge.target)
        frontier = [nid for nid, deg in indegree.items() if deg == 0]
        resolved = 0
        while frontier:
            nid = frontier.pop()
            resolved += 1
            for nxt in outgoing[nid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    frontier.append(nxt)
        if resolved != len(indegree):
            blocked = sorted(nid for nid, deg in indegree.items() if deg > 0)
            raise ValueError(
                "Plan edges contain a cycle; nodes on or downstream of it "
                f"{blocked} — the DAG executor (ADR-0012) cannot schedule this Plan"
            )
        return self


class PlanCreate(PlanBase):
    """Input shape for creating a Plan.

    `edited_diff` lands on Plan only after a human edit (ADR-0019),
    so the create path defaults it to `None`. The repository enforces
    at least one node — a Plan with zero nodes has no audit value and
    confuses the Frontend DAG renderer.
    """


class PlanUpdate(BaseModel):
    """Partial update shape for `plans`.

    Lifecycle transitions have dedicated repository methods so audit
    hooks see them as discrete events; this model only covers the
    user-driven edits described in ADR-0019.
    """

    model_config = ConfigDict(extra="forbid")

    status: PlanStatus | None = None
    edited_diff: dict[str, Any] | None = Field(
        default=None,
        description="JSON diff between the original and edited Plan (ADR-0019).",
    )


class PlanInDB(PlanBase):
    """Persisted shape of a `plans` document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    edited_diff: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class Plan(PlanInDB):
    """Canonical read shape — what API responses return.

    Inherits every persisted field; nothing redacted. The DAG is
    returned verbatim so the React Flow renderer (T19) can lay it
    out without re-deriving edges.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# plan_executions  (T06 / #7)
# ---------------------------------------------------------------------------


class PlanNodeResult(BaseModel):
    """Outcome of one node inside a `plan_execution`.

    Captures the per-node lifecycle: which tool, what parameters
    were sent, what came back, whether it succeeded. The error shape
    follows the Worker-side envelope so a future SSE consumer can
    replay the result without re-running.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(description="Stable Plan-node id this result refers to.")
    status: PlanNodeStatus = Field(
        description="Per-node status — see `PlanNodeStatus` for the lifecycle.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="Wall-clock start. Stamped by the Worker on `tool.started`.",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="Wall-clock finish. Stamped by the Worker on terminal status transition.",
    )
    request: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Outgoing HTTP request payload sent to the upstream API. "
            "Persisted alongside the result (ADR-0028). None until the "
            "request has been sent."
        ),
    )
    response: dict[str, Any] | None = Field(
        default=None,
        description="Upstream API response body (success or error). None until terminal.",
    )
    error: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured error envelope (code / message / details) on failure. "
            "None on success."
        ),
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description="How many times the Worker retried before this terminal state.",
    )


class PlanExecutionBase(BaseModel):
    """Fields shared between create / read shapes for `plan_executions`.

    One per Plan; one at most per Plan (a re-execution after a
    business user action creates a new execution row rather than
    mutating the prior one, so audit trails see each attempt
    separately).
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(
        description="ObjectId of `plans._id`. Indexed.",
    )
    conversation_id: str = Field(
        description="ObjectId of `conversations._id`. Denormalised for fast session-level rollups.",
    )
    status: Literal["running", "completed", "failed", "aborted"] = Field(
        default="running",
        description="Aggregate execution status — rolled up from `node_results`.",
    )
    node_results: list[PlanNodeResult] = Field(
        default_factory=list,
        description=(
            "Per-node outcomes keyed by `node_id`. Includes pending nodes "
            "with status `pending` so the Frontend can render a partial DAG."
        ),
    )


class PlanExecutionCreate(PlanExecutionBase):
    """Input shape for creating a PlanExecution.

    The first call carries `node_results=[]` (the Worker appends as
    each node terminalises). `started_at` defaults to the repository
    clock; `finished_at` stays `None` until the execution closes.
    """


class PlanExecutionUpdate(BaseModel):
    """Partial update shape for `plan_executions`.

    The Worker uses `upsert_node_result` for per-node appends and
    `set_status` for the aggregate terminal. This `update` path
    covers edge cases (e.g. attaching an admin note) — most fields
    are intentionally NOT modifiable here to keep audit semantics
    clear.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "completed", "failed", "aborted"] | None = None


class PlanExecutionInDB(PlanExecutionBase):
    """Persisted shape of a `plan_executions` document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    started_at: datetime
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PlanExecution(PlanExecutionInDB):
    """Canonical read shape — what API responses return."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# audit_logs  (T06 / #7)
# ---------------------------------------------------------------------------


class AuditLogBase(BaseModel):
    """Fields shared between create / read shapes for `audit_logs`.

    Per ADR-0002 every Tool invocation must be auditable, and per
    ADR-0028 the row carries its own retention metadata so the
    hot/cold split (and future recall) can be enforced without a
    second collection.

    `actor_id` is the user who triggered the call. `tool_snapshot`
    freezes the Tool definition that was actually invoked (ADR-0027)
    so the log row answers "what version of the Tool did this run?"
    without joining `tools` (which may have moved on).
    """

    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(
        description="ObjectId of `users._id` who triggered the call. Indexed.",
    )
    conversation_id: str = Field(
        description="ObjectId of `conversations._id`. Indexed.",
    )
    turn_id: str = Field(
        description="ObjectId of `turns._id`. Indexed.",
    )
    plan_id: str = Field(
        description="ObjectId of `plans._id`. Indexed.",
    )
    plan_execution_id: str | None = Field(
        default=None,
        description=(
            "ObjectId of `plan_executions._id`. Indexed. `None` for "
            "audit rows that don't belong to a single execution — "
            "Plan-edit events (T26 / ADR-0019) record the diff here "
            "in `response` and leave `plan_execution_id` empty until "
            "the (post-edit) Worker lands one."
        ),
    )
    tool_name: str = Field(
        description="Slug of the Tool that was invoked. Indexed for filter UIs.",
    )
    tool_snapshot: ToolSnapshot = Field(
        description="Frozen Tool definition per ADR-0027 — what the Worker actually saw.",
    )
    parameters: dict[str, Any] = Field(
        description="Resolved Tool arguments. Persisted in full (with PII redaction at the seam).",
    )
    response: dict[str, Any] | None = Field(
        default=None,
        description="Upstream API response (success or error body). None until terminal.",
    )
    status: Literal["running", "succeeded", "failed", "skipped"] = Field(
        description="Per-call outcome — `running` is brief; admins see terminal only.",
    )
    error: dict[str, Any] | None = Field(
        default=None,
        description="Structured error envelope on failure. None on success.",
    )
    risk_level: ToolRiskLevel = Field(
        description="Tool's risk tier at call time — drives HITL re-confirm display in audit UI.",
    )
    retry_count: int = Field(default=0, ge=0)


class AuditLogCreate(AuditLogBase):
    """Input shape for creating an audit log entry.

    Repository stamps `occurred_at` and `retention` on insert; the
    caller never supplies them. Retention metadata follows the
    default 1-year hot + 3-year cold schedule (ADR-0028) and the
    repair job bumps `cold_archived_at` as the row crosses the
    boundary.
    """


class AuditLogInDB(AuditLogBase):
    """Persisted shape of an `audit_logs` document.

    Names the lifecycle column `lifecycle_status` rather than
    `status` because `status` is already the per-call outcome
    (succeeded / failed / running / skipped). Renaming on the wire
    keeps the canonical `AuditLog` shape readable — both sides carry
    the same field name, so the simpler `InDB → Canonical` mapping
    is a no-op transform.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    occurred_at: datetime
    lifecycle_status: AuditLogStatus = Field(
        default="active",
        description="ADR-0028 retention lifecycle — `active` / `archived` / `recalled`.",
    )
    cold_storage_ref: str | None = None
    cold_archived_at: datetime | None = None


class AuditLog(AuditLogInDB):
    """Canonical read shape — what API responses return.

    Inherits every persisted field directly (mirrors
    `Tool(ToolInDB)`). Lifecycle metadata is surfaced so the admin
    audit UI can render "this row expires in 364 days" without
    joining a second collection (ADR-0028).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

