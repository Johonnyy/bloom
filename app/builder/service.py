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

**An edit is the same machinery with a different question at the end.** The two
statuses cannot be decided the same way, because the two runs differ in what
"succeeded" means:

* a build is judged by whether an agent exists afterwards. An edit cannot be — the
  agent existed beforehand too, which is the whole premise. It is judged by
  ``changes``, the list its tools append to as they write;
* a build's connections are all freshly created and therefore ``pending``, so
  `_all_active` alone settles it correctly. An edit's connections are usually
  already ``active`` and stay that way even when a scope change has left them
  needing re-consent — a token keeps the grant it was issued with. So an edit also
  settles on its checklist: any step that is not ``manual`` is work for a human,
  and reporting ``ready`` while a `connect_oauth` step is outstanding would tell
  someone a permission is live when it is not.

That asymmetry is deliberate. The alternative — downgrading a re-scoped connection
to ``pending`` so `_all_active` catches it — would strip every one of that agent's
tools until someone came back, breaking a working agent to signal that it is about
to become more capable.
"""

from __future__ import annotations

import asyncio
import logging

from app.builder.agent import BUILDER_SLUG, ensure_builder_config
from app.config import Settings, get_settings
from app.db import Store, get_store, new_id
from app.runtime_service import execute_run

logger = logging.getLogger(__name__)


class UnknownAgent(LookupError):
    """The slug an edit names does not exist, or may not be edited."""


def resolve_edit_target(slug: str, store: Store | None = None) -> dict:
    """The agent an edit brief names, or raise :class:`UnknownAgent`.

    Called *before* the run rather than left to the builder's own tools, because a
    brief naming an agent that does not exist would otherwise cost a full builder
    run — a strong model, a web search or two, a minute and a half — to arrive at
    "no such agent", which is knowable for free.
    """
    store = store or get_store()
    slug = (slug or "").strip().lower()
    if slug == BUILDER_SLUG:
        raise UnknownAgent(
            f"{BUILDER_SLUG!r} is Bloom's own builder. Its instructions are defined in "
            "code and re-seeded at every boot, so it cannot be edited."
        )
    row = store.get_config_by_slug(slug)
    if row is None:
        raise UnknownAgent(f"No agent named {slug!r}.")
    return row


def edit_brief(agent_slug: str, change: str) -> str:
    """The brief an edit run is given.

    Composed here rather than by each caller so the MCP tool and the HTTP route hand
    the builder the identical framing — the ``brief`` column is also what a human
    reads back weeks later to see what was asked for.
    """
    return (
        f"Edit the existing Bloom agent '{agent_slug}'. Do not create a new agent.\n\n"
        f"Requested change:\n{change.strip()}"
    )


async def start_build(
    brief: str,
    *,
    origin: str,
    mode: str = "build",
    agent: dict | None = None,
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

    ``mode='edit'`` takes the already-resolved ``agent`` (see :func:`resolve_edit_target`)
    and stamps it on the row up front, so a run that dies in its first second still
    leaves a build naming what was being edited.
    """
    settings = settings or get_settings()
    store = store or get_store()

    config = await asyncio.to_thread(ensure_builder_config, store, settings)
    run_id = run_id or new_id()
    build_id = build_id or new_id()
    await asyncio.to_thread(
        store.create_build,
        build_id=build_id,
        run_id=run_id,
        brief=brief,
        mode=mode,
        agent_config_id=agent["id"] if agent else None,
        agent_slug=agent["slug"] if agent else "",
    )

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

    if build.get("mode") == "edit":
        return await _settle_edit(store, build, result)

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


async def _settle_edit(store: Store, build: dict, result: dict) -> dict:
    """Decide an edit's terminal status. See the module docstring for why it differs.

    A run that changed nothing is ``failed`` even though the model returned happily.
    That is the honest report for "I could not widen those scopes" and, more often,
    for a brief the builder answered with a paragraph instead of a tool call — and
    the caller relaying it to a user needs to know which of those happened.
    """
    build_id = build["id"]
    summary = build["summary"] or (result.get("output") or "")[:4000]

    if not build["changes"]:
        return await asyncio.to_thread(  # type: ignore[return-value]
            store.update_build,
            build_id,
            status="failed",
            summary=summary,
            error="nothing was changed",
        )

    # Any non-manual step is work only a person can do — a credential to paste, a
    # consent screen to approve. `manual` is the kind the checklist coerces anything
    # unrecognised to, and the one the prompt asks for when there is nothing to do.
    outstanding = any(
        isinstance(step, dict) and step.get("kind") != "manual" for step in build["checklist"]
    )
    live = await _all_active(store, build["agent_config_id"])
    status = "ready" if live and not outstanding else "needs_setup"
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


__all__ = ["BUILDER_SLUG", "UnknownAgent", "edit_brief", "resolve_edit_target", "start_build"]
