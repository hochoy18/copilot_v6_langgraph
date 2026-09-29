"""Application settings, loaded from environment via pydantic-settings.

All configuration for the Copilot backend must land here so values are
centralised, not scattered across modules. Env vars use the `COPILOT_`
prefix and are seeded from a local `.env` file when present.

Per-issue T03 (#4): connection strings for MongoDB / Milvus / Langfuse ship
with sensible localhost / remote URLs so a fresh checkout can boot against
the dependencies documented in `docs/SPEC.md` without further setup.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Copilot backend."""

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- CORS ------------------------------------------------------------
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        description=(
            "Comma-separated origins allowed by CORS. SPEC defers policy to V1.1; "
            "default covers the local Vite dev server."
        ),
    )

    # ---- MongoDB (T03 / T04+) -------------------------------------------
    # T03 only verifies reachability. T04 (#5) introduces Motor (the
    # async SDK) and the `copilot` database the four core collections
    # land in; later tickets (T05 / T39 / T42) reuse the same client.
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection URI. mongodb://host:port for a single node.",
    )
    mongodb_database: str = Field(
        default="copilot",
        description=(
            "Logical database name for the Copilot app. T04 seeds four "
            "collections (users / roles / refresh_tokens / tool_groups) "
            "here; later tickets reuse the same DB."
        ),
    )
    mongodb_server_selection_timeout_ms: int = Field(
        default=2000,
        ge=100,
        le=60_000,
        description=(
            "How long Motor waits for a server to become available before "
            "raising `ServerSelectionTimeoutError`. Mirrors "
            "`health_check_timeout_seconds` for the SDK path so the "
            "/healthz probe and a live `find_one` agree on the threshold."
        ),
    )

    # ---- Milvus (T03 / T31+) --------------------------------------------
    # T03 only verifies reachability via TCP. The gRPC SDK lands in T31 (#27).
    milvus_host: str = Field(
        default="localhost",
        description="Milvus gRPC host. Local dev uses the bundled instance.",
    )
    milvus_port: int = Field(
        default=19530,
        description="Milvus gRPC port. Default matches the official image.",
    )

    # ---- Langfuse (T03 / T35+) ------------------------------------------
    # T03 verifies reachability against the public health endpoint. SDK
    # wiring lands in T35 (#35). The URL must NOT include a trailing slash.
    langfuse_host: str = Field(
        default="https://langfuse.bananahochoy.online",
        description=(
            "Langfuse base URL. Used to derive the public health probe and "
            "(later) the OTel/SDK endpoints. No trailing slash."
        ),
    )

    # ---- Memory window (T30 / #26, ADR-0007) -----------------------------
    # How many recent user turns the Planner sees verbatim. SPEC
    # floors K=5; operators tighten to fit a smaller context budget
    # or widen when cross-turn references dominate. The minimum of 1
    # guards against a misconfiguration that would otherwise disable
    # the window entirely (the LLM would see only the current
    # instruction and lose all cross-turn continuity).
    memory_window_k: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "Number of recent user turns the Planner receives as "
            "verbatim context (ADR-0007 '记忆窗口'). Default 5 per "
            "SPEC; configure to widen/narrow the window."
        ),
    )

    # ---- Long-term memory recall (T32 / #28, ADR-0007) -------------------
    # How many historical Plan summaries the Planner receives alongside
    # the recent-K memory window. T32 (#28) asks the Milvus reader for
    # the Top-N most-similar `plan_history_vectors` rows for the current
    # instruction and renders them as the `{{long_term_memory}}` Prompt
    # slot. Default 3 per SPEC; operators widen when "上周那个"-style
    # cross-session references dominate and tighten when the budget is
    # tight. Minimum 1 — a value of 0 would silence recall entirely
    # and lose the cross-session continuity the seam exists to provide.
    memory_recall_top_n: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Top-N historical Plan summaries the Planner sees as "
            "long-term memory (T32 / ADR-0007 '长期记忆'). Default 3 "
            "per SPEC; configure to widen/narrow recall."
        ),
    )

    # ---- LLM Provider (T16 / #14, ADR-0014 / ADR-0016) -------------------
    # The LangChain ChatModel seam. Every LLM call in the backend is
    # made against an OpenAI-compatible endpoint built from these three
    # values (ADR-0014 — default provider is OpenAI-compatible, pointing
    # at OpenAI / DeepSeek / 豆包 / self-hosted vLLM alike).
    #
    # `llm_base_url` / `llm_api_key` default to empty: an deployment
    # without an LLM is fully supported — the description generator
    # (T16) degrades to raw OpenAPI text with an admin-visible warning
    # rather than failing the request. `build_chat_model` raises
    # `LLMConfigurationError` when an unconfigured backend is asked to
    # call a model.
    llm_base_url: str = Field(
        default="",
        description=(
            "OpenAI-compatible endpoint base URL, e.g. "
            "`https://api.openai.com/v1` or a vLLM/DeepSeek gateway. "
            "Empty means the LLM provider is not configured."
        ),
    )
    llm_api_key: str = Field(
        default="",
        description=(
            "Bearer key for `llm_base_url`. Empty means not configured. "
            "Confidential — set via env in prod, never committed."
        ),
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        min_length=1,
        max_length=128,
        description=(
            "Model name passed to the provider. MVP: every LLM call "
            "shares this one configuration (ADR-0014 §multi-model)."
        ),
    )
    llm_request_timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        description="Per-call timeout handed to the ChatModel.",
    )
    # ADR-0016: enterprise deployments default to the no-train path.
    # `llm_provider_supports_no_train` is the operator's declaration
    # that the chosen endpoint honours it (zero-retention agreement,
    # private deployment, 脱敏模式). If the two disagree the provider
    # factory refuses to build — silently training on enterprise API
    # metadata is the failure mode this guards against.
    llm_data_usage_opt_out: bool = Field(
        default=True,
        description=(
            "ADR-0016 opt-out flag. `true` (enterprise default) requires "
            "`llm_provider_supports_no_train=true` for any LLM call to proceed."
        ),
    )
    llm_provider_supports_no_train: bool = Field(
        default=True,
        description=(
            "Operator declaration that the endpoint at `llm_base_url` "
            "supports the no-train path. Set to `false` to opt back into "
            "training-enabled endpoints (requires `llm_data_usage_opt_out=false`)."
        ),
    )

    # ---- Langfuse prompts (T16 / #14, ADR-0013) --------------------------
    # Prompt templates are fetched from the Langfuse public API at
    # runtime (ADR-0013). The key pair below authenticates those reads;
    # when unset the provider falls back to the cached last-good copy and
    # then to the code-embedded bootstrap template (degradation ladder
    # documented in `app.llm.prompts`).
    langfuse_public_key: str = Field(
        default="",
        description="Langfuse public key (`pk-lf-…`). Empty = fetch off; cache/bootstrap only.",
    )
    langfuse_secret_key: str = Field(
        default="",
        description="Langfuse secret key (`sk-lf-…`). Confidential.",
    )
    langfuse_prompt_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        le=86_400,
        description=(
            "How long a freshly-fetched prompt stays fresh. Past the TTL "
            "the next fetch re-reads Langfuse; on failure the stale copy "
            "is still served (ADR-0013 degradation)."
        ),
    )
    langfuse_prompt_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Per-request timeout for prompt fetches.",
    )

    # ---- Health-check tuning --------------------------------------------
    health_check_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=10.0,
        description=(
            "Per-dependency probe timeout. T03 uses TCP / HTTP-level pings; "
            "the same value applies to all three dependencies."
        ),
    )

    # ---- Credential encryption (T05 / #6) -------------------------------
    # The symmetric key used to seal Tool credentials at rest (ADR-0002).
    # Production should set a base64-encoded 32-byte literal via
    # `COPILOT_CREDENTIAL_ENCRYPTION_KEY` — `.env.example` ships a
    # dev-only passphrase that `MasterKey.from_passphrase` derives.
    credential_encryption_key: str = Field(
        default="dev-only-do-not-use-in-prod",
        description=(
            "Either a base64-encoded 32-byte key (preferred for prod) "
            "or a human-readable passphrase (dev convenience). The "
            "encryption layer picks the right factory based on whether "
            "the value decodes to 32 bytes."
        ),
    )
    credential_encryption_salt: str = Field(
        default="copilot-dev-salt-001",
        min_length=16,
        description=(
            "PBKDF2 salt for passphrase-derived keys. UTF-8 encoded "
            "to bytes by `keys.build_credential_encryptor`; must be at "
            "least 16 bytes after encoding (per NIST SP 800-132). The "
            "`min_length=16` here guards against single-byte encodings "
            "falling under the floor. Fixed in dev so restarts decrypt "
            "existing rows."
        ),
    )

    # ---- OIDC SSO (T08 / #46) ------------------------------------------
    # `oidc_issuer_url` is the canonical identifier for the upstream IdP
    # (e.g. Okta / Azure AD / Auth0). The adapter pulls metadata from
    # `<issuer>/.well-known/openid-configuration` per the OIDC
    # discovery spec — `oidc_issuer` MUST match the `issuer` claim
    # returned by `id_token`s or verification will reject them.
    oidc_issuer_url: str = Field(
        default="https://idp.example.com",
        description=(
            "OIDC issuer URL. The adapter fetches "
            "`<issuer>/.well-known/openid-configuration` once and caches it."
        ),
    )
    oidc_client_id: str = Field(
        default="copilot-dev",
        description="OIDC client id issued by the IdP for this app.",
    )
    # Client secret is confidential; in dev a placeholder is fine because
    # the value is only consulted when the adapter hits a real IdP. The
    # IdP mock used in tests signs tokens with its own key and the
    # verification path doesn't read this field.
    oidc_client_secret: str = Field(
        default="dev-client-secret-not-for-prod",
        description="OIDC client secret. Confidential — set via env in prod.",
    )
    oidc_redirect_uri: str = Field(
        default="http://localhost:3000/auth/callback",
        description=(
            "Redirect URI registered with the IdP. The token-exchange "
            "call sends the same value so the IdP rejects any mismatch."
        ),
    )
    # Audience the access token is *for* (this system). ADR-0009 says
    # the access token is short-lived; the IdP-issued `id_token` has its
    # own audience — `oidc_audience` is the value we expect to see in
    # `id_token.aud` during verification.
    oidc_audience: str = Field(
        default="copilot-api",
        description=(
            "Expected `aud` claim of the IdP-issued `id_token`. The "
            "adapter rejects any token whose `aud` doesn't match."
        ),
    )
    # HS256 signing key for the access token we mint ourselves. Kept on
    # the server only; clients only see the encoded form. T09 will add
    # the verification middleware that consumes this same key.
    oidc_jwt_signing_key: str = Field(
        default="dev-internal-jwt-signing-key-not-for-prod",
        min_length=16,
        description=(
            "HMAC-SHA256 secret used to sign the short-lived access "
            "tokens we mint. T09 will read the same key to verify."
        ),
    )
    # IdP-side signing key. Distinct from `oidc_jwt_signing_key` (the
    # key WE use to sign access tokens). HS256 IdPs are uncommon in
    # production (most use RS256 with JWKS) but our test IdP mock and
    # some lightweight providers sign with HS256; this is the key
    # that the verifier uses against `id_token`. RS256 deployments
    # leave this empty and signature verification falls through to
    # the JWKS path (T35).
    oidc_id_token_signing_key: str = Field(
        default="dev-idp-hs256-key-not-for-prod",
        min_length=16,
        description=(
            "HMAC-SHA256 secret the IdP uses to sign `id_token`s. "
            "Only consulted when the IdP's `id_token` uses HS256; "
            "RS256 IdPs ignore this value and rely on JWKS."
        ),
    )
    oidc_jwt_issuer: str = Field(
        default="copilot-backend",
        description="`iss` claim for the access tokens we mint.",
    )
    oidc_jwt_audience: str = Field(
        default="copilot-api",
        description="`aud` claim for the access tokens we mint.",
    )
    oidc_access_token_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description=(
            "Access-token TTL per ADR-0009 (15 minutes default). The "
            "minimum guards against misconfiguration dropping the "
            "window below an interactive session; the maximum keeps "
            "blast radius bounded."
        ),
    )
    oidc_discovery_cache_seconds: int = Field(
        default=3600,
        ge=0,
        le=86_400,
        description=(
            "How long the adapter caches `/.well-known/openid-configuration`. "
            "0 disables caching (each login re-fetches)."
        ),
    )
    oidc_state_ttl_seconds: int = Field(
        default=600,
        ge=30,
        le=3600,
        description=(
            "How long the backend holds `state → (nonce, code_verifier)` "
            "between `GET /auth/sso/login` and `POST /auth/sso/callback`."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency: cached settings instance per process."""
    return Settings()
