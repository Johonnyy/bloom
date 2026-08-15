"""Refreshing access tokens before they lapse.

Two mechanisms, and both are needed.

This background sweep refreshes anything expiring inside
``BLOOM_OAUTH_REFRESH_SKEW_S`` so the common case — a token that quietly aged out
overnight — is already fixed before anyone asks for it. It cannot be the only
mechanism: a token can expire between the sweep and the call, the process can have
been down, and a run can outlive the window. `app.credentials.CredentialResolver`
therefore *also* checks expiry at call time, and both paths take the same
per-connection lock, so the two never race each other.

Nothing here raises. A provider being briefly unreachable must not stop the loop —
it would stop refreshing every other connection too, which turns a five-minute
outage into a service-wide reauth.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.credentials import CredentialResolver, _parse_expiry
from app.db import Store, get_store

logger = logging.getLogger(__name__)


async def refresh_due(
    store: Store | None = None,
    settings: Settings | None = None,
    *,
    resolver: CredentialResolver | None = None,
) -> int:
    """Refresh every connection expiring inside the skew window. Returns the count."""
    settings = settings or get_settings()
    store = store or get_store()
    resolver = resolver or CredentialResolver(store, settings)

    cutoff = datetime.now(UTC) + timedelta(seconds=settings.oauth_refresh_skew_s)
    rows = await asyncio.to_thread(store.refreshable_connections)

    refreshed = 0
    for row in rows:
        expiry = _parse_expiry(row["expires_at"])
        if expiry is None or expiry > cutoff:
            continue
        # Through the resolver, not a private helper: it holds the per-connection
        # lock, so a sweep and an in-flight run cannot both spend the same
        # rotating refresh token and permanently break the grant.
        if await resolver.access_token(row["id"], force_refresh=True):
            refreshed += 1
    if refreshed:
        logger.info("Proactively refreshed %d connection(s)", refreshed)
    return refreshed


async def refresh_loop(store: Store | None = None, settings: Settings | None = None) -> None:
    """Sweep forever. Started from the lifespan when OAuth is enabled."""
    settings = settings or get_settings()
    store = store or get_store()
    resolver = CredentialResolver(store, settings)
    logger.info("Token refresh loop started (every %.0fs)", settings.oauth_refresh_interval_s)
    while True:
        try:
            await refresh_due(store, settings, resolver=resolver)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad sweep must not end the loop
            logger.exception("Token refresh sweep failed")
        await asyncio.sleep(settings.oauth_refresh_interval_s)


def start_loop(store: Store, settings: Settings) -> asyncio.Task | None:
    """Start the sweep if OAuth is on, else return None."""
    if not settings.oauth_enabled:
        return None
    return asyncio.create_task(refresh_loop(store, settings), name="bloom-token-refresh")


async def stop_loop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
