"""Test runs, run history, and the live trace.

Three ways to look at the same rows:

* ``POST /admin/agents/{id}/test-run`` starts a run and answers immediately with
  its id — the run itself continues in the background.
* ``GET /admin/runs/{run_id}/events`` streams that run's trace as it happens.
* ``GET /admin/agents/{id}/runs/{run_id}/trace`` reads the finished article.

The last two return **the same event records** through two transports, so a client
writes one renderer and uses it for both a live panel and a history view.

**Why test-run is 202 and not a synchronous answer.** The spec called for a
synchronous call, and a synchronous call has no way to hand the caller a run id
before the run ends — which means no way to attach to the trace, which is the
entire value of a test run in a GUI. Answering with the id immediately and letting
the client stream is strictly more useful and only marginally more work: the
"synchronous" experience is the stream, and the final result is the last event on
it as well as a row in the history.

**Streaming is a cursor over SQLite, nudged in memory — not a per-subscriber
queue.** Every event carries its autoincrement id as the SSE ``id:`` field, so a
dropped connection resumes from ``Last-Event-ID`` with nothing lost. It costs one
indexed query per second per viewer and buys resumability, immunity to a slow
consumer, and correctness across more than one uvicorn worker, none of which an
in-memory fan-out gives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.admin.deps import require_admin
from app.config import get_settings
from app.db import get_store, new_id
from app.errors import ApiError
from app.runtime_service import cancel_run, execute_run
from app.trace import get_writer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["runs"], dependencies=[Depends(require_admin)])

# How long a reader waits on the in-memory nudge before polling anyway. The nudge
# is a latency optimisation, not the correctness mechanism — see the docstring —
# so a missed one costs at most this long.
_POLL_INTERVAL_S = 1.0
# A comment line keeps the connection alive through Caddy and any intermediary
# that closes an idle stream. 15s is comfortably inside the usual 30-60s timeouts.
_KEEPALIVE_S = 15.0

# Background test runs, held so the garbage collector cannot cancel one mid-flight:
# asyncio keeps only a weak reference to a task, and a run that vanishes halfway
# leaves a `running` row and a stream that never terminates.
_BACKGROUND: set[asyncio.Task] = set()


class TestRunIn(BaseModel):
    input: str = Field(min_length=1, description="The task to hand the agent.")


class TestRunOut(BaseModel):
    run_id: str
    status: str
    stream_url: str
    trace_url: str


def _require_config(config_id: str) -> dict:
    config = get_store().get_config(config_id)
    if config is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    return config


@router.post("/agents/{config_id}/test-run", response_model=TestRunOut, status_code=202)
async def test_run(
    config_id: str, body: TestRunIn, caller: str = Depends(require_admin)
) -> TestRunOut:
    """Start a run against this config and return where to watch it."""
    config = await asyncio.to_thread(_require_config, config_id)
    if not get_settings().openrouter_api_key:
        raise ApiError(
            503,
            "unavailable",
            "No BLOOM_OPENROUTER_API_KEY is configured, so no agent can run yet.",
        )

    run_id = new_id()
    task = asyncio.create_task(
        execute_run(config, body.input, origin="test_run", run_id=run_id, caller=caller),
        name=f"bloom-run-{run_id}",
    )
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    return TestRunOut(
        run_id=run_id,
        status="running",
        stream_url=f"/admin/runs/{run_id}/events",
        trace_url=f"/admin/agents/{config_id}/runs/{run_id}/trace",
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel(run_id: str) -> dict:
    """Ask an in-flight run to stop.

    202, not 200: cancellation is cooperative and asynchronous. This returns as soon
    as the request is delivered; the *outcome* arrives on the trace as
    ``run_finished{status: 'cancelled'}``, the same terminal event every other ending
    produces. A client that waits for the event rather than this response needs no
    special case for stopping.

    A run that has already finished is a 409 rather than a 404 — the run exists, it
    simply cannot be cancelled, and telling those apart is what lets a UI say "it
    already finished" instead of "no such run".
    """
    run = await asyncio.to_thread(get_store().get_run, run_id)
    if run is None:
        raise ApiError(404, "not_found", f"No run {run_id!r}.")
    if run["status"] != "running":
        raise ApiError(409, "conflict", f"Run {run_id!r} already finished ({run['status']}).")
    if not cancel_run(run_id):
        # The row says running but this process is not the one running it: another
        # worker owns it, or it was abandoned by a process that died without the
        # startup sweep having reached it yet.
        raise ApiError(
            409,
            "conflict",
            "This run is not executing in this process, so it cannot be cancelled "
            "here. It will be closed out as abandoned on the next restart.",
        )
    logger.info("Cancel requested for run %s", run_id)
    return {"run_id": run_id, "status": "cancelling"}


@router.get("/runs")
async def list_all_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="running|succeeded|failed|…"),
    origin: str | None = Query(default=None, description="mcp | test_run"),
) -> list[dict]:
    """Every agent's runs in one list, newest first — the activity feed.

    Ordering is why this exists rather than a client fanning out over
    ``/agents/{id}/runs``: pages fetched per agent cannot be interleaved correctly
    without fetching all of them.
    """
    return await asyncio.to_thread(
        get_store().list_all_runs, limit=limit, offset=offset, status=status, origin=origin
    )


@router.get("/agents/{config_id}/runs")
async def list_runs(
    config_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """This agent's runs, newest first."""
    await asyncio.to_thread(_require_config, config_id)
    return await asyncio.to_thread(get_store().list_runs, config_id, limit=limit, offset=offset)


@router.get("/agents/{config_id}/runs/{run_id}/trace")
async def run_trace(
    config_id: str,
    run_id: str,
    after: int = Query(default=0, ge=0, description="Return only events with a greater id."),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """The run plus its events. ``after`` makes this pageable and resumable."""
    run = await asyncio.to_thread(get_store().get_run, run_id)
    # Checked rather than assumed: a run id is unguessable, but serving one config's
    # run under another's URL would make the history view quietly wrong.
    if run is None or run["agent_config_id"] != config_id:
        raise ApiError(404, "not_found", f"No run {run_id!r} for agent {config_id!r}.")
    events = await asyncio.to_thread(get_store().events_after, run_id, after, limit=limit)
    return {"run": run, "events": events}


# How long after a run's row is closed the terminal event is still presumed to be
# in flight on the writer's queue. Comfortably longer than a queue drain, short
# enough that a genuinely lost event does not hold a stream open.
_TERMINAL_GRACE_S = 3.0


def _settled(run: dict) -> bool:
    """Whether a finished run's terminal event has clearly had its chance to land.

    An unparseable or missing ``finished_at`` counts as settled: this gates a safety
    net, and a safety net that fails closed on a malformed timestamp would hold a
    stream open forever.
    """
    raw = run.get("finished_at")
    if not raw:
        return True
    try:
        finished = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return (datetime.now(UTC) - finished).total_seconds() >= _TERMINAL_GRACE_S


def _sse(event_id: int | None, kind: str, data: dict) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/runs/{run_id}/events")
async def stream_events(
    request: Request,
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-sent events for one run: replay, then tail, then stop.

    ``Last-Event-ID`` wins over ``?after=`` because the browser sends it
    automatically on reconnect, and an explicit query parameter left over from the
    first attempt would rewind the stream on every retry.
    """
    store = get_store()
    run = await asyncio.to_thread(store.get_run, run_id)
    if run is None:
        raise ApiError(404, "not_found", f"No run {run_id!r}.")

    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)

    async def generate():
        nonlocal cursor
        nudge = get_writer(store).nudge_for(run_id)
        idle = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    return
                rows = await asyncio.to_thread(store.events_after, run_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield _sse(row["id"], row["kind"], row)
                    if row["kind"] == "run_finished":
                        return

                # Already over before this reader arrived and no terminal event
                # exists — an abandoned run from a previous process, or one whose
                # event was dropped under queue pressure. End rather than tail
                # forever.
                #
                # Gated on how long ago it finished, because the run row and the
                # terminal event are NOT written together: `finish_run` writes the
                # row synchronously while `run_finished` is only queued for the
                # background writer. Without the grace period this fires in that
                # window, synthesizes a terminal event without the real one's cost
                # and stopped_by, and closes the stream just before the real event
                # arrives — so the client sees a worse answer *because* it was
                # watching closely.
                if not rows:
                    current = await asyncio.to_thread(store.get_run, run_id)
                    if current and current["status"] != "running" and _settled(current):
                        yield _sse(None, "run_finished", {"status": current["status"]})
                        return

                nudge.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(nudge.wait(), timeout=_POLL_INTERVAL_S)
                    idle = 0.0
                    continue
                idle += _POLL_INTERVAL_S
                if idle >= _KEEPALIVE_S:
                    idle = 0.0
                    yield ": ping\n\n"
        finally:
            get_writer(store).forget(run_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Belt and braces alongside Caddy's `import streaming` — an nginx in
            # front of this would otherwise buffer the whole response.
            "X-Accel-Buffering": "no",
        },
    )
