"""Liveness and readiness.

Two levels, because they answer different questions. ``GET /health`` is a liveness
probe: cheap, and it must not touch the database, or a brief lock contention gets
the container killed and restarted into the same contention.
``GET /health?deep=true`` is readiness — it actually reads a row — and that is what
a compose healthcheck or a load balancer should gate traffic on.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Response, status

from app.config import get_settings
from app.db import get_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _count_configs() -> int:
    store = get_store()
    with store._lock:  # noqa: SLF001 — the readiness probe is the one legitimate peek
        return store._conn.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0]


@router.get("/health")
async def health(
    response: Response,
    deep: bool = Query(default=False, description="Also check the database."),
) -> dict[str, str | bool | int]:
    """Report service health. Shallow by default."""
    settings = get_settings()
    body: dict[str, str | bool | int] = {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.version,
    }
    if not deep:
        return body

    try:
        # A real query, not PRAGMA: it proves the schema applied and the file is
        # readable, which "the connection object exists" does not.
        body["agents"] = await asyncio.to_thread(_count_configs)
        body["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — any storage error means "not ready"
        logger.warning("deep health check failed: %s", exc)
        body["status"] = "degraded"
        body["database"] = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body
