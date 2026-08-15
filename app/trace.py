"""The run trace: what happened inside a run, live and afterwards.

Neither embedded library offers a step or debug hook — `agent_mcp` has no such
mechanism at all, and `agent_runtime` has exactly one callback, ``on_sentence``.
So the trace is assembled here from the three seams that genuinely exist:

===========================  ==========================================  =======
Seam                         Gives                                       Live?
===========================  ==========================================  =======
``run(on_sentence=...)``     assistant text, sentence by sentence        yes
:class:`TracingBroker`       tool name and args *before* the call, then  yes
                             outcome and latency after it
``RunResult.steps``          per-step model, tokens, cost, ``stopped_by``  no
===========================  ==========================================  =======

What is deliberately *not* here: token-level deltas. Those exist only through
``AgentRunner.stream()``, which discards its run state and records no usage rows,
so using it would trade the entire cost ledger for a smoother cursor.

Also absent: an observer stop-condition. `agent_runtime` calls ``first_triggered``
only after a tool round trip, so a condition never sees the final text-only step —
it would deliver step costs slightly earlier for some steps and never for the last
one. ``RunResult.steps`` is written once when the run returns, and there is
nothing to reconcile.

**Why a background writer.** ``on_sentence`` is awaited *inside* the generation
loop, and ``bloom.db`` is written by three separate connections holding three
separate locks (Bloom's tables, `agent_mcp`'s tool log, `agent_runtime`'s cost
rows). WAL removes reader/writer blocking, not writer/writer — so a synchronous
per-event write from that callback intermittently stalls the model on ``database
is locked``. Every callback here therefore does one ``put_nowait`` onto a queue
and returns; a single task drains it. If the queue is full the event is dropped
with a warning, because losing a line of narration is strictly better than
stalling the run it describes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.db import Store

logger = logging.getLogger(__name__)

# How much of a tool's result is worth keeping in the trace. A trace is for
# understanding what happened, not for replaying it — the model already got the
# full text, and a 200 KB API response in the event log helps nobody.
MAX_TRACED_RESULT = 2000
MAX_TRACED_ARGS = 2000


@dataclass(frozen=True)
class TraceEvent:
    """One row destined for ``run_events``."""

    run_id: str
    seq: int
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)


def _clip(text: str, limit: int) -> str:
    """Truncate with a visible marker, so a reader knows it is not the whole thing."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more characters]"


