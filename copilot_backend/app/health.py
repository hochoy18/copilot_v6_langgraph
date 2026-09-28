"""Liveness and readiness probes for backend dependencies.

The three dependencies declared in T03 (#4) — MongoDB, Milvus, and Langfuse
— are reached via *protocol-level* pings, not full SDK round-trips:

* MongoDB / Milvus: open a TCP connection under the configured timeout.
  The gRPC / wire SDKs land in later tickets (T04 / T05 / T31+); TCP
  reachability is the canonical "process is up" signal at scaffold time.
* Langfuse: an HTTP GET against `/api/public/health` on the configured host.
  The Langfuse SDK wires in T35 (#40).

The module exposes one checker per dependency plus a `HealthChecker`
aggregator that the FastAPI lifespan runs on startup (logging only — never
raising, so a degraded boot still serves `/healthz` as 503) and the
`/healthz` route calls per request.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping

import httpx
from pydantic import BaseModel

from app.settings import Settings

logger = logging.getLogger(__name__)


# --- Constants & type aliases --------------------------------------------

# Single source of truth for dependency order. Adding a fourth
# dependency is a one-line change here, plus a probe + an entry in
# DEFAULT_PROBES.
DEPENDENCIES: tuple[str, ...] = ("mongodb", "milvus", "langfuse")

# Mypy-only narrowing for callers that want a closed set; the public
# surface still takes `str` so tests can pass arbitrary keys.
DependencyName = str  # see DEPENDENCIES for the canonical enum
HealthStatus = str  # see HEALTHY / UNHEALTHY

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"

# A probe is an async function taking settings and returning a fully
# populated `DependencyStatus`. Tests swap entries via
# `HealthChecker(checks=...)` so unit tests don't touch real services.
Probe = Callable[[Settings], Awaitable["DependencyStatus"]]


# --- Wire model -----------------------------------------------------------


class DependencyStatus(BaseModel):
    """Wire shape of a single dependency's health entry in `/healthz`.

    Stays a flat model — T04+ tickets will fold SDK-level state into the
    `detail` field without changing the JSON contract.
    """

    name: str
    status: str  # HEALTHY | UNHEALTHY
    latency_ms: float
    detail: str
    error: str | None = None


# --- Probes ---------------------------------------------------------------


async def check_mongodb(settings: Settings) -> DependencyStatus:
    """TCP-reachability probe for MongoDB.

    Healthy when a TCP connect to the URI's host:port completes within
    `health_check_timeout_seconds`. Unhealthy on timeout, connection
    refused, DNS failure, or a URI we cannot parse.
    """
    host, port = parse_mongo_uri(settings.mongodb_uri)
    return await _tcp_probe(
        name="mongodb",
        host=host,
        port=port,
        seconds=settings.health_check_timeout_seconds,
    )


async def check_milvus(settings: Settings) -> DependencyStatus:
    """TCP-reachability probe for Milvus.

    Milvus speaks gRPC on the configured port. Until T31 ships the SDK we
    only verify the port is accepting connections.
    """
    return await _tcp_probe(
        name="milvus",
        host=settings.milvus_host,
        port=settings.milvus_port,
        seconds=settings.health_check_timeout_seconds,
    )


async def check_langfuse(settings: Settings) -> DependencyStatus:
    """HTTP-reachability probe for Langfuse.

    Hits `<langfuse_host>/api/public/health` which returns 200 when the
    Langfuse web service is up. The Langfuse SDK (T35) reads the same URL
    plus `/api/public/ingestion` and `/api/public/traces`.
    """
    host = settings.langfuse_host.rstrip("/")
    url = f"{host}/api/public/health"
    timeout = httpx.Timeout(settings.health_check_timeout_seconds)
    start = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                return _status(
                    name="langfuse",
                    start=start,
                    status=UNHEALTHY,
                    detail="http:error",
                    error=f"{type(exc).__name__}: {exc}".strip(),
                )
    except httpx.HTTPError as exc:
        # Defensive: `httpx.AsyncClient()` itself can raise on bad URL.
        return _status(
            name="langfuse",
            start=start,
            status=UNHEALTHY,
            detail="http:error",
            error=f"{type(exc).__name__}: {exc}".strip(),
        )

    if 200 <= response.status_code < 300:
        return _status(
            name="langfuse",
            start=start,
            status=HEALTHY,
            detail=f"http:{response.status_code} /api/public/health",
        )
    return _status(
        name="langfuse",
        start=start,
        status=UNHEALTHY,
        detail=f"http:{response.status_code}",
        error=f"status={response.status_code}",
    )


# --- Aggregator -----------------------------------------------------------

DEFAULT_PROBES: Mapping[str, Probe] = {
    "mongodb": check_mongodb,
    "milvus": check_milvus,
    "langfuse": check_langfuse,
}


class HealthChecker:
    """Run all dependency probes and decide overall liveness.

    Order: MongoDB → Milvus → Langfuse. Probe results are returned in this
    stable order so the wire shape is deterministic.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        checks: Mapping[str, Probe] | None = None,
    ) -> None:
        self._settings = settings
        self._checks: Mapping[str, Probe] = checks or DEFAULT_PROBES

    async def run_all(self) -> tuple[list[DependencyStatus], bool]:
        """Run every probe in parallel; return `(results, all_healthy)`.

        Probes are individually shielded: any exception (e.g. a bad
        `mongodb_uri` raising `ValueError` from `parse_mongo_uri`) is
        captured as `unhealthy` with `detail=probe_raised`. The
        aggregator never lets a single dependency bring the whole
        endpoint down.
        """
        coros = [_shield(self._checks[name], name, self._settings) for name in DEPENDENCIES]
        results = await asyncio.gather(*coros)
        all_healthy = all(r.status == HEALTHY for r in results)
        return list(results), all_healthy


