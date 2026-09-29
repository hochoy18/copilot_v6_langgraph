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

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.answer.generator import AnswerGenerator
from app.api.admin_tools import router as admin_tools_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.auth.login import build_state_store
from app.auth.oidc import OIDCAdapter
from app.db.mongo import MongoClient
from app.exceptions import register_exception_handlers
from app.health import HealthChecker
from app.llm.prompts import PromptProvider
from app.llm.provider import build_chat_model
from app.memory.plan_history import InMemoryMilvusWriter
from app.memory.recall import InMemoryMilvusReader
from app.planner.planner import ToolPlanner
from app.realtime.bus import SseEventBus
from app.realtime.stream import router as sse_router
from app.security.crypto import CredentialEncryptor
from app.security.keys import build_credential_encryptor
from app.security.logging_filter import install_credential_redaction_filter
from app.settings import Settings, get_settings
from app.tools.description_generator import ToolDescriptionGenerator

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
    """Standard FastAPI lifespan: open Mongo, build encryptor, probe deps, log on shutdown.

    T04 (#5) opens one `MongoClient` per process here. T05 (#6) adds
    the `CredentialEncryptor` instance on `app.state` so the
    `CredentialRepository` dependency can reach it. Closing Mongo on
    shutdown is critical — Motor's client owns a connection pool plus a
    background monitoring task; letting them leak until GC leaves the
    process holding sockets open across the lifespan window.
    """
    settings = app.state.settings
    mongo = MongoClient(settings)
    encryptor: CredentialEncryptor = build_credential_encryptor(settings)
    app.state.mongo = mongo
    app.state.database = mongo.database
    app.state.credential_encryptor = encryptor
    # T08 / #46 — OIDC adapter + state store on app.state so the
    # auth dependency seam can read them. Construction is cheap and
    # pure; the OIDC adapter's network calls are deferred to the
    # first `discovery()`.
    oidc_adapter = OIDCAdapter(settings)
    state_store = build_state_store(settings)
    app.state.oidc_adapter = oidc_adapter
    app.state.oidc_state_store = state_store
    # T16 / #14 — Prompt provider + description generator on app.state.
    # The provider owns the in-process Prompt cache (ADR-0013), so it
    # is per-process like the OIDC adapter; its httpx client is closed
    # on shutdown for the same socket-leak reason. The chat model is
    # built lazily inside the generator (`build_chat_model(settings)`
    # on first use) — constructing it here would make an unconfigured
    # LLM a boot error, and degraded boot is deliberately allowed.
    # T18 / #16 — the provider is shared with the Planner (stashed on
    # app.state for `get_prompt_provider`), so it is a first-class
    # member of the lifespan rather than an implementation detail of
    # the description generator.
    prompt_http_client = httpx.AsyncClient()
    prompt_provider = PromptProvider(settings=settings, http_client=prompt_http_client)
    app.state.prompt_provider = prompt_provider
    app.state.description_generator = ToolDescriptionGenerator(
        settings=settings,
        prompt_provider=prompt_provider,
        chat_model_factory=lambda: build_chat_model(settings),
    )
    app.state.tool_planner = ToolPlanner(
        settings=settings,
        prompt_provider=prompt_provider,
        chat_model_factory=lambda: build_chat_model(settings),
        memory_window_k=settings.memory_window_k,
        memory_recall_top_n=settings.memory_recall_top_n,
    )
    # T22 / #19 — the final-answer LLM call. Lazy chat-model build for
    # the same reason as the description generator / Planner: an
    # unconfigured LLM is allowed at boot (degraded-mode /healthz
    # contract) so we never construct a ChatModel eagerly here.
    app.state.answer_generator = AnswerGenerator(
        settings=settings,
        prompt_provider=prompt_provider,
        chat_model_factory=lambda: build_chat_model(settings),
    )
    # T23 / #20 — process-wide SSE event bus. In-memory only; ADR-0010
    # permits this for the MVP. Stash on `app.state` so the SSE route's
    # dependency finds the same instance every request.
    app.state.sse_bus = SseEventBus()
    # T31 / #27 — long-term-memory Plan-history writer. Default
    # implementation is the in-memory recorder (tests + dev) so the
    # seam is exercisable without a Milvus SDK on the box. A real
    # `pymilvus`-backed writer is the deliberate follow-up (T32 or a
    # dedicated SDK ticket) — ADR-0008 explicitly permits a degraded
    # write path while the SDK lands: Milvus is the derived index,
    # MongoDB is the truth.
    milvus_plan_history_writer = InMemoryMilvusWriter()
    app.state.milvus_plan_history_writer = milvus_plan_history_writer
    # T32 / #28 — long-term-memory Plan-history reader. The reader
    # wraps the same in-memory store the writer pushes into, so a
    # test that runs the Executor (T31's seam) and then the Planner
    # (T32's seam) sees the records it just wrote. Production swaps
    # the writer / reader pair to a `pymilvus`-backed one — the
    # reader is constructed with the writer instance so the seam
    # carries one store across the two protocols. ADR-0008 explicitly
    # permits a degraded read path while the SDK lands: a missing
    # reader is best-effort, not a 5xx.
    app.state.milvus_plan_history_reader = InMemoryMilvusReader(
        milvus_plan_history_writer
    )
    try:
        await _probe_dependencies(settings)
        yield
    finally:
        await oidc_adapter.aclose()
        await prompt_http_client.aclose()
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
    app.include_router(auth_router)
    # T10 / #40 — conversation CRUD endpoints
    # (`POST /conversations`, `GET` list/detail, `POST /archive`).
    # Lives after `auth_router` so the OpenAPI tag order reads
    # auth → conversations; the load order has no runtime effect.
    app.include_router(conversations_router)
    # T23 / #20 — SSE stream endpoint (`GET /conversations/{id}/stream`).
    # Lives after `conversations_router` so the OpenAPI tag order reads
    # auth → conversations; load order has no runtime effect.
    app.include_router(sse_router)
    # T09 / #10 — `/admin/me` lives alongside the rest of the
    # auth-owned endpoints; `admin_router` (declared in `app.api.auth`)
    # owns the `admin` OpenAPI tag and groups the future admin-only
    # routes under one prefix.
    from app.api.auth import admin_router

    app.include_router(admin_router)
    # T12 / #11 — admin Tool CRUD (`POST /tools`, `GET /tools`,
    # `GET /tools/{id}`, `PATCH /tools/{id}`). Lives after `admin_router`
    # so the OpenAPI tag order reads auth → admin-tools; load order
    # has no runtime effect.
    app.include_router(admin_tools_router)

    # Unified error contract (ADR-0031).
    register_exception_handlers(app)

    # T33 / #29 — credential redaction logging filter. Installed on
    # the root logger so every `logger.info(...)` / `logger.exception(...)`
    # that ever receives a credential byte (via `extra=`, `exc_info`,
    # or accidental string-format of the Worker's `plaintext_payload`)
    # sees the bytes replaced with `[REDACTED]` before the formatter
    # writes to stdout / file / log-shipper sidecar. Idempotent — the
    # helper short-circuits when the filter is already attached so
    # test-suite app boots don't accumulate duplicate filters.
    install_credential_redaction_filter()

    return app


# Module-level instance for `uvicorn main:app`.
app: FastAPI = create_app()
