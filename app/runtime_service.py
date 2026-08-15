"""Turning a stored `AgentConfig` into a running agent.

This is the seam the whole service exists to provide: a row in a table becomes an
`agent_runtime.AgentRunner` with the right prompt, the right model, the right peer
servers, and — once OAuth lands — the right credentials, without any of that
touching the caller who delegated the task.

Three things here are load-bearing and easy to get wrong.

**Bloom owns the broker's teardown.** `AgentRunner` closes the broker only when it
built one itself, from ``mcp_servers=``. Passing ``broker=`` sets ``_owns_broker``
False and the runner's ``finally`` becomes dead code — so an `MCPClient`'s
``AsyncExitStack`` of streamable-HTTP sessions and its ``httpx2`` client would leak
once per run, ending in fd exhaustion after a few hundred. Hence
:func:`build_runner` returning ``(runner, aclose)`` and every caller using
``try/finally``.

**Stop conditions are always passed explicitly.** Any non-empty list *replaces*
the runtime's default ``StopOnSteps(max_steps)``. A list containing only, say, a
cost guard would leave the step count bounded solely by the hard cap — and hitting
that cap forces an extra completion, so the mistake is more expensive than it
looks.

**Ceilings clamp, they do not defer.** A config may lower ``max_steps`` and
``max_cost_usd`` below the service-wide values but never raise them. A delegated
task is unattended by definition: nobody is watching the tool loop, so this is the
only thing standing between a badly written prompt and an open-ended bill.

One accounting caveat worth knowing when reading a cost: when a stop condition
fires, `agent_runtime` makes one further "final answer" completion that appends no
`Step`. Its tokens reach neither ``RunResult.total_cost`` nor
``agent_runtime_usage``, so a stopped run under-reports by exactly one completion.
That is upstream behaviour, recorded here rather than papered over.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runtime import (
    AgentRunner,
    CompositeBroker,
    LocalToolBroker,
    MCPClient,
    StopOnCost,
    StopOnSteps,
)
from agent_runtime import Settings as RuntimeSettings

from app.config import Settings, get_settings
from app.credentials import CredentialResolver
from app.db import Store, get_store, new_id
from app.providers import get_provider, register_operations
from app.trace import RunRecorder, TracingBroker, get_writer

logger = logging.getLogger(__name__)


def runtime_settings(settings: Settings | None = None) -> RuntimeSettings:
    """Build `agent_runtime`'s settings from Bloom's own.

    Injection rather than environment. `agent_runtime` would happily read
    ``AGENT_RUNTIME_*``, but then Bloom would have two config surfaces that could
    disagree — most damagingly about ``db_path``, where a mismatch means cost rows
    land in ``./agent_runtime.db`` while every other table lives elsewhere, and the
    join between spend and the calls that caused it silently returns nothing.
    """
    settings = settings or get_settings()
    return RuntimeSettings(
        _env_file=None,
        openrouter_api_key=settings.openrouter_api_key,
        app_name=settings.app_name,
        default_tier=settings.default_tier,
        max_tokens=settings.max_tokens,
        max_steps=settings.max_steps,
        db_path=settings.db_path,
        title="Bloom",
    )


def _known_peers(names: list[str]) -> list[str]:
    """Drop peers that cannot be resolved right now, loudly.

    `MCPClient` tolerates a peer that is *unreachable* — it warns and skips — but a
    name it cannot resolve at all raises. Filtering here turns "an agent named a
    peer that has not registered yet" into a degraded run with a warning instead of
    a failed one, which matches the edit-time behaviour: naming a future peer is
    allowed.
    """
    if not names:
        return []
    try:
        from agent_mcp.registry import known_peers

        available = set(known_peers())
    except Exception:  # noqa: BLE001 — no registry means no peers, not a failed run
        logger.warning("Peer registry unavailable; running without peer tools")
        return []
    usable = [n for n in names if n in available]
    missing = [n for n in names if n not in available]
    if missing:
        logger.warning("Skipping unresolvable peer(s): %s", ", ".join(missing))
    return usable


def _credential_broker(config: dict, store: Store, settings: Settings) -> Any | None:
    """Tools backed by this config's connected accounts, or ``None`` if it has none.

    One `LocalToolBroker` holding every operation the bound connections can
    perform. Each tool closes over a *connection id*, never a token — see
    `app.credentials` for why resolving at call time is what makes a token
    expiring mid-run survivable.

    A connection that is not ``active`` contributes no tools, and the caller adds a
    line to the system prompt saying so. Silence would be worse: the model would
    confidently describe having done something it had no way to do.
    """
    if not settings.oauth_enabled:
        return None

    connections = store.connections_for(config["id"])
    if not connections:
        return None

    broker = LocalToolBroker()
    resolver = CredentialResolver(store, settings)
    registered = 0
    for connection in connections:
        if connection["status"] != "active":
            continue
        provider = get_provider(connection["provider"])
        if provider is None:
            logger.warning(
                "Connection %s names provider %r, which has no manifest",
                connection["id"],
                connection["provider"],
            )
            continue
        registered += register_operations(
            broker,
            provider,
            connection["id"],
            resolver,
            granted_scopes=connection["scopes"],
        )

    if not registered:
        return None
    logger.info("Config %s: %d credential tool(s) available", config["slug"], registered)
    return broker


def _connection_notes(config: dict, store: Store, settings: Settings) -> str:
    """A line per unusable connection, appended to the system prompt.

    Without this the model sees no Spotify tools and improvises — usually by
    claiming it did the thing. Told plainly that the account needs reconnecting, it
    passes that on to the user, which is the only useful outcome available.
    """
    if not settings.oauth_enabled:
        return ""
    notes = []
    for connection in store.connections_for(config["id"]):
        if connection["status"] == "active":
            continue
        provider = get_provider(connection["provider"])
        label = provider.display_name if provider else connection["provider"]
        notes.append(
            f"- {label} is not currently connected ({connection['status']}). You cannot "
            f"use it. If the task needs it, say so and tell the user to reconnect it "
            f"in Aperture."
        )
    return ("\n\nConnected accounts:\n" + "\n".join(notes)) if notes else ""


def build_runner(
    config: dict,
    *,
    recorder: RunRecorder,
    settings: Settings | None = None,
    store: Store | None = None,
) -> tuple[AgentRunner, Callable[[], Awaitable[None]]]:
    """Assemble the runner for one run, and the coroutine that tears it down."""
    settings = settings or get_settings()
    store = store or get_store()

    brokers: list[Any] = []
    # Credential tools first: a peer server must never be able to shadow a tool
    # that carries the user's own account access.
    credentials = _credential_broker(config, store, settings)
    if credentials is not None:
        brokers.append(credentials)

    peers = _known_peers(list(config.get("mcp_servers") or ()))
    if peers:
        brokers.append(MCPClient(peers))

    inner = None
    if len(brokers) == 1:
        inner = brokers[0]
    elif brokers:
        inner = CompositeBroker(brokers)

    # Wrapped even when there are no tools: the wrapper is what makes a tool call
    # visible in the trace, and "this agent has no tools" is itself worth being
    # able to see rather than infer from silence.
    broker = TracingBroker(inner, recorder) if inner is not None else None

    max_steps = min(config.get("max_steps") or settings.max_steps, settings.max_steps)
    max_cost = min(config.get("max_cost_usd") or settings.max_cost_usd, settings.max_cost_usd)

    system_prompt = (config.get("system_prompt") or "") + _connection_notes(config, store, settings)

    runner = AgentRunner(
        model=config.get("model_tier") or settings.default_tier,
        broker=broker,
        system_prompt=system_prompt,
        stop_conditions=[StopOnSteps(max_steps), StopOnCost(max_cost)],
        max_tokens=settings.max_tokens,
        settings=runtime_settings(settings),
    )

    async def aclose() -> None:
        if broker is not None:
            await broker.aclose()

    return runner, aclose


async def execute_run(
    config: dict,
    prompt: str,
    *,
    origin: str,
    run_id: str | None = None,
    conversation_id: str = "",
    depth: int = 0,
    caller: str = "",
    settings: Settings | None = None,
    store: Store | None = None,
) -> dict:
    """Run a task end to end and record it. The single execution path.

    ``run_task`` over MCP and ``/admin/agents/{id}/test-run`` both come through
    here — they differ only in ``origin`` and ``caller``. Two implementations would
    drift, and the one that drifted would be the one nobody was watching.

    The ``runs`` row is committed *before* the model is called so a client can
    attach to the trace while the run is still in flight, and the terminal event is
    emitted on every path out, including cancellation, because that event is the
    only thing that ends a stream.
    """
    settings = settings or get_settings()
    store = store or get_store()
    run_id = run_id or new_id()

    await asyncio.to_thread(
        store.create_run,
        run_id=run_id,
        agent_config_id=config["id"],
        prompt=prompt,
        origin=origin,
        conversation_id=conversation_id,
        depth=depth,
        caller=caller,
    )

    writer = get_writer(store)
    recorder = RunRecorder(writer, run_id)
    recorder.run_started(
        agent_slug=config["slug"], origin=origin, model_tier=config.get("model_tier", "")
    )

    runner, aclose = build_runner(config, recorder=recorder, settings=settings, store=store)

    async def on_sentence(sentence: str) -> None:
        # Non-blocking by construction: this is awaited inside the generation loop.
        recorder.text(sentence)

    status = "succeeded"
    error: str | None = None
    output = ""
    cost = 0.0
    stopped_by: str | None = None
    cancelled = False

    try:
        async with asyncio.timeout(settings.run_timeout_s):
            result = await runner.run(
                prompt,
                conversation_id=conversation_id or run_id,
                depth=depth,
                on_sentence=on_sentence,
            )
        output = result.text
        cost = result.total_cost
        stopped_by = result.stopped_by
        recorder.steps(result.steps)
    except asyncio.CancelledError:
        # Re-raised below, after the run is closed out. A cancelled run that stays
        # `running` forever is the worst of both outcomes.
        status, error, cancelled = "cancelled", "cancelled", True
    except TimeoutError:
        status = "failed"
        error = f"timed out after {settings.run_timeout_s:.0f}s"
        logger.warning("Run %s %s", run_id, error)
    except Exception as exc:  # noqa: BLE001 — one bad run must not take down the service
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Run %s failed", run_id)
    finally:
        try:
            await aclose()
        except Exception:  # noqa: BLE001 — teardown must not mask the real outcome
            logger.exception("Failed to close the broker for run %s", run_id)

        await asyncio.to_thread(
            store.finish_run,
            run_id,
            status=status,
            result_text=output or None,
            stopped_by=stopped_by,
            total_cost_usd=cost,
            error=error,
        )
        recorder.run_finished(status=status, cost_usd=cost, stopped_by=stopped_by, error=error)

    if cancelled:
        raise asyncio.CancelledError

    return {
        "run_id": run_id,
        "status": status,
        "output": output,
        "cost_usd": cost,
        "stopped_by": stopped_by,
        "error": error,
    }
