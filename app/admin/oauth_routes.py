"""Connecting, inspecting and disconnecting a provider account.

**Two routers, and the split is a security boundary rather than a tidy-up.**
Everything a GUI calls is bearer-authenticated. The callback cannot be: it is
reached by the provider redirecting a *browser*, which carries no bearer token and
never will. So it lives on its own unauthenticated router, and its security is the
single-use ``state`` row minted at ``/start`` plus the PKCE verifier that never
travels through the browser.

The one thing not to do here is add a route to ``public_router`` because it was
convenient. A route under ``/admin`` that skips the admin dependency is exactly the
kind of thing that gets copy-pasted, so there is one, it is named, and this
paragraph exists.

**No token value is ever returned.** The status endpoint answers with provider,
state, scopes and last-used. Aperture has no use for a token and every reason not
to hold one.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.admin.deps import require_admin
from app.config import get_settings
from app.db import get_store
from app.errors import ApiError
from app.oauth.flow import OAuthError, completion_page, exchange, start
from app.providers import get_provider, providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["oauth"], dependencies=[Depends(require_admin)])

# Unauthenticated ON PURPOSE — see the module docstring. Exactly one route.
public_router = APIRouter(prefix="/admin/oauth", tags=["oauth"], include_in_schema=True)


class StartIn(BaseModel):
    # Omitted means the manifest's defaults. Narrowing them is supported; a config
    # that only needs to read playback should not be asking for write access.
    scopes: list[str] | None = None


class StartOut(BaseModel):
    authorize_url: str
    state: str
    expires_at: str
    scopes: list[str]
    redirect_uri: str


def _require_provider(name: str):
    provider = get_provider(name)
    if provider is None:
        known = ", ".join(sorted(providers())) or "none"
        raise ApiError(404, "not_found", f"No manifest for provider {name!r}. Known: {known}.")
    return provider


def _require_oauth_enabled() -> None:
    settings = get_settings()
    if not settings.oauth_enabled:
        raise ApiError(
            503,
            "unavailable",
            "OAuth is not configured. Set BLOOM_FEATURE_OAUTH=true and "
            "BLOOM_FERNET_KEYS — storing a token without a key is not a degraded "
            "mode, so this refuses rather than falling back to plaintext.",
        )


@router.get("/oauth/providers")
async def list_providers() -> list[dict]:
    """Every provider Bloom has a manifest for, and whether it can be connected.

    ``configured`` is the difference between "a manifest ships in the image" and
    "client credentials exist in this deployment" — which is what lets Aperture
    grey out a Connect button with a reason rather than failing at the redirect.

    The two ``*_env`` names are what make that reason *actionable*. Adding a provider
    is meant to be one file in ``app/providers/`` and nothing else anywhere — but until
    these were reported, the deployment tooling had to enumerate every provider's
    client credentials by hand, so every new connection needed an amber-infra release
    to go with it. The manifest already declares which variables it reads; saying so
    over the API is what lets Aperture offer to fill them without knowing in advance
    that this provider exists.

    Names, never values. They are public — committed in the provider's own TOML — and
    the value is read from the process environment and stays there.
    """
    return [
        {
            "name": p.name,
            "display_name": p.display_name,
            "configured": p.configured,
            "client_id_env": p.client_id_env,
            "client_secret_env": p.client_secret_env,
            "scopes_default": list(p.scopes_default),
            "operations": [op.tool_name(p.name) for op in p.operations],
            "docs_url": p.docs_url,
        }
        for p in sorted(providers().values(), key=lambda p: p.name)
    ]


@router.get("/agents/{config_id}/oauth")
async def connection_status(config_id: str) -> list[dict]:
    """This config's connections: provider, state, scopes, last used. No tokens."""
    store = get_store()
    if await asyncio.to_thread(store.get_config, config_id) is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    rows = await asyncio.to_thread(store.connections_for, config_id)
    return [
        {
            "id": r["id"],
            "provider": r["provider"],
            "status": r["status"],
            "scopes": r["scopes"],
            "expires_at": r["expires_at"],
            "last_used_at": r["last_used_at"],
            "shared": r["agent_config_id"] is None,
        }
        for r in rows
    ]


@router.post("/agents/{config_id}/oauth/{provider_name}/start", response_model=StartOut)
async def start_connect(
    config_id: str, provider_name: str, body: StartIn | None = None
) -> StartOut:
    """Begin a connection. Returns the URL Aperture should open in a browser."""
    _require_oauth_enabled()
    provider = _require_provider(provider_name)
    store = get_store()
    if await asyncio.to_thread(store.get_config, config_id) is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")

    try:
        return StartOut(
            **await asyncio.to_thread(
                start, store, provider, config_id, scopes=(body.scopes if body else None)
            )
        )
    except OAuthError as exc:
        # 503 rather than 400: nothing about the *request* was wrong, the service
        # is missing configuration only an operator can supply.
        raise ApiError(503, "unavailable", str(exc)) from exc


@router.delete("/agents/{config_id}/oauth/{provider_name}", status_code=204)
async def disconnect(config_id: str, provider_name: str) -> None:
    """Revoke a connection and zero its tokens.

    The row survives, marked ``revoked``, so the agent's page can still show
    "Spotify — disconnected" rather than the provider silently disappearing.
    """
    _require_provider(provider_name)
    if not await asyncio.to_thread(get_store().revoke_connection, config_id, provider_name):
        raise ApiError(
            404, "not_found", f"Agent {config_id!r} has no {provider_name!r} connection."
        )
    logger.info("Revoked %s for agent config %s", provider_name, config_id)


@public_router.get("/{provider_name}/callback", response_class=HTMLResponse)
async def callback(
    provider_name: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Where the provider sends the browser. **Deliberately unauthenticated.**

    Always answers with a page a human can read, never a JSON error: whatever went
    wrong, the person is sitting in front of a browser tab and needs a sentence,
    not an envelope.
    """
    provider = get_provider(provider_name)
    if provider is None:
        return HTMLResponse(
            completion_page(_Unknown(provider_name), "error", "Unknown provider."),
            status_code=404,
        )

    if error:
        # The user pressed Deny, or the provider refused. Not an incident.
        logger.info("OAuth callback for %s returned error=%s", provider_name, error)
        return HTMLResponse(
            completion_page(provider, "error", f"The authorization was not completed ({error})."),
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            completion_page(provider, "error", "The provider's redirect was missing a code."),
            status_code=400,
        )

    # Single use: read and delete in one transaction, so replaying a captured
    # state cannot bind a second connection.
    state_row = await asyncio.to_thread(get_store().consume_oauth_state, state)
    if state_row is None or state_row["provider"] != provider_name:
        logger.warning("OAuth callback presented an unknown or expired state")
        return HTMLResponse(
            completion_page(
                provider,
                "error",
                "This link has expired or was already used. Start the connection again "
                "from Aperture.",
            ),
            status_code=400,
        )

    try:
        await exchange(get_store(), provider, code, state_row)
    except OAuthError as exc:
        logger.warning("OAuth exchange failed for %s: %s", provider_name, exc)
        return HTMLResponse(completion_page(provider, "error", str(exc)), status_code=400)
    except Exception:  # noqa: BLE001 — the user is in a browser; answer with prose
        logger.exception("OAuth exchange for %s failed unexpectedly", provider_name)
        return HTMLResponse(
            completion_page(provider, "error", "Something went wrong completing the connection."),
            status_code=500,
        )

    return HTMLResponse(completion_page(provider, "success"))


class _Unknown:
    """Just enough of a Provider to render the failure page for an unknown name."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
