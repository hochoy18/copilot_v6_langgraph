"""Liveness endpoint.

`/healthz` is intentionally outside the `/api/v1` prefix (per SPEC: the
v1 surface is for JWT-protected business endpoints) and does not require
auth — it's used by deploy probes, not by the SPA.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}