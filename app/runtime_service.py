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

from app import models
from app.builder import is_builder
from app.config import Settings, get_settings
from app.credentials import CredentialResolver
from app.crypto import UndecryptableToken, decrypt
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


def _connection_broker(
    connections: list[dict], config: dict, store: Store, settings: Settings
) -> Any | None:
    """Tools backed by this agent's provider connections — OAuth and API key alike.

    One `LocalToolBroker` holding every operation the attached connections can
    perform. Each tool closes over a *connection id*, never a secret — see
    `app.credentials` for why resolving at call time is what makes a token
    expiring mid-run survivable.

    A connection that is not ``active`` contributes no tools, and the caller adds a
    line to the system prompt saying so. Silence would be worse: the model would
    confidently describe having done something it had no way to do.
    """
    if not settings.oauth_enabled:
        return None

    broker = LocalToolBroker()
    resolver = CredentialResolver(store, settings)
    seen: set[str] = set()
    registered = 0
    for connection in connections:
        if connection["kind"] not in {"oauth", "api_key"} or connection["status"] != "active":
            continue
        provider = get_provider(connection["provider"])
        if provider is None:
            logger.warning(
                "Connection %s names provider %r, which has no manifest",
                connection["id"],
                connection["provider"],
            )
            continue
        if connection["provider"] in seen:
            # The attach endpoint refuses this with a 409, so reaching here means a
            # hand-edited database. Skipping beats registering two tools under one
            # name and letting the broker silently pick.
            logger.warning(
                "Config %s attaches two %s connections; using the first",
                config["slug"],
                connection["provider"],
            )
            continue
        seen.add(connection["provider"])
        registered += register_operations(
            broker,
            provider,
            connection["id"],
            resolver,
            # OAuth: the granted scopes bound what the token can do. API key: the
            # provider's own console did, invisibly — so unscoped unless an operator
            # deliberately narrowed it.
            granted_scopes=(
                connection["scopes"]
                if connection["kind"] == "oauth"
                else (connection["scopes"] or None)
            ),
        )

    if not registered:
        return None
    logger.info("Config %s: %d credential tool(s) available", config["slug"], registered)
    return broker


def _peer_resolver(
    connections: list[dict], store: Store, settings: Settings
) -> tuple[list[str], dict[str, dict]]:
    """The peer names and the plain mapping `MCPClient` resolves them through.

    This replaces looking names up in `agent_mcp.registry`: a peer is now a stored
    connection carrying its own URL and bearer, so an agent reaches exactly what it
    was given rather than whatever a shared registry happens to hold.

    Peer bearers are decrypted **here, at build time** — the opposite of the rule
    for provider tokens, for reasons that do not apply to them:

    * a peer bearer is a static token an operator set, not a grant that expires
      mid-run, so the failure `app.credentials` exists to prevent cannot happen;
    * `MCPClient` opens one session per server and caches it, setting
      ``Authorization`` once at session open — late resolution could not change a
      header that is already fixed for the life of the client;
    * its ``_resolve`` is *synchronous*, so a lazy mapping would put a SQLite read
      and a Fernet decrypt on the event loop, against the house rule that every
      store call is wrapped in ``asyncio.to_thread``.

    The plaintext lives in this mapping and in the client's own headers for one run,
    then goes with them. It never reaches the model, a tool argument, or the trace.
    """
    names: list[str] = []
    mapping: dict[str, dict] = {}
    for connection in connections:
        if connection["kind"] != "mcp" or connection["status"] != "active":
            continue
        config = connection["config"] or {}
        url = str(config.get("url") or "").strip()
        if not url:
            logger.warning("MCP connection %s has no url; skipping", connection["name"])
            continue
        # `url` vs `base_url` decides whether `MCPClient` uses the string verbatim.
        # Its `_endpoint` appends `/mcp` and a trailing slash to a `base_url`, which
        # is right for an ecosystem peer entered as a bare host and wrong for a
        # server discovered from the MCP registry, whose remotes are already complete
        # endpoints (`https://server.smithery.ai/@owner/thing/mcp`). Appending there
        # yields `/mcp/mcp/` and a 404 that reads like the server being down.
        #
        # So the distinction is stored rather than guessed: whoever created the
        # connection knew which kind of URL it was, and said so.
        record: dict[str, Any] = {"url": url} if config.get("endpoint") else {"base_url": url}
        row = store.connection_secrets(connection["id"])
        if row is not None and row["secret"]:
            try:
                record["token"] = decrypt(row["secret"], settings)
            except UndecryptableToken:
                logger.exception(
                    "MCP connection %s cannot be decrypted; skipping", connection["name"]
                )
                continue
        names.append(connection["name"])
        mapping[connection["name"]] = record
    return names, mapping


