"""Bloom's MCP server — the execution surface.

This is not "an app optionally exposing itself to the agent layer", which is what
every other repo in the ecosystem mounts. It is the reason Bloom exists: Amber
hands a task here rather than carrying a Spotify integration herself, and MCP is
how she does it.

So the tool list is deliberately short. One action — ``run_task`` — plus read-only
surfaces that let a caller find out which agents exist before choosing one. Broad
capability lives in the *configured agents*, not in this server's own tool list.

Everything is configured by injection from `app.config`, never from ``AGENT_MCP_*``
environment variables — Bloom has one config surface, and the most damaging thing
two surfaces could disagree about is which database file to write.

**On ``requires_confirmation``.** ``run_task`` changes state, spends money, and can
reach a user's connected accounts, so it is the strongest candidate in the
ecosystem for a confirmation gate. It is still not marked: that gate is satisfied
only by an inbound ``X-Confirmed: true``, and no caller can produce one yet —
Amber's wire protocol has no tool-approval frame and Aperture has no HTTP client.
Marking it today would make Bloom permanently uncallable rather than safely gated.
The real control is the per-config step and cost ceiling, which binds now. Revisit
the moment a human-approval channel exists.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path

from agent_mcp import AgentMCPServer, AgentMCPSettings, current_call

from app.config import Settings, get_settings
from app.db import get_store
from app.runtime_service import execute_run

logger = logging.getLogger(__name__)


def mcp_settings(settings: Settings | None = None) -> AgentMCPSettings:
    """Build `agent_mcp`'s settings from Bloom's own.

    ``_env_file=None`` so nothing is read from disk: every value comes from
    `app.config`.

    Note what is *not* used: ``auth_keys_env``. That parameter names an environment
    variable the library reads through ``os.environ``, but pydantic-settings loads
    ``.env`` into the model without exporting it — so a key written only to
    ``.env`` would be invisible, auth would fail closed, and the server would
    refuse to start. It works under Docker, where compose exports the file, and
    breaks under a local ``uvicorn``. Passing ``keys=`` behaves identically in both.
    """
    settings = settings or get_settings()

    # The usage sink opens this file eagerly when the server is constructed, and
    # sqlite3 will not create a missing directory — a fresh clone with the default
    # `data/bloom.db` would otherwise die at startup with a bare "unable to open
    # database file" that names no path.
    usage_path = Path(settings.db_path)
    if usage_path.parent != Path(""):
        usage_path.parent.mkdir(parents=True, exist_ok=True)

    return AgentMCPSettings(
        _env_file=None,
        app_name=settings.app_name,
        version=settings.version,
        keys=settings.mcp_keys,
        allow_anonymous=False,
        public_url=settings.mcp_public_url,
        sync_store_url=settings.mcp_sync_store_url,
        sync_store_token=settings.mcp_sync_store_token,
        peers=settings.mcp_peers,
        peer_token=settings.mcp_peer_token,
        # The tool log shares Bloom's database with its own tables and
        # agent-runtime's cost rows. The shared conversation_id / app_name / depth
        # / created_at columns let spend be joined to the calls that caused it;
        # see app/db.py on why WAL and a busy timeout are mandatory here.
        usage_db_path=settings.db_path,
        usage_enabled=True,
    )


def build_server(settings: Settings | None = None) -> AgentMCPServer:
    """Construct the MCP server with every resource and tool registered.

    Not cached itself — see :func:`get_mcp_server` — so tests can build an isolated
    instance without clearing a singleton.
    """
    settings = settings or get_settings()
    scheme = settings.app_name
    mcp = AgentMCPServer(
        app_name=settings.app_name,
        version=settings.version,
        settings=mcp_settings(settings),
        # Written for a model, not a human. This is the only description an
        # aggregating agent sees before it decides whether to delegate here.
        instructions=(
            "Bloom runs configured specialist agents on your behalf. Each has its "
            "own instructions, tools and connected accounts — a music agent that "
            "can control Spotify, for example. List the agents to see what is "
            "available, then delegate a whole task to the one that fits and use "
            "its answer. Prefer delegating over refusing: an agent here may hold "
            "credentials and tools you do not."
        ),
    )

    # --- resources: the same rows the management API serves ---

    @mcp.resource(f"{scheme}://agents")
    async def configured_agents() -> list[dict]:
        """Every configured agent: slug, name, tier, and what it can reach."""
        rows = await asyncio.to_thread(get_store().list_configs)
        # One join rather than a query per row: this endpoint builds a list, and a
        # per-agent read would scale with it.
        attached = await asyncio.to_thread(get_store().connections_by_agent)
        return [
            {
                "slug": r["slug"],
                "name": r["name"],
                "model_tier": r["model_tier"],
                # Capability, not configuration. A caller deciding whether to
                # delegate needs to tell "has Spotify" from "had Spotify", so the
                # status is here — but no ids, which are admin handles that mean
                # nothing to a model, and nothing that could carry a secret.
                "connections": [
                    {
                        "kind": c["kind"],
                        "provider": c["provider"],
                        "name": c["name"],
                        "label": c["label"],
                        "status": c["status"],
                    }
                    for c in attached.get(r["id"], ())
                ],
                # The prompt is the agent's *description* for a caller deciding
                # whether to delegate, so it is served rather than hidden. It is
                # configuration a human wrote, not a secret.
                "system_prompt": r["system_prompt"],
            }
            for r in rows
        ]

    # --- tools ---

    @mcp.tool(read_only=False)
    async def run_task(description: str, agent_slug: str) -> dict:
        """Delegate a whole task to one of Bloom's configured agents.

        Give the task in plain language, as you would to a capable colleague — the
        agent has its own instructions and tools and does not see this
        conversation. Use ``list_agents`` first if you are unsure which to pick.
        Returns the agent's answer, what it cost, and a run id for its trace.
        """
        config = await asyncio.to_thread(get_store().get_config_by_slug, agent_slug)
        if config is None:
            known = [c["slug"] for c in await asyncio.to_thread(get_store().list_configs)]
            # A string, not an exception: an ordinary return keeps the caller's
            # turn alive and lets the model retry with a name that exists.
            return {
                "error": f"No agent named {agent_slug!r}.",
                "available": known,
            }

        # Depth and conversation come from the *authenticated* call scope, never
        # from arguments. A tool argument is model-authored data that anyone
        # reaching this endpoint can set, and depth is precisely the counter that
        # stops an unbounded billable agent-to-agent loop. `agent_mcp` has already
        # parsed and range-checked the header at the gate.
        scope = current_call()
        if scope is None:
            # No transport — an in-process or stdio client. Treat as a fresh
            # depth-0 exchange and say so, rather than accepting an override.
            logger.info("run_task called with no call scope; assuming depth 0")

        return await execute_run(
            config,
            description,
            origin="mcp",
            conversation_id=scope.conversation_id if scope else "",
            depth=scope.depth if scope else 0,
            caller=scope.caller if scope else "local",
        )

    @mcp.tool(read_only=True)
    async def list_agents() -> str:
        """List the specialist agents available to run a task, with what each is for."""
        rows = await asyncio.to_thread(get_store().list_configs)
        if not rows:
            return "No agents are configured yet."
        attached = await asyncio.to_thread(get_store().connections_by_agent)
        lines = []
        for r in rows:
            summary = (r["system_prompt"] or "").strip().splitlines()
            headline = summary[0][:160] if summary else "(no description)"
            # Only the active ones: a connection that cannot be used is not a
            # capability, and naming it here would invite a caller to delegate a
            # task that is going to fail.
            live = [
                c["label"] or c["name"]
                for c in attached.get(r["id"], ())
                if c["status"] == "active"
            ]
            suffix = f" [connected: {', '.join(live)}]" if live else ""
            lines.append(f"- {r['slug']}: {r['name'] or r['slug']} — {headline}{suffix}")
        return "\n".join(lines)

    logger.info(
        "%s MCP server ready: %d tool(s), %d resource(s)",
        settings.app_name,
        len(mcp.tool_policies()),
        len(mcp.resource_policies()),
    )
    return mcp


@lru_cache
def get_mcp_server() -> AgentMCPServer:
    """Process-wide MCP server singleton.

    Cached because ``asgi_app()`` must be built exactly once — two calls would
    create two session managers, and the lifespan would run the one that is not
    serving traffic, so every request would fail in a way that looks like a
    protocol bug. Tests clear it with ``get_mcp_server.cache_clear()``.
    """
    return build_server()
