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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency: cached settings instance per process."""
    return Settings()
