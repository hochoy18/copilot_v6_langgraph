"""Index specifications for the four core collections.

Single source of truth for `init_db.py` — collections and indexes are
created in one pass. Two design rules govern the entries below:

1. **Lookups come first.** Every field the repository layer reads by
   has a unique or non-unique index. Without this, the user-lookup
   queries in T07 (login) and T08 (refresh) scan the full collection.
2. **Sparse + unique for "one-of" fields.** `sso_subject` and
   `local_username` are unique only when present (sparse=True). A row
   missing the field shouldn't collide with another row also missing
   it — that would let every SSO user claim the same "blank subject".

`refresh_tokens` carries a TTL index on `expires_at`: the persistence
layer purges expired rows itself, so the application code never has to
sweep a dead-token pile. T07 wires the rotation flow.
"""
from __future__ import annotations

from collections.abc import Iterable

from pymongo import ASCENDING, IndexModel

# ---------------------------------------------------------------------------
# Collection names — referenced from init_db.py AND repositories.
# ---------------------------------------------------------------------------

USERS = "users"
ROLES = "roles"
REFRESH_TOKENS = "refresh_tokens"
TOOL_GROUPS = "tool_groups"
TOOLS = "tools"
CREDENTIALS = "credentials"


CORE_COLLECTIONS: tuple[str, ...] = (
    USERS,
    ROLES,
    REFRESH_TOKENS,
    TOOL_GROUPS,
    TOOLS,
    CREDENTIALS,
)


# ---------------------------------------------------------------------------
# Index specs
# ---------------------------------------------------------------------------
#
# IndexModel is the canonical pymongo description. We pass them straight
# to `create_indexes(...)` which is idempotent — recreating an identical
# index is a no-op.

USER_INDEXES: list[IndexModel] = [
    # `email` is the login lookup for both SSO and local paths. Unique
    # so two users cannot share an email regardless of source.
    IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
    # `sso_subject` is unique only when present. The sparse flag lets
    # local users (no subject) coexist without colliding on null.
    IndexModel(
        [("sso_subject", ASCENDING)],
        unique=True,
        sparse=True,
        name="uniq_sso_subject_sparse",
    ),
    # Mirror index for local admin lookups.
    IndexModel(
        [("local_username", ASCENDING)],
        unique=True,
        sparse=True,
        name="uniq_local_username_sparse",
    ),
    # Role-grants lookup. Used by the auth middleware to resolve
    # `role_names` for an incoming JWT claim.
    IndexModel([("role_ids", ASCENDING)], name="by_role_ids"),
    # Active-user filter (login-disabled flags).
    IndexModel([("is_active", ASCENDING)], name="by_is_active"),
]

ROLE_INDEXES: list[IndexModel] = [
    # `name` is the slug surfaced in role-grants. Unique by design.
    IndexModel([("name", ASCENDING)], unique=True, name="uniq_name"),
    # Group-grants lookup (admin / audit).
    IndexModel([("tool_group_ids", ASCENDING)], name="by_tool_group_ids"),
]

REFRESH_TOKEN_INDEXES: list[IndexModel] = [
    # `token_hash` is the lookup on every refresh request. Unique so
    # rotation collisions are impossible.
    IndexModel([("token_hash", ASCENDING)], unique=True, name="uniq_token_hash"),
    # Per-user lookup: list a user's active sessions, force-logout.
    IndexModel(
        [("user_id", ASCENDING), ("revoked_at", ASCENDING)],
        name="by_user_revoked",
    ),
    # TTL: Mongo purges rows whose `expires_at` is in the past. The
    # 0-second `expireAfterSeconds` is the documented form for "use the
    # date field as the absolute expiry".
    IndexModel(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_expires_at",
    ),
]

TOOL_GROUP_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], unique=True, name="uniq_name"),
    IndexModel([("tool_ids", ASCENDING)], name="by_tool_ids"),
]


# `tools` (T05 / #6) — per ADR-0018 the runtime filter is
# `{status: "active"}` over the whole registry; an index on `status`
# alone lets the Planner load the active set in one shot. `risk_level`
# is a secondary filter for the admin dashboard and is paired with
# `status` so compound queries hit one index.
TOOL_INDEXES: list[IndexModel] = [
    # `name` is the LLM-facing slug. Unique so two Tools can't claim
    # the same identifier in a Plan.
    IndexModel([("name", ASCENDING)], unique=True, name="uniq_name"),
    # Primary runtime filter: "give me every active Tool".
    IndexModel([("status", ASCENDING)], name="by_status"),
    # Admin dashboard + audit drill-down by risk tier.
    IndexModel(
        [("status", ASCENDING), ("risk_level", ASCENDING)],
        name="by_status_risk_level",
    ),
    # FK lookup from the credential rotation flow: list Tools that
    # use a given credential row (ADR-0024).
    IndexModel([("credentials_ref", ASCENDING)], name="by_credentials_ref"),
]

# `credentials` (T05 / #6) — the admin labels credentials by `name`
# (`salesforce-prod`, etc.), so it gets a unique index. A `key_id`
# index supports the future multi-key rotation flow (find every
# ciphertext sealed under the deprecated key).
CREDENTIAL_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], unique=True, name="uniq_name"),
    IndexModel([("key_id", ASCENDING)], name="by_key_id"),
]


# ---------------------------------------------------------------------------
# Aggregator: lookup by collection name → index list.
# ---------------------------------------------------------------------------

INDEX_SPECS: dict[str, list[IndexModel]] = {
    USERS: USER_INDEXES,
    ROLES: ROLE_INDEXES,
    REFRESH_TOKENS: REFRESH_TOKEN_INDEXES,
    TOOL_GROUPS: TOOL_GROUP_INDEXES,
    TOOLS: TOOL_INDEXES,
    CREDENTIALS: CREDENTIAL_INDEXES,
}


def all_indexes() -> Iterable[tuple[str, list[IndexModel]]]:
    """Iterate `(collection_name, index_list)` pairs in stable order.

    Yields the four CORE_COLLECTIONS in their declared order so init
    output is deterministic and easy to diff.
    """
    for name in CORE_COLLECTIONS:
        yield name, INDEX_SPECS[name]