def _connection_notes(connections: list[dict], settings: Settings) -> str:
    """A line per unusable connection, appended to the system prompt.

    Without this the model sees no Spotify tools and improvises — usually by
    claiming it did the thing. Told plainly that the account needs reconnecting, it
    passes that on to the user, which is the only useful outcome available.
    """
    if not settings.oauth_enabled:
        return ""
    notes = []
    for connection in connections:
        if connection["status"] == "active":
            continue
        if connection["kind"] == "mcp":
            notes.append(
                f"- The {connection['label'] or connection['name']} server is not "
                f"currently reachable ({connection['status']}), so its "
                f"{connection['name']}__* tools are unavailable."
            )
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
    prompt: str = "",
) -> tuple[AgentRunner, Callable[[], Awaitable[None]]]:
    """Assemble the runner for one run, and the coroutine that tears it down."""
    settings = settings or get_settings()
    store = store or get_store()

    # Read once and pass down: this used to be three separate queries per build.
    connections = store.connections_for(config["id"])

    brokers: list[Any] = []
    # First, and only for the builder. These are the tools that write configurations
    # and connections, and nothing an operator can attach produces them: they are not
    # derived from `connections` at all, so the only way to reach them is to *be* the
    # row whose slug is BUILDER_SLUG — which the admin validators reserve and the
    # UNIQUE index makes unforgeable. First in the list for the same reason
    # credentials precede peers below: nothing attached should be able to shadow them.
    if is_builder(config) and settings.feature_builder:
        from app.builder.tools import builder_broker

        brokers.append(builder_broker(store, settings, run_id=recorder.run_id, brief=prompt))

    # Credential tools next: a peer server must never be able to shadow a tool
    # that carries the user's own account access.
    credentials = _connection_broker(connections, config, store, settings)
    if credentials is not None:
        brokers.append(credentials)

    peers, peer_records = _peer_resolver(connections, store, settings)
    if peers:
        # One client for every peer, resolving through a plain mapping — the sync
        # store registry is bypassed entirely.
        brokers.append(MCPClient(peers, resolver=peer_records))

    inner = None
    if len(brokers) == 1:
        inner = brokers[0]
    elif brokers:
        inner = CompositeBroker(brokers)

    # Wrapped even when there are no tools: the wrapper is what makes a tool call
    # visible in the trace, and "this agent has no tools" is itself worth being
    # able to see rather than infer from silence.
    broker = TracingBroker(inner, recorder) if inner is not None else None

    # Two ceilings, and which one applies is a property of the agent rather than of
    # the request. A delegated task is a handful of steps; a build is inspect, search
    # the registry, read a page or two, create, verify, write the checklist — 12 to 25
    # — and clamping that to the service default of 8 would truncate every build.
    # Raising the *service* ceiling instead would raise it for every unattended task,
    # which is precisely what that ceiling exists to prevent.
    #
    # The invariant is unchanged: a config may lower its ceiling but never raise it.
    # There are simply two of them.
    ceiling_steps = settings.builder_max_steps if is_builder(config) else settings.max_steps
    ceiling_cost = settings.builder_max_cost_usd if is_builder(config) else settings.max_cost_usd
    max_steps = min(config.get("max_steps") or ceiling_steps, ceiling_steps)
    max_cost = min(config.get("max_cost_usd") or ceiling_cost, ceiling_cost)

    system_prompt = (config.get("system_prompt") or "") + _connection_notes(connections, settings)

    runner = AgentRunner(
        # Resolved here rather than handed over as a keyword: the vocabulary is the
        # ecosystem's model keywords, and `agent_runtime.model_router` knows only
        # three of them — it would raise `UnknownTier` on `coding`.
        model=models.resolve(config.get("model_tier"), settings),
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


# Every in-flight run, by id, so it can be cancelled from a request handler.
#
# Keyed by run id rather than held beside the background tasks in `app.admin.runs`
# because an MCP-origin run has no background task — `run_task` awaits `execute_run`
# inline in the request handler. Registering `current_task()` from inside the run
# itself is the only place that sees both origins.
_IN_FLIGHT: dict[str, asyncio.Task] = {}


def cancel_run(run_id: str) -> bool:
    """Ask an in-flight run to stop. False when it is not running here.

    Cancellation is cooperative and asynchronous: this requests it and returns.
    `execute_run`'s ``except CancelledError`` closes the row and emits the terminal
    event, so the caller learns the outcome from the trace like any other ending
    rather than from this return value.

    "Not running here" is also the honest answer for a run owned by a different
    process — a state this service cannot reach and should not pretend to.
    """
    task = _IN_FLIGHT.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    logger.info("Cancellation requested for run %s", run_id)
    return True


def in_flight_ids() -> list[str]:
    """Run ids this process is currently executing. Used by tests and diagnostics."""
    return [rid for rid, task in _IN_FLIGHT.items() if not task.done()]


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
    timeout_s: float | None = None,
) -> dict:
    """Run a task end to end and record it. The single execution path.

    ``run_task`` over MCP and ``/admin/agents/{id}/test-run`` both come through
    here — they differ only in ``origin`` and ``caller``. Two implementations would
    drift, and the one that drifted would be the one nobody was watching.

    The ``runs`` row is committed *before* the model is called so a client can
    attach to the trace while the run is still in flight, and the terminal event is
    emitted on every path out, including cancellation, because that event is the
    only thing that ends a stream.

    ``timeout_s`` overrides the service-wide wall clock for this one invocation. A
    parameter rather than a config lookup because it is a property of *what is being
    run*, not of the deployment: a build makes six network round trips and needs
    minutes, while a delegated task that has not answered in five is stuck.
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

    # Registered after the row exists, so anything that can be cancelled can also be
    # read back. Removed in the `finally` below, whatever the outcome.
    current = asyncio.current_task()
    if current is not None:
        _IN_FLIGHT[run_id] = current

    writer = get_writer(store)
    recorder = RunRecorder(writer, run_id)
    recorder.run_started(
        agent_slug=config["slug"], origin=origin, model_tier=config.get("model_tier", "")
    )

    runner, aclose = build_runner(
        config, recorder=recorder, settings=settings, store=store, prompt=prompt
    )

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
        async with asyncio.timeout(timeout_s or settings.run_timeout_s):
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
        error = f"timed out after {timeout_s or settings.run_timeout_s:.0f}s"
        logger.warning("Run %s %s", run_id, error)
    except Exception as exc:  # noqa: BLE001 — one bad run must not take down the service
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Run %s failed", run_id)
    finally:
        _IN_FLIGHT.pop(run_id, None)
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