class TraceWriter:
    """Process-wide queue and the single task that drains it into the store.

    One writer, not one per run: the point is to serialise every trace write onto
    a thread that is never the one running a model loop.
    """

    def __init__(self, store: Store, *, maxsize: int = 2000) -> None:
        self._store = store
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self._nudges: dict[str, asyncio.Event] = {}
        self._dropped = 0

    # --- producer side (called from the model loop; must never block) ---

    def emit(self, event: TraceEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            # Logged once per hundred so a sustained overflow is visible without
            # the log itself becoming the bottleneck.
            if self._dropped % 100 == 1:
                logger.warning(
                    "Trace queue full; dropped %d event(s). The run is unaffected.",
                    self._dropped,
                )

    # --- consumer side ---

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="bloom-trace-writer")

    async def stop(self) -> None:
        """Drain what is queued, then stop. Called from the lifespan on shutdown."""
        if self._task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._queue.join(), timeout=5.0)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await asyncio.to_thread(
                    self._store.append_event,
                    event.run_id,
                    seq=event.seq,
                    kind=event.kind,
                    **event.fields,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a trace failure must not end the writer
                logger.exception("Failed to persist trace event for run %s", event.run_id)
            finally:
                self._queue.task_done()
                self._wake(event.run_id)

    # --- the nudge a streaming reader waits on ---

    def _wake(self, run_id: str) -> None:
        nudge = self._nudges.get(run_id)
        if nudge is not None:
            nudge.set()

    def nudge_for(self, run_id: str) -> asyncio.Event:
        """The event a reader awaits to learn that new rows may exist.

        Only a hint. A reader that misses one still finds the rows on its next
        poll, because it always re-queries by cursor and never trusts the nudge to
        tell it *what* changed — which is what keeps this correct despite the
        obvious set/clear race between one writer and several readers.
        """
        return self._nudges.setdefault(run_id, asyncio.Event())

    def forget(self, run_id: str) -> None:
        self._nudges.pop(run_id, None)


class RunRecorder:
    """The trace of one run: a sequence counter and typed emit helpers.

    Every method is synchronous and non-blocking on purpose — they are called from
    inside the generation loop.
    """

    def __init__(self, writer: TraceWriter, run_id: str, *, start_seq: int = 0) -> None:
        self._writer = writer
        self.run_id = run_id
        self._seq = start_seq

    def _emit(self, kind: str, **fields: Any) -> None:
        self._writer.emit(TraceEvent(self.run_id, self._seq, kind, fields))
        self._seq += 1

    def run_started(self, *, agent_slug: str, origin: str, model_tier: str) -> None:
        self._emit(
            "run_started",
            payload={"agent_slug": agent_slug, "origin": origin, "model_tier": model_tier},
        )

    def text(self, sentence: str) -> None:
        """One completed sentence of the assistant's answer, as it forms."""
        self._emit("text", payload={"text": sentence})

    def tool_started(self, name: str, args: dict, *, call_id: str | None = None) -> None:
        self._emit(
            "tool_started",
            tool_name=name,
            tool_call_id=call_id,
            payload={"args": _clip(json.dumps(args, default=str), MAX_TRACED_ARGS)},
        )

    def tool_finished(
        self, name: str, *, ok: bool, latency_ms: int, result: str, call_id: str | None = None
    ) -> None:
        self._emit(
            "tool_finished",
            tool_name=name,
            tool_call_id=call_id,
            ok=ok,
            latency_ms=latency_ms,
            payload={"result": _clip(result, MAX_TRACED_RESULT)},
        )

    def steps(self, steps: list) -> None:
        """Persist ``RunResult.steps`` once the run has returned.

        This is the authoritative half of the trace: per-step model, token counts
        and cost. It arrives at the end because that is when the runtime makes it
        available, and no amount of hooking changes that.
        """
        for step in steps:
            self._emit(
                "step_finished",
                step_index=getattr(step, "index", None),
                tokens_in=getattr(step, "tokens_in", None),
                tokens_out=getattr(step, "tokens_out", None),
                cost_usd=getattr(step, "cost_usd", None),
                payload={
                    "model": getattr(step, "model", ""),
                    "tool_calls": [c.get("name") for c in getattr(step, "tool_calls", []) or []],
                    "started_at": getattr(step, "started_at", ""),
                    "finished_at": getattr(step, "finished_at", ""),
                },
            )

    def run_finished(
        self, *, status: str, cost_usd: float, stopped_by: str | None, error: str | None = None
    ) -> None:
        """The terminal event. **A stream ends on this and nothing else**, so it
        must be emitted on every path out of a run, including cancellation."""
        self._emit(
            "run_finished",
            ok=status == "succeeded",
            cost_usd=cost_usd,
            payload={"status": status, "stopped_by": stopped_by, "error": error},
        )


class TracingBroker:
    """Wraps a `ToolBroker` to narrate its calls.

    This is the only place a tool can be observed *before* it returns, which is
    what makes "Bloom is currently searching Spotify" possible rather than a list
    of things that already happened.

    ``bind`` takes and forwards ``**kwargs`` rather than a fixed signature:
    `CompositeBroker.bind` fans arbitrary keywords out to its children, and a
    wrapper that pinned the signature would silently swallow anything the runtime
    adds later.
    """

    def __init__(self, inner: Any, recorder: RunRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self._recorder.tool_started(name, args or {})
        started = time.monotonic()
        try:
            result = await self._inner.call_tool(name, args)
        except asyncio.CancelledError:
            # An interrupt must unwind the turn, but the trace should still show
            # that the tool did not finish rather than ending mid-sentence.
            self._recorder.tool_finished(
                name,
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                result="cancelled",
            )
            raise
        # No broad `except` here: the underlying brokers already convert every
        # tool failure into a result *string* rather than an exception, so
        # catching would only ever catch a bug in the broker itself — which
        # should surface, not be logged as a failed tool call.
        self._recorder.tool_finished(
            name,
            ok=not result.startswith("Error"),
            latency_ms=int((time.monotonic() - started) * 1000),
            result=result,
        )
        return result

    def bind(self, **kwargs: Any) -> None:
        bind = getattr(self._inner, "bind", None)
        if bind is not None:
            bind(**kwargs)

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()


_writer: TraceWriter | None = None


def get_writer(store: Store | None = None) -> TraceWriter:
    """The process-wide writer. Built on first use, started by the lifespan."""
    global _writer
    if _writer is None:
        from app.db import get_store

        _writer = TraceWriter(store or get_store())
    return _writer


def reset_writer() -> None:
    """Drop the singleton. For tests, which build their own store per case."""
    global _writer
    _writer = None
