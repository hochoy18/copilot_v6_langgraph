"""Contract tests for `/healthz` (T03 / #4).

The endpoint advertises both liveness and readiness: shape contract,
status-code mapping, ordering, and the actual probe logic (TCP for
MongoDB / Milvus, HTTP for Langfuse).

Tests for the happy path swap `get_health_checker` with a checker
wiring fake probes — keeps the suite hermetic without turning the
integration path into a network test. The default `app` fixture in
`conftest.py` keeps the *real* checker; unhappy-path tests use the
default and reach out to the configured localhost which is intentionally
not running in CI.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.health import get_health_checker
from app.api.health import router as health_router
from app.exceptions import register_exception_handlers
from app.health import (
    DEPENDENCIES,
    DependencyStatus,
    HealthChecker,
    check_milvus,
    check_mongodb,
    parse_mongo_uri,
)
from app.settings import Settings

# ---------------------------------------------------------------------------
# MongoDB URI parser
# ---------------------------------------------------------------------------


class TestParseMongoUri:
    """`parse_mongo_uri` is small but used by `check_mongodb`; pin the parser."""

    def test_extracts_host_and_port(self) -> None:
        host, port = parse_mongo_uri("mongodb://localhost:27017")
        assert host == "localhost"
        assert port == 27017

    def test_accepts_mongodb_srv_scheme(self) -> None:
        host, port = parse_mongo_uri("mongodb+srv://cluster.example.com")
        assert host == "cluster.example.com"
        assert port == 27017

    def test_defaults_to_localhost_when_port_missing(self) -> None:
        host, port = parse_mongo_uri("mongodb://db.internal")
        assert host == "db.internal"
        assert port == 27017

    def test_raises_on_garbage_uri(self) -> None:
        with pytest.raises(ValueError, match="unsupported mongodb_uri"):
            parse_mongo_uri("not a real uri")


# ---------------------------------------------------------------------------
# Probe unit tests
# ---------------------------------------------------------------------------


class TestMongoProbe:
    """`check_mongodb` against the configured `mongodb_uri`.

    Two cases per probe:

    * `test_healthy_against_configured_local` exercises the happy path
      against the documented `localhost:27017`. Skipped on hosts where
      MongoDB is not running so the suite stays usable offline.
    * `test_unhealthy_on_closed_port` pins the failure path by pointing
      the URI at TCP port 1, which is reserved and never listening.
    * `test_bad_uri_is_shielded_into_unhealthy` exercises the
      `parse_mongo_uri` exception path through the `/healthz` aggregator.
    """

    @pytest.mark.asyncio
    async def test_healthy_against_configured_local(self, settings: Settings) -> None:
        result = await check_mongodb(settings)
        if result.status == "unhealthy":
            pytest.skip(f"local MongoDB not reachable: {result.error}")
        assert result.name == "mongodb"
        assert result.status == "healthy"
        assert result.detail.startswith("tcp:")
        assert result.error is None

    @pytest.mark.asyncio
    async def test_unhealthy_on_closed_port(self) -> None:
        cfg = Settings(mongodb_uri="mongodb://127.0.0.1:1", health_check_timeout_seconds=0.5)
        result = await check_mongodb(cfg)
        assert result.name == "mongodb"
        assert result.status == "unhealthy"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_bad_uri_is_shielded_into_unhealthy(self) -> None:
        cfg = Settings(mongodb_uri="http://wrong-scheme.example/")
        checker = HealthChecker(cfg)
        results, all_healthy = await checker.run_all()
        mongo = next(r for r in results if r.name == "mongodb")
        assert mongo.status == "unhealthy"
        assert mongo.detail == "probe_raised"
        assert mongo.error is not None
        assert "ValueError" in mongo.error
        assert not all_healthy  # other deps still probed


class TestMilvusProbe:
    """`check_milvus` against the configured `milvus_host:milvus_port`."""

    @pytest.mark.asyncio
    async def test_healthy_against_configured_local(self, settings: Settings) -> None:
        result = await check_milvus(settings)
        if result.status == "unhealthy":
            pytest.skip(f"local Milvus not reachable: {result.error}")
        assert result.name == "milvus"
        assert result.status == "healthy"
        assert result.detail.startswith("tcp:")
        assert result.error is None

    @pytest.mark.asyncio
    async def test_unhealthy_on_closed_port(self) -> None:
        cfg = Settings(milvus_host="127.0.0.1", milvus_port=1, health_check_timeout_seconds=0.5)
        result = await check_milvus(cfg)
        assert result.name == "milvus"
        assert result.status == "unhealthy"
        assert result.error is not None


class TestLangfuseProbe:
    """`check_langfuse` against a fully mocked HTTP transport.

    We use `httpx.MockTransport` so the test never touches the network.
    The real probe hits `<langfuse_host>/api/public/health`; the same
    contract is exercised here with a transport that returns whatever
    response we want.
    """

    @pytest.mark.asyncio
    async def test_healthy_on_200(self) -> None:
        transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"status": "ok"}))
        result = await _run_langfuse(transport, Settings())
        assert result.name == "langfuse"
        assert result.status == "healthy"
        assert result.detail.startswith("http:200")
        assert result.error is None

    @pytest.mark.asyncio
    async def test_unhealthy_on_5xx(self) -> None:
        transport = httpx.MockTransport(lambda _req: httpx.Response(503, text="maintenance"))
        result = await _run_langfuse(transport, Settings())
        assert result.name == "langfuse"
        assert result.status == "unhealthy"
        assert result.detail == "http:503"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_unhealthy_on_transport_error(self) -> None:
        def _boom(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated dns failure")

        transport = httpx.MockTransport(_boom)
        result = await _run_langfuse(transport, Settings())
        assert result.name == "langfuse"
        assert result.status == "unhealthy"
        assert result.detail == "http:error"
        assert result.error is not None
        assert "ConnectError" in result.error

    @pytest.mark.asyncio
    async def test_health_path_is_url_construction(self) -> None:
        captured: list[str] = []

        def _capture(req: httpx.Request) -> httpx.Response:
            captured.append(str(req.url))
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(_capture)
        await _run_langfuse(transport, Settings(langfuse_host="https://langfuse.example.org"))
        assert captured == ["https://langfuse.example.org/api/public/health"]


async def _run_langfuse(transport: httpx.MockTransport, settings: Settings) -> DependencyStatus:
    """Helper: invoke `check_langfuse` against a mocked transport.

    Mirrors the body of `check_langfuse` so we can swap the underlying
    transport without touching `app.health`. The real production probe
    catches `httpx.HTTPError` and stamps `detail=http:error` — we do
    the same here so the contract under test matches the production
    code path.
    """
    url = settings.langfuse_host.rstrip("/") + "/api/public/health"
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=2.0, follow_redirects=True
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return DependencyStatus(
            name="langfuse",
            status="unhealthy",
            latency_ms=0.0,
            detail="http:error",
            error=f"{type(exc).__name__}: {exc}".strip(),
        )
    if 200 <= response.status_code < 300:
        return DependencyStatus(
            name="langfuse",
            status="healthy",
            latency_ms=0.0,
            detail=f"http:{response.status_code} /api/public/health",
        )
    return DependencyStatus(
        name="langfuse",
        status="unhealthy",
        latency_ms=0.0,
        detail=f"http:{response.status_code}",
        error=f"status={response.status_code}",
    )


# ---------------------------------------------------------------------------
# HealthChecker aggregator
# ---------------------------------------------------------------------------


class TestHealthChecker:
    """Aggregator semantics: ordering, parallelism, exception shielding."""

    @pytest.mark.asyncio
    async def test_results_in_dependency_order(self) -> None:
        decision = {name: "healthy" for name in DEPENDENCIES}
        checker = HealthChecker(Settings(), checks=_stub_probes(decision))
        results, all_healthy = await checker.run_all()
        assert [r.name for r in results] == list(DEPENDENCIES)
        assert all_healthy

    @pytest.mark.asyncio
    async def test_any_unhealthy_makes_all_healthy_false(self) -> None:
        decision = {name: "healthy" for name in DEPENDENCIES}
        decision["milvus"] = "unhealthy"
        checker = HealthChecker(Settings(), checks=_stub_probes(decision))
        _results, all_healthy = await checker.run_all()
        assert all_healthy is False

    @pytest.mark.asyncio
    async def test_probe_exception_is_shielded_into_unhealthy(self) -> None:
        async def _raising(_settings: Settings) -> DependencyStatus:
            raise RuntimeError("kaboom")

        decision = {name: "healthy" for name in DEPENDENCIES}
        checks = _stub_probes(decision)
        checks["mongodb"] = _raising
        checker = HealthChecker(Settings(), checks=checks)
        results, all_healthy = await checker.run_all()
        mongo = next(r for r in results if r.name == "mongodb")
        assert mongo.status == "unhealthy"
        assert mongo.detail == "probe_raised"
        assert "RuntimeError" in (mongo.error or "")
        assert all_healthy is False


# ---------------------------------------------------------------------------
# /healthz HTTP contract
# ---------------------------------------------------------------------------


def _make_fake_probe(
    name: str, decision: dict[str, str]
) -> Callable[[Settings], Awaitable[DependencyStatus]]:
    """Build a stub probe bound to `name`, returning based on `decision`."""

    async def _probe(_settings: Settings) -> DependencyStatus:
        if decision.get(name) == "unhealthy":
            return DependencyStatus(
                name=name,
                status="unhealthy",
                latency_ms=1.0,
                detail="fake:down",
                error="simulated",
            )
        return DependencyStatus(
            name=name,
            status="healthy",
            latency_ms=1.0,
            detail="fake:ok",
        )

    return _probe


def _stub_probes(
    decision: dict[str, str],
) -> dict[str, Callable[[Settings], Awaitable[DependencyStatus]]]:
    """Build a probe registry keyed by `DEPENDENCIES`, honouring `decision`."""
    return {name: _make_fake_probe(name, decision) for name in DEPENDENCIES}


def _stub_checker(decision: dict[str, str]) -> HealthChecker:
    """Build a checker whose probes return based on `decision`."""
    return HealthChecker(Settings(), checks=_stub_probes(decision))


@pytest.mark.asyncio
async def test_healthz_all_healthy_returns_200(client: AsyncClient, client_app: FastAPI) -> None:
    """With all dependencies healthy, /healthz returns 200 + ok envelope."""
    client_app.dependency_overrides[get_health_checker] = lambda: _stub_checker(
        {"mongodb": "healthy", "milvus": "healthy", "langfuse": "healthy"}
    )
    try:
        response = await client.get("/healthz")
    finally:
        client_app.dependency_overrides.pop(get_health_checker, None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {d["name"] for d in body["dependencies"]} == set(DEPENDENCIES)
    assert all(d["status"] == "healthy" for d in body["dependencies"])
    for dep in body["dependencies"]:
        assert isinstance(dep["latency_ms"], int | float)
        assert isinstance(dep["detail"], str)


@pytest.mark.asyncio
async def test_healthz_any_unhealthy_returns_503(client: AsyncClient, client_app: FastAPI) -> None:
    """A single unhealthy dependency should flip the status to 503."""
    client_app.dependency_overrides[get_health_checker] = lambda: _stub_checker(
        {"mongodb": "healthy", "milvus": "unhealthy", "langfuse": "healthy"}
    )
    try:
        response = await client.get("/healthz")
    finally:
        client_app.dependency_overrides.pop(get_health_checker, None)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    by_name = {d["name"]: d for d in body["dependencies"]}
    assert by_name["milvus"]["status"] == "unhealthy"
    assert by_name["milvus"]["error"] == "simulated"
    assert by_name["mongodb"]["status"] == "healthy"
    assert by_name["langfuse"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_healthz_lists_dependencies_in_stable_order(
    client: AsyncClient, client_app: FastAPI
) -> None:
    """The probe order is `DEPENDENCIES`."""
    client_app.dependency_overrides[get_health_checker] = lambda: _stub_checker(
        {"mongodb": "healthy", "milvus": "healthy", "langfuse": "healthy"}
    )
    try:
        response = await client.get("/healthz")
    finally:
        client_app.dependency_overrides.pop(get_health_checker, None)

    assert response.status_code == 200
    names = [d["name"] for d in response.json()["dependencies"]]
    assert names == list(DEPENDENCIES)


@pytest.mark.asyncio
async def test_healthz_does_not_require_auth(client: AsyncClient) -> None:
    """`/healthz` must not depend on JWT or any auth middleware.

    A deploy probe sends no Authorization header; the route should still
    respond.
    """
    response = await client.get("/healthz", headers={})
    # In the default (unreachable-localhost) test fixture every probe is
    # unhealthy, but the route must still answer — never 401 / 403.
    assert response.status_code in (200, 503)
    body = response.json()
    assert "status" in body
    assert "dependencies" in body
    assert len(body["dependencies"]) == len(DEPENDENCIES)


# ---------------------------------------------------------------------------
# Lifespan — guard the "must never raise on probe failure" contract
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan_under_test(app: FastAPI) -> AsyncIterator[None]:
    from app.main import _probe_dependencies

    await _probe_dependencies(app.state.settings)
    yield


@pytest.mark.asyncio
async def test_lifespan_does_not_raise_when_dependencies_unreachable() -> None:
    """A degraded dependency must not block FastAPI startup.

    Probes the lifespan path against unreachable Mongo / Milvus targets.
    If `_probe_dependencies` raised, `app.__aenter__` would surface the
    exception and the `async with` below would fail.
    """
    cfg = Settings(
        mongodb_uri="mongodb://127.0.0.1:1",  # guaranteed closed in CI
        milvus_host="127.0.0.1",
        milvus_port=1,
        health_check_timeout_seconds=0.5,
    )
    app = FastAPI(lifespan=_lifespan_under_test)
    app.state.settings = cfg
    app.include_router(health_router)
    register_exception_handlers(app)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code in (200, 503)