# --- Helpers --------------------------------------------------------------


async def _tcp_probe(*, name: str, host: str, port: int, seconds: float) -> DependencyStatus:
    """Open a TCP connection under `seconds`; classify the result.

    Shared by MongoDB / Milvus because both reduce to the same probe
    shape — only how `host` / `port` are obtained differs. Returning a
    fully-built `DependencyStatus` keeps the public probes small.
    """
    start = time.monotonic()
    try:
        fut = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=seconds)
    except TimeoutError:
        return _status(
            name=name,
            start=start,
            status=UNHEALTHY,
            detail="tcp:timeout",
            error="timeout",
        )
    except OSError as exc:
        return _status(
            name=name,
            start=start,
            status=UNHEALTHY,
            detail=f"tcp:{_os_error_label(exc)}",
            error=str(exc) or "os_error",
        )

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    return _status(
        name=name,
        start=start,
        status=HEALTHY,
        detail=f"tcp:{host}:{port}",
    )


def _status(
    *,
    name: str,
    start: float,
    status: str,
    detail: str,
    error: str | None = None,
) -> DependencyStatus:
    """Stamp `latency_ms` and emit a `DependencyStatus` with a typed tuple.

    Centralising the construction lets `check_mongodb` / `check_milvus` /
    `check_langfuse` each read as a flat sequence of return paths.
    """
    return DependencyStatus(
        name=name,
        status=status,
        latency_ms=_now_ms(start),
        detail=detail,
        error=error,
    )


def _now_ms(start: float) -> float:
    """Return milliseconds elapsed since `start` (monotonic clock)."""
    return round((time.monotonic() - start) * 1000.0, 2)


async def _shield(probe: Probe, name: str, settings: Settings) -> DependencyStatus:
    """Run a probe and turn any escaping exception into `unhealthy`.

    `HealthChecker.run_all` calls probes concurrently via `gather`; a
    single bad config (e.g. `parse_mongo_uri` raising `ValueError`) must
    not cascade into a 500 on `/healthz`. This wrapper keeps the
    invariants stated in the aggregator docstring.
    """
    start = time.monotonic()
    try:
        return await probe(settings)
    except Exception as exc:  # noqa: BLE001 — health must never propagate
        return DependencyStatus(
            name=name,
            status=UNHEALTHY,
            latency_ms=_now_ms(start),
            detail="probe_raised",
            error=f"{type(exc).__name__}: {exc}".strip(),
        )


def _os_error_label(exc: OSError) -> str:
    """Reduce an OSError to a short, stable label for the `detail` field."""
    errname: str | None = getattr(exc, "strerror", None)
    if errname:
        return errname.lower().replace(" ", "_")
    errno: int | None = getattr(exc, "errno", None)
    if errno is not None:
        return f"errno_{errno}"
    return "os_error"


def parse_mongo_uri(uri: str) -> tuple[str, int]:
    """Pull `host:port` out of an `mongodb://host:port` URI.

    Supports the common MVP single-node form (`mongodb://` and
    `mongodb+srv://`). Raises `ValueError` on an unrecognized scheme so
    a misconfigured deployment surfaces in `/healthz` rather than
    silently probing `localhost`.
    """
    match = re.match(r"^mongodb(?:\+srv)?://([^/:?]+)(?::(\d+))?", uri)
    if match is None:
        raise ValueError(f"unsupported mongodb_uri scheme: {uri!r}")
    host = match.group(1) or "127.0.0.1"
    port = int(match.group(2)) if match.group(2) else 27017
    return host, port
