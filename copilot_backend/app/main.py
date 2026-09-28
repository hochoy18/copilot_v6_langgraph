"""FastAPI application factory.

`app = create_app()` is the import target for `uvicorn main:app`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.exceptions import register_exception_handlers
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a configured FastAPI instance.

    Tests pass an explicit `settings`; production calls `create_app()` and
    reads from env.
    """
    cfg = settings or get_settings()

    app = FastAPI(
        title="Copilot Backend",
        version="0.1.0",
    )

    # CORS middleware: SPEC defers policy to V1.1, but the scaffold ticket
    # requires the seam to be wired. Origins are env-driven.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(health_router)

    # Unified error contract (ADR-0031).
    register_exception_handlers(app)

    return app


# Module-level instance for `uvicorn main:app`.
app: FastAPI = create_app()