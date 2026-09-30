"""Index specifications for the MongoDB collections.

Single source of truth for `init_db.py` — collections and indexes are
created in one pass. Three design rules govern the entries below:

1. **Lookups come first.** Every field the repository layer reads by
   has a unique or non-unique index. Without this, the user-lookup
   queries in T07 (login), the conversation list in T10 (CRUD), and
   the audit-log drill-downs in T43 scan the full collection.
2. **Sparse + unique for "one-of" fields.** `sso_subject` and
   `local_username` are unique only when present (sparse=True). A row
   missing the field shouldn't collide with another row also missing
   it — that would let every SSO user claim the same "blank subject".
3. **Compound for hot paths.** The conversation list view sorts by
   `user_id` then `last_activity_at`; turning that into one index
   keeps T39's "flip to idle" sweep an index scan rather than a
   collection scan + sort.

`refresh_tokens` carries a TTL index on `expires_at`: the persistence
layer purges expired rows itself, so the application code never has to
sweep a dead-token pile. T07 (#8) wires the rotation flow and adds a
non-unique `family_id` index so a reuse-detection event revokes every
token in the compromised chain with one query.
"""
from __future__ import annotations

from collections.abc import Iterable

from pymongo import ASCENDING, DESCENDING, IndexModel

# ---------------------------------------------------------------------------
# Collection names — referenced from init_db.py AND repositories.
# ---------------------------------------------------------------------------

USERS = "users"
ROLES = "roles"
REFRESH_TOKENS = "refresh_tokens"
TOOL_GROUPS = "tool_groups"
TOOLS = "tools"
CREDENTIALS = "credentials"
CONVERSATIONS = "conversations"
TURNS = "turns"
PLANS = "plans"
PLAN_EXECUTIONS = "plan_executions"
AUDIT_LOGS = "audit_logs"


CORE_COLLECTIONS: tuple[str, ...] = (
    USERS,
    ROLES,
    REFRESH_TOKENS,
    TOOL_GROUPS,
    TOOLS,
    CREDENTIALS,
    # T06 (#7) — conversation-domain collections.
    CONVERSATIONS,
    TURNS,
    PLANS,
    PLAN_EXECUTIONS,
    AUDIT_LOGS,
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
    # Per-family lookup: T07 (#8) reuse detection sweeps the rotation
    # chain in one query when a revoked token is presented again.
    IndexModel([("family_id", ASCENDING)], name="by_family_id"),
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


# `conversations` (T06 / #7) — the list view is
# `find({user_id}).sort({last_activity_at: -1})`; turning the hot
# path into one compound index keeps the Frontend "recent
# conversations" load off the full scan. `by_status` powers T39's
# sweep ("find every active conversation whose last activity is
# older than 15 min"). `by_user_status` covers the Frontend's
# active / idle / archived filter.
CONVERSATION_INDEXES: list[IndexModel] = [
    # Hot read: "my active conversations, newest first".
    IndexModel(
        [("user_id", ASCENDING), ("last_activity_at", DESCENDING)],
        name="by_user_last_activity",
    ),
    # T39 idle sweep: every active conversation sorted by activity.
    IndexModel(
        [("status", ASCENDING), ("last_activity_at", ASCENDING)],
        name="by_status_activity",
    ),
    # T39 archive sweep: every idle conversation sorted by
    # `idle_since`. The lifecycle service asks this list every
    # scan tick; turning it into an index scan keeps the
    # sweep O(candidates), not O(conversations).
    IndexModel(
        [("status", ASCENDING), ("idle_since", ASCENDING)],
        name="by_status_idle_since",
    ),
    # Frontend filter for a single conversation's status filter.
    IndexModel(
        [("user_id", ASCENDING), ("status", ASCENDING)],
        name="by_user_status",
    ),
]


# `turns` (T06 / #7) — the chat history is a per-conversation fetch
# in `created_at` order. `created_at` alone is fine for queries
# scoped to one conversation (Mongo sorts in-memory cheap at this
# scale), but the compound lets the archive flow
# (`conversation_id`, status) cleanly without re-scanning.
TURN_INDEXES: list[IndexModel] = [
    # Primary read: a conversation's turns in order.
    IndexModel(
        [("conversation_id", ASCENDING), ("created_at", ASCENDING)],
        name="by_conversation_created_at",
    ),
    # Lookup by Plan (cross-turn analysis, audit joins).
    IndexModel([("plan_id", ASCENDING)], name="by_plan_id"),
]


# `plans` (T06 / #7) — the chat UI fetches the latest plan for a
# conversation (`conversation_id`, `created_at` DESC). The audit
# drill-down (T42) looks up by `turn_id`. The lifecycle filter
# (`status: pending|approved|...`) drives the HITL pending queue.
PLAN_INDEXES: list[IndexModel] = [
    # Latest-plan-per-conversation lookup.
    IndexModel(
        [("conversation_id", ASCENDING), ("created_at", DESCENDING)],
        name="by_conversation_created_at",
    ),
    # Per-Turn lookup.
    IndexModel([("turn_id", ASCENDING)], name="by_turn_id"),
    # HITL pending queue + lifecycle dashboards.
    IndexModel([("status", ASCENDING)], name="by_status"),
]


# `plan_executions` (T06 / #7) — one row per Plan invocation; the
# read pattern is "give me the run for this Plan, newest first" —
# plans can re-execute (admin retry, agent-initiated re-run), so we
# sort by `started_at` DESC. `status` powers the "running now"
# dashboard.
PLAN_EXECUTION_INDEXES: list[IndexModel] = [
    # Per-Plan lookup of every execution attempt (audit / replay).
    IndexModel(
        [("plan_id", ASCENDING), ("started_at", DESCENDING)],
        name="by_plan_started_at",
    ),
    # Session-level rollup: executions per conversation.
    IndexModel([("conversation_id", ASCENDING)], name="by_conversation_id"),
    # "What's currently running" query.
    IndexModel([("status", ASCENDING)], name="by_status"),
]


# `audit_logs` (T06 / #7) — the read patterns are diverse
# (per-conversation, per-turn, per-plan, per-actor, per-tool, time
# range). For T06 we cover the four FK lookups + a time index
# (per-tenant "list my audit rows this month" scans); retention
# sweeps and recall land with T42 and add their own indexes here.
AUDIT_LOG_INDEXES: list[IndexModel] = [
    IndexModel([("conversation_id", ASCENDING)], name="by_conversation_id"),
    IndexModel([("turn_id", ASCENDING)], name="by_turn_id"),
    IndexModel([("plan_id", ASCENDING)], name="by_plan_id"),
    IndexModel([("actor_id", ASCENDING)], name="by_actor_id"),
    IndexModel([("tool_name", ASCENDING)], name="by_tool_name"),
    IndexModel([("occurred_at", DESCENDING)], name="by_occurred_at"),
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
    CONVERSATIONS: CONVERSATION_INDEXES,
    TURNS: TURN_INDEXES,
    PLANS: PLAN_INDEXES,
    PLAN_EXECUTIONS: PLAN_EXECUTION_INDEXES,
    AUDIT_LOGS: AUDIT_LOG_INDEXES,
}


def all_indexes() -> Iterable[tuple[str, list[IndexModel]]]:
    """Iterate `(collection_name, index_list)` pairs in stable order.

    Yields the CORE_COLLECTIONS in their declared order so init
    output is deterministic and easy to diff.
    """
    for name in CORE_COLLECTIONS:
        yield name, INDEX_SPECS[name]
