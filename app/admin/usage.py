"""What Bloom has spent, behind the admin key.

`agent-mcp-py` already mounts `GET /agent/usage`, and it is not this. That endpoint is
authenticated with ``BLOOM_MCP_KEYS`` — the key a *peer agent* holds — and handing that
key to Aperture so a GUI could draw a cost chart would undo the separation the two key
sets exist for: one leaked token would then buy both reading the ledger and spending
against it. So the same numbers are served again here, behind ``BLOOM_ADMIN_KEYS``.

Three sources, because no one of them can answer the question alone:

* **`agent_runtime_usage`**, via `CostTracker.summary()` — model spend, per model.
* **`agent_mcp_usage`**, via the MCP server's usage sink — tool and resource calls,
  per tool and per caller. Only present when the MCP server is mounted; a Bloom with
  ``BLOOM_FEATURE_MCP=false`` has no such table and says so rather than reporting zero,
  which would read as "nothing has called me" instead of "nobody could".
* **`runs`** — how many tasks were delegated and how many failed. Neither library knows
  what a run *is*, so neither can answer that.

**One caveat travels with every number here.** When a stop condition fires,
`agent_runtime` makes one further completion that appends no `Step`; its tokens reach
neither `RunResult.total_cost` nor `agent_runtime_usage`. So a run that hit its step or
cost ceiling under-reports by exactly one model call, and the honest presentation of
these totals is "at least". That is upstream behaviour, surfaced rather than hidden.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query

from app.admin.deps import require_admin
from app.config import get_settings
from app.db import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["usage"], dependencies=[Depends(require_admin)])


def _model_spend(since: str | None) -> dict:
    """Model spend from `agent_runtime`'s own tracker, reading Bloom's database."""
    from agent_runtime.cost_tracker import CostTracker

    settings = get_settings()
    # A short-lived tracker rather than `get_tracker()`: that singleton is built from
    # agent_runtime's *own* settings, which Bloom never populates — it would open
    # ./agent_runtime.db and report an empty ledger that looks like a free month.
    tracker = CostTracker(path=settings.db_path, app_name=settings.app_name)
    try:
        return tracker.summary(since)
    finally:
        tracker.close()


def _tool_calls(since: str | None) -> dict | None:
    """Tool-call counts from the MCP layer's sink, or None when MCP is not mounted."""
    settings = get_settings()
    if not settings.mcp_enabled:
        return None
    from agent_mcp.usage_log import SQLiteUsageSink

    sink = SQLiteUsageSink(settings.db_path)
    try:
        return sink.summary(since=since)
    finally:
        sink.close()


@router.get("/usage")
async def usage(
    since: str | None = Query(
        default=None,
        description="ISO-8601 timestamp. Omit for all time.",
    ),
) -> dict:
    """Spend, tool calls and run counts in one document.

    Every read is a separate SQLite connection over the same file, so they run in a
    worker thread together rather than three times over — see `app/db.py` on why the
    busy timeout matters with this many writers and readers on one database.
    """

    def _gather() -> dict:
        return {
            "since": since,
            "runs": get_store().runs_rollup(since),
            "models": _model_spend(since),
            "tools": _tool_calls(since),
            # Stated in the payload, not only in the docs: a client that renders a
            # total has to decide how to label it, and this is the answer.
            "caveat": (
                "A run stopped by a step or cost ceiling makes one further model call "
                "that the runtime does not report, so model spend is a floor, not a "
                "total."
            ),
        }

    return await asyncio.to_thread(_gather)
