"""FastAPI application factory.

`app = create_app()` is the import target for `uvicorn main:app`.

The lifespan below probes MongoDB / Milvus / Langfuse on startup. We log
the per-dependency results but **never raise** — a degraded boot is
allowed so that, e.g., a flaky Langfuse shouldn't take down the API.
The corresponding `/healthz` will then report 503 until the dependency
recovers, which is the contract deploy probes expect (ADR-0002 layers the
availability story on top of this).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.db.mongo import MongoClient
from app.exceptions import register_exception_handlers
from app.health import HealthChecker
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def _probe_dependencies(settings: Settings) -> None:
    """Run every dependency probe once on startup and log the result.

    Errors are logged, not raised. The FastAPI process should boot even if
    a probe fails — the readiness gate is served via `/healthz` returning
    503, which K8s probes handle natively.
    """
    checker = HealthChecker(settings)
    results, all_healthy = await checker.run_all()
    for r in results:
        line = (
            f"startup health {r.name}={r.status} "
            f"latency={r.latency_ms}ms detail={r.detail!r}"
        )
        if r.status == "healthy":
            logger.info(line)
        else:
            logger.warning("%s error=%r", line, r.error)
    if all_healthy:
        logger.info("startup health: all dependencies healthy")
    else:
        logger.warning("startup health: backend booting in degraded mode")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Standard FastAPI lifespan: open Mongo, probe deps, log on shutdown.

    T04 (#5) opens one `MongoClient` per process here. Closing it on
    shutdown is critical — Motor's client owns a connection pool plus a
    background monitoring task; letting them leak until GC leaves the
    process holding sockets open across the lifespan window.
    """
    settings = app.state.settings
    mongo = MongoClient(settings)
    app.state.mongo = mongo
    app.state.database = mongo.database
    try:
        await _probe_dependencies(settings)
        yield
    finally:
        await mongo.close()
        logger.info("backend shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a configured FastAPI instance.

    Tests pass an explicit `settings`; production calls `create_app()` and
    reads from env.
    """
    cfg = settings or get_settings()

    app = FastAPI(
        title="Copilot Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Stash settings on app.state so lifespan (and any future SDK client
    # initialisation in T04/T31/T35) can reach the same instance the
    # dependency-injected routes see.
    app.state.settings = cfg

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
