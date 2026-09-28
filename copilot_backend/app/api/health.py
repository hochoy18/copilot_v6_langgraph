"""Liveness and readiness endpoints.

`/healthz` lives outside the `/api/v1` namespace on purpose (per SPEC: the
v1 surface is for JWT-protected business endpoints). The route is public
so deploy probes and dev tools can hit it without credentials.

The wire shape follows ADR-0031's flat-envelope convention but adds a
per-dependency `dependencies` array so a single endpoint serves both
liveness ("the process is up") and readiness ("the dependencies it needs
are reachable").

Status code mapping:
    * 200 — all declared dependencies are healthy.
    * 503 — at least one dependency is unhealthy; the response body still
      carries the per-dependency breakdown so callers can diagnose.

`HealthChecker` is built via a FastAPI dependency (`get_health_checker`)
so tests can swap the registry of probes without touching the network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app.health import DependencyStatus, HealthChecker
from app.settings import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Envelope for `/healthz`. `dependencies` is ordered and deterministic."""

    status: str
    dependencies: list[DependencyStatus]


def get_health_checker(
    settings: Settings = Depends(get_settings),  # noqa: B008  (FastAPI idiom)
) -> HealthChecker:
    """FastAPI dependency: produce a `HealthChecker` for this request.

    Production callers get the real checker wired against the configured
    URLs; tests override this dependency to inject a `HealthChecker` with
    fake probes (see `tests/test_health.py`).
    """
    return HealthChecker(settings)


@router.get(
    "/healthz",
    response_model=HealthResponse,
    responses={
        503: {"model": HealthResponse, "description": "One or more dependencies are unhealthy."},
    },
)
async def healthz(
    response: Response,
    checker: HealthChecker = Depends(get_health_checker),  # noqa: B008  (FastAPI idiom)
) -> HealthResponse:
    """Report overall liveness and per-dependency reachability.

    On any unhealthy dependency the response status becomes 503 so K8s
    probes and load balancers can route around the pod without bespoke
    JSON parsing.
    """
    results, all_healthy = await checker.run_all()

    response.status_code = (
        status.HTTP_200_OK if all_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return HealthResponse(
        status="ok" if all_healthy else "degraded",
        dependencies=results,
    )
