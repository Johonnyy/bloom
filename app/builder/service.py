"""Starting a build: the one entry point both the HTTP route and the MCP tool use.

Two callers, one implementation, for the same reason `execute_run` is the single
execution path: two would drift, and the one that drifted would be the one nobody was
watching.

The ordering here matters and mirrors `Store.create_run`'s. The ``builds`` row and the
``runs`` row are both committed **before** the model is called, sharing a run id
minted up front, so a client that posts a brief can attach to the trace while the work
is still in flight. That is also why the HTTP route can answer 202 with a stream URL
instead of blocking for a minute and a half.

The build's terminal status is decided *from* the row the tools wrote, not from the
model's closing paragraph:

* the builder called `bloom_set_setup_checklist` → ``needs_setup``, or ``ready`` if
  every connection it attached is somehow already active;
* it created an agent but never wrote a checklist → ``needs_setup`` anyway, with a
  summary saying so. The agent exists; pretending the build failed would hide it;
* it created nothing → ``failed``, which is the honest outcome for "no MCP server and
  no manifest" and the one the prompt tells it to produce deliberately.

A model's prose is not evidence. What it actually did is.
"""

from __future__ import annotations

import asyncio
import logging

from app.builder.agent import BUILDER_SLUG, ensure_builder_config
from app.config import Settings, get_settings
from app.db import Store, get_store, new_id
from app.runtime_service import execute_run

logger = logging.getLogger(__name__)


async def start_build(
    brief: str,
    *,
    origin: str,
    conversation_id: str = "",
    depth: int = 0,
    caller: str = "",
    settings: Settings | None = None,
    store: Store | None = None,
    build_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Create the build row and run the builder. Returns the finished build.

    Awaits the run — the HTTP route wraps this in a background task and answers 202,
    while the MCP tool awaits it inline, because Amber's caller wants the checklist
    in the reply rather than a handle to poll.

    ``build_id`` and ``run_id`` may be supplied by a caller that has already answered
    with them. That is what lets the HTTP route return a stream URL synchronously
    while the work happens behind it; omitted, they are minted here.
    """
    settings = settings or get_settings()
    store = store or get_store()

    config = await asyncio.to_thread(ensure_builder_config, store, settings)
    run_id = run_id or new_id()
    build_id = build_id or new_id()
    await asyncio.to_thread(store.create_build, build_id=build_id, run_id=run_id, brief=brief)

    try:
        result = await execute_run(
            config,
            brief,
            origin=origin,
            run_id=run_id,
            conversation_id=conversation_id,
            depth=depth,
            caller=caller,
            settings=settings,
            store=store,
            timeout_s=settings.builder_run_timeout_s,
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(store.update_build, build_id, status="failed", error="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 — one bad build must not take down the service
        logger.exception("Build %s failed", build_id)
        await asyncio.to_thread(
            store.update_build, build_id, status="failed", error=f"{type(exc).__name__}: {exc}"
        )
        return await asyncio.to_thread(store.get_build, build_id)  # type: ignore[return-value]

    return await _settle(store, build_id, result)


async def _settle(store: Store, build_id: str, result: dict) -> dict:
    """Decide the build's terminal status from what the tools actually recorded."""
    build = await asyncio.to_thread(store.get_build, build_id)
    if build is None:  # pragma: no cover — the row is created above
        return {}

    if result.get("status") != "succeeded":
        return await asyncio.to_thread(  # type: ignore[return-value]
            store.update_build,
            build_id,
            status="failed",
            error=result.get("error") or result.get("status"),
            summary=build["summary"] or (result.get("output") or "")[:4000],
        )

    if not build.get("agent_config_id"):
        # No agent was created. For "no MCP server and no manifest" this is the
        # correct, prompted outcome — so the model's own explanation is the summary,
        # because it is the only place the reason exists.
        return await asyncio.to_thread(  # type: ignore[return-value]
            store.update_build,
            build_id,
            status="failed",
            summary=build["summary"] or (result.get("output") or "")[:4000],
            error="no agent was created",
        )

    summary = build["summary"] or (result.get("output") or "")[:4000]
    if not build.get("checklist"):
        # The agent exists, so this is not a failure — but a build that skipped the
        # checklist has left the user with no idea what to do next, and saying so is
        # more useful than an empty list.
        summary = (
            summary + "\n\n(The builder finished without writing a setup checklist. Check the "
            "agent's connections in Aperture to see what still needs a credential.)"
        ).strip()

    status = "ready" if await _all_active(store, build["agent_config_id"]) else "needs_setup"
    return await asyncio.to_thread(  # type: ignore[return-value]
        store.update_build, build_id, status=status, summary=summary
    )


async def _all_active(store: Store, agent_config_id: str) -> bool:
    """Whether every connection this agent has is usable right now.

    An agent with no connections at all is ``ready``: a summariser needs nothing
    attached, and reporting it as awaiting setup would send the user looking for work
    that does not exist.
    """
    attached = await asyncio.to_thread(store.connections_for, agent_config_id)
    return all(c["status"] == "active" for c in attached)


__all__ = ["BUILDER_SLUG", "start_build"]
