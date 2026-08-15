"""FastAPI application: two surfaces, one process.

* ``/admin/*`` — REST, consumed by Aperture. CRUD over agent configurations, test
  runs, run history and traces, OAuth connect flows.
* ``/mcp`` — Bloom's MCP server, how *other agents* delegate a task, with
  ``/agent/usage`` beside it for per-call cost and latency.

They are deliberately different protocols for deliberately different callers. MCP
exists so a model can discover and choose a tool; a GUI managing configuration
needs neither of those things and is better served by plain REST with an OpenAPI
schema it can generate a client from.

With ``BLOOM_FEATURE_MCP=false`` (or no keys) this degrades to the admin API alone
and never imports the agent stack — the ecosystem's core principle survives even in
the one service whose whole purpose is the agent layer, because a config editor
that runs without an OpenRouter key is genuinely useful while you set one up.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.agents import router as agents_router
from app.admin.oauth_routes import public_router as oauth_public_router
from app.admin.oauth_routes import router as oauth_router
from app.admin.runs import router as runs_router
from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.health import router as health_router

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(settings.app_name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP server's own lifecycle alongside the HTTP app.

    This is mandatory, not decorative: a mounted sub-app's lifespan never runs, so
    unless the host enters it here the MCP session manager is never started and the
    very first MCP request fails. `agent_mcp` bundles that, sync-store registration
    and the heartbeat into one context manager.

    When MCP is disabled this is an empty context and the admin API is unaffected.

    Four things happen either way, before MCP is considered:

    * **The encryption key is checked** — but only fatally when connections are
      already stored. Booting with unreadable credentials would surface days later
      as a run failing for no visible reason; booting *without* a key and without
      any connections is a legitimate fresh install.
    * **Abandoned runs are swept.** A process that dies mid-run leaves a row
      claiming to be ``running`` and a trace with no terminal event, so any client
      tailing it hangs forever. The sweep closes both.
    * **The trace writer starts.** It is the single task that serialises every
      trace write off the thread running a model loop.
    * **The token refresh loop starts**, if OAuth is configured.
    """
    from app.crypto import assert_usable
    from app.db import get_store
    from app.oauth.refresh import start_loop, stop_loop
    from app.trace import get_writer

    settings_now = get_settings()
    store = get_store()

    assert_usable(settings_now, stored_connections=await asyncio.to_thread(store.count_connections))
    await asyncio.to_thread(store.sweep_abandoned_runs)
    writer = get_writer(store)
    await writer.start()
    refresher = start_loop(store, settings_now)

    try:
        if not get_settings().mcp_enabled:
            logger.info("MCP server disabled (BLOOM_FEATURE_MCP off, or no BLOOM_MCP_KEYS)")
            yield
            return

        # Imported here, not at module scope, so a deploy with the flag off never
        # pulls in the MCP server at all.
        from app.mcp import get_mcp_server

        async with get_mcp_server().lifespan():
            logger.info("MCP server mounted at /mcp")
            yield
    finally:
        await stop_loop(refresher)
        # Drains what is queued before stopping, so the last events of a run that
        # finished during shutdown are not lost.
        await writer.stop()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router)
app.include_router(agents_router)
app.include_router(runs_router)
app.include_router(oauth_router)
# Unauthenticated by design — the provider redirects a browser here, which carries
# no bearer token. Its security is a single-use state row plus PKCE. See
# app/admin/oauth_routes.py; do not add a second route to this router.
app.include_router(oauth_public_router)


def _mount_mcp(app: FastAPI, settings: Settings) -> None:
    """Attach the MCP server and its usage endpoint, if enabled.

    Routes rather than ``app.mount``: ``Mount("/mcp")`` matches ``/mcp/...`` but not
    a bare ``POST /mcp``, so the host router answers that with a 307 which real MCP
    clients do not follow — it surfaces as an unhelpful "unexpected content type"
    error. ``routes()`` claims both forms.
    """
    if not settings.mcp_enabled:
        return
    from app.mcp import get_mcp_server

    mcp = get_mcp_server()
    app.router.routes.extend(mcp.routes())
    app.router.routes.extend(mcp.usage_routes())


_mount_mcp(app, settings)
