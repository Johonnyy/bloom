"""CRUD for agent configurations — the first thing Aperture will talk to.

An `AgentConfig` is the whole point of Bloom: a name, a system prompt, a model
tier, a set of peer MCP servers, and (later) OAuth bindings. Defining one here is
what replaces building and deploying a standalone app for a single integration.

These request and response models *are* the contract Aperture generates a client
from, so they are written as the public surface they are rather than as thin
wrappers over table rows: the join table becomes an ``oauth_connections`` list, the
JSON column becomes a real array, and nothing about the storage shape leaks.

**Validation is strict where a mistake is expensive and advisory where it is
not.** An unknown model tier is rejected: it would fail at the first run with an
error from inside the runtime, far from the edit that caused it. An unknown peer
name is only warned about, because a peer may legitimately register *after* the
config that names it — rejecting would make configuration order load-bearing.
"""

from __future__ import annotations

import asyncio
import logging

from agent_runtime.model_router import UnknownTier
from agent_runtime.model_router import resolve as resolve_tier
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.admin.deps import require_admin
from app.db import SlugTaken, Store, get_store
from app.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/agents", tags=["agents"], dependencies=[Depends(require_admin)])

_SLUG = r"^[a-z][a-z0-9-]{0,63}$"


def _validate_tier(value: str) -> str:
    """Accept a named tier or a literal ``vendor/model`` id, reject anything else.

    ``resolve`` passes anything containing "/" through untouched — that is the
    documented escape hatch for a model the tier table does not name yet — and
    raises `UnknownTier` for a bare word it does not know.
    """
    try:
        resolve_tier(value)
    except UnknownTier as exc:
        raise ValueError(str(exc)) from exc
    return value


class AgentConfigIn(BaseModel):
    """Everything a caller may set when creating a config."""

    slug: str = Field(
        pattern=_SLUG, description="How a caller names this agent, e.g. 'spotify-dj'."
    )
    name: str = ""
    system_prompt: str = ""
    model_tier: str = "balanced"
    mcp_servers: list[str] = Field(default_factory=list)
    # NULL means "use the service ceiling". A config may lower these; the
    # service-wide values in app/config.py are the ceiling it cannot raise, which
    # is enforced where the runner is built, not here — a config edited before a
    # ceiling changes should not have to be re-saved.
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_cost_usd: float | None = Field(default=None, gt=0)

    @field_validator("model_tier")
    @classmethod
    def _tier(cls, value: str) -> str:
        return _validate_tier(value)


class AgentConfigPatch(BaseModel):
    """A partial update. Every field optional; ``None`` means "leave alone"."""

    slug: str | None = Field(default=None, pattern=_SLUG)
    name: str | None = None
    system_prompt: str | None = None
    model_tier: str | None = None
    mcp_servers: list[str] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_cost_usd: float | None = Field(default=None, gt=0)

    @field_validator("model_tier")
    @classmethod
    def _tier(cls, value: str | None) -> str | None:
        return _validate_tier(value) if value is not None else None


class AgentConfigOut(BaseModel):
    id: str
    slug: str
    name: str
    system_prompt: str
    model_tier: str
    mcp_servers: list[str]
    oauth_connections: list[str]
    max_steps: int | None
    max_cost_usd: float | None
    created_at: str
    updated_at: str


def _warn_unknown_peers(names: list[str]) -> None:
    """Log peers Bloom cannot currently resolve. Advisory only — see the docstring.

    Imported lazily: `agent_mcp.registry` reads a process-wide registry that is
    only populated once the MCP server's lifespan has run, and a config edit must
    not depend on that having happened.
    """
    if not names:
        return
    try:
        from agent_mcp.registry import known_peers

        unknown = [n for n in names if n not in set(known_peers())]
    except Exception:  # noqa: BLE001 — discovery is advisory; never fail an edit on it
        logger.debug("Peer check skipped: registry unavailable")
        return
    if unknown:
        logger.warning(
            "Config names peer(s) not currently registered: %s. This is allowed — "
            "a peer may register later — but a run will skip them until it does.",
            ", ".join(unknown),
        )


async def _out(store: Store, row: dict) -> AgentConfigOut:
    """Project a stored row into the wire shape, filling in its bindings."""
    bound = await asyncio.to_thread(store.connection_ids_for, row["id"])
    return AgentConfigOut(**row, oauth_connections=bound)


@router.post("", response_model=AgentConfigOut, status_code=201)
async def create_agent(body: AgentConfigIn) -> AgentConfigOut:
    """Define a new agent."""
    store = get_store()
    _warn_unknown_peers(body.mcp_servers)
    try:
        row = await asyncio.to_thread(
            store.create_config,
            slug=body.slug,
            name=body.name or body.slug,
            system_prompt=body.system_prompt,
            model_tier=body.model_tier,
            mcp_servers=body.mcp_servers,
            max_steps=body.max_steps,
            max_cost_usd=body.max_cost_usd,
        )
    except SlugTaken as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    logger.info("Created agent config %s (%s)", row["slug"], row["id"])
    return await _out(store, row)


@router.get("", response_model=list[AgentConfigOut])
async def list_agents() -> list[AgentConfigOut]:
    """Every configured agent, newest first."""
    store = get_store()
    rows = await asyncio.to_thread(store.list_configs)
    return [await _out(store, r) for r in rows]


@router.get("/{config_id}", response_model=AgentConfigOut)
async def get_agent(config_id: str) -> AgentConfigOut:
    store = get_store()
    row = await asyncio.to_thread(store.get_config, config_id)
    if row is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    return await _out(store, row)


@router.patch("/{config_id}", response_model=AgentConfigOut)
async def update_agent(config_id: str, body: AgentConfigPatch) -> AgentConfigOut:
    store = get_store()
    if body.mcp_servers is not None:
        _warn_unknown_peers(body.mcp_servers)
    try:
        row = await asyncio.to_thread(
            store.update_config, config_id, **body.model_dump(exclude_unset=True)
        )
    except SlugTaken as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    if row is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    return await _out(store, row)


@router.delete("/{config_id}", status_code=204)
async def delete_agent(config_id: str) -> None:
    """Delete a config, its bindings, and any OAuth connection it owns.

    Connections marked shared (no owning config) survive deliberately — see
    `app.db.Store.delete_config`.
    """
    store = get_store()
    if not await asyncio.to_thread(store.delete_config, config_id):
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    logger.info("Deleted agent config %s", config_id)
