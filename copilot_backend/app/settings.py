"""Application settings, loaded from environment via pydantic-settings.

Kept intentionally minimal for the scaffold ticket. New env vars must land
here so configuration is centralised, not scattered across modules.
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

    # CORS: SPEC defers policy to V1.1 (SPA + FastAPI same-origin in prod),
    # but the issue acceptance criteria require the middleware be wired.
    # Default to common local-dev origins; override in deployments.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        description="Comma-separated origins allowed by CORS.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency: cached settings instance per process."""
    return Settings()