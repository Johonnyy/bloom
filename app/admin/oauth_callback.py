"""The one route a browser reaches, and the only unauthenticated one in Bloom.

Everything a GUI calls lives in `app.admin.connections` behind the admin bearer.
This cannot: it is reached by the provider redirecting a *browser*, which carries
no bearer token and never will. So it sits on its own router, and its security is
the single-use ``state`` row minted at ``/start`` plus a PKCE verifier that never
travels through the browser at all.

**Do not add a second route to ``public_router``.** A route under ``/admin`` that
skips the admin dependency is exactly the kind of thing that gets copy-pasted, so
there is one, this file is named after it, and this paragraph exists. The file used
to also hold the whole authenticated OAuth surface, which is precisely how a second
one would have arrived.

Every answer is a page a human can read, never a JSON envelope: whatever went
wrong, the person is sitting in front of a browser tab and needs a sentence.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.credentials import client_credentials
from app.db import get_store
from app.oauth.flow import OAuthError, completion_page, exchange
from app.providers import get_provider

logger = logging.getLogger(__name__)

# Unauthenticated ON PURPOSE — see the module docstring. Exactly one route.
public_router = APIRouter(prefix="/admin/oauth", tags=["oauth"], include_in_schema=True)


@public_router.get("/{provider_name}/callback", response_class=HTMLResponse)
async def callback(
    provider_name: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Where the provider sends the browser. **Deliberately unauthenticated.**"""
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

    # The connection carries the app registration this code was issued to, so it is
    # read here rather than assumed from the environment. Checked before the
    # exchange because a connection deleted mid-flow should cost a sentence, not a
    # round trip to the provider that can only fail.
    row = await asyncio.to_thread(get_store().connection_secrets, state_row["connection_id"])
    if row is None:
        return HTMLResponse(
            completion_page(
                provider,
                "error",
                "This connection no longer exists — it was deleted while you were "
                "approving. Create it again in Aperture and reconnect.",
            ),
            status_code=400,
        )

    try:
        await exchange(
            get_store(),
            provider,
            code,
            state_row,
            client=client_credentials(row, get_settings()),
        )
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
