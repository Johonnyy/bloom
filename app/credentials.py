"""Live access tokens, resolved at the moment a tool is called.

The obvious design — decrypt every bound token when the runner is built, hand the
plaintext to the tools — is wrong in a way that only shows up in production. A run
can last minutes; an access token can expire during it; and the failure lands
halfway through a task the user asked for. So nothing is decrypted up front. Each
synthesised tool holds a *connection id* and calls :meth:`CredentialResolver
.access_token` when it fires, which reads, decrypts, and refreshes if needed.

**Refresh is serialised per connection, and re-reads after taking the lock.** Two
tools on one connection can fire concurrently. For a provider whose refresh tokens
rotate — Spotify, Google, Dropbox; the ``refresh_rotates`` flag in the manifest —
losing that race does not merely waste a request, it *permanently breaks the
connection*: the loser presents a refresh token the provider has already retired
and gets an invalid_grant it can never recover from. The re-read inside the lock
is what makes the second caller use the winner's new token instead of racing it.

That correctness is single-process. If Bloom ever runs multiple uvicorn workers
this needs a row claim in SQL (``UPDATE … WHERE refreshing_at IS NULL``) rather
than an in-process lock; the comment is here so that is a decision rather than a
surprise.

A short cache sits in front, keyed by connection id, because a single run may call
several tools on one provider and each read is a decrypt.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.crypto import UndecryptableToken, decrypt, encrypt
from app.db import Store, get_store
from app.providers import get_provider

logger = logging.getLogger(__name__)

# How close to expiry counts as expired. The cost of refreshing early is one HTTP
# call; the cost of refreshing late is a task failing mid-run.
EXPIRY_SKEW_S = 60


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _expiry_from_now(expires_in: float | int | None) -> str | None:
    if not expires_in:
        return None
    when = datetime.now(UTC) + timedelta(seconds=float(expires_in))
    return when.replace(microsecond=0).isoformat()


class CredentialResolver:
    """Reads, refreshes and hands out access tokens for stored connections."""

    def __init__(
        self,
        store: Store | None = None,
        settings: Settings | None = None,
        *,
        http_client_factory=None,
    ) -> None:
        self._store = store or get_store()
        self._settings = settings or get_settings()
        self._factory = http_client_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, tuple[str, float]] = {}

    def _lock_for(self, connection_id: str) -> asyncio.Lock:
        return self._locks.setdefault(connection_id, asyncio.Lock())

    async def access_token(self, connection_id: str, *, force_refresh: bool = False) -> str:
        """A usable access token, or ``""`` when the connection cannot provide one.

        Returning empty rather than raising is deliberate: the caller is a tool,
        and a tool that raises kills a turn where a sentence — "Spotify needs
        reconnecting" — lets the model tell the user something useful.
        """
        if not force_refresh:
            cached = self._cache.get(connection_id)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        async with self._lock_for(connection_id):
            # Re-read inside the lock: another caller may have refreshed while we
            # waited, and using its result is the whole point of serialising.
            if not force_refresh:
                cached = self._cache.get(connection_id)
                if cached and cached[1] > time.monotonic():
                    return cached[0]

            row = await asyncio.to_thread(self._store.connection_secrets, connection_id)
            if row is None:
                logger.warning("No such OAuth connection: %s", connection_id)
                return ""
            if row["status"] in {"revoked", "needs_reauth"}:
                return ""

            try:
                access = decrypt(row["access_token"], self._settings)
                refresh = decrypt(row["refresh_token"], self._settings)
            except UndecryptableToken:
                logger.exception("Connection %s cannot be decrypted", connection_id)
                await self.mark_needs_reauth(connection_id)
                return ""

            expiry = _parse_expiry(row["expires_at"])
            stale = force_refresh or (
                expiry is not None
                and expiry <= datetime.now(UTC) + timedelta(seconds=EXPIRY_SKEW_S)
            )

            if stale and refresh:
                access = await self._refresh(connection_id, row["provider"], refresh)
            elif stale and not refresh:
                # Expired with no way back. Say so once, in the status, rather than
                # letting every tool call discover it by 401.
                logger.warning("Connection %s expired and has no refresh token", connection_id)
                await asyncio.to_thread(self._store.set_connection_status, connection_id, "expired")
                return ""

            if access:
                # Deliberately short: this exists to avoid decrypting once per tool
                # call within a single run, not to hold a token across runs.
                self._cache[connection_id] = (access, time.monotonic() + 30)
                await asyncio.to_thread(self._store.touch_connection, connection_id)
            return access

    async def _refresh(self, connection_id: str, provider_name: str, refresh_token: str) -> str:
        """Exchange a refresh token. Returns the new access token, or ``""``."""
        provider = get_provider(provider_name)
        if provider is None or not provider.configured:
            logger.error(
                "Cannot refresh %s: provider %r is unknown or has no client credentials",
                connection_id,
                provider_name,
            )
            return ""

        payload = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if provider.auth_style == "basic":
            raw = f"{provider.client_id}:{provider.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            payload["client_id"] = provider.client_id
            payload["client_secret"] = provider.client_secret

        import httpx2

        factory = self._factory or httpx2.AsyncClient
        try:
            async with factory() as client:
                response = await client.post(
                    provider.token_url, data=payload, headers=headers, timeout=15.0
                )
        except Exception:  # noqa: BLE001 — a network blip is not a revoked grant
            logger.exception("Refresh request failed for %s", connection_id)
            return ""

        if response.status_code >= 400:
            # Only a 4xx means the grant itself is gone. A 5xx is the provider
            # having a bad day, and marking the connection dead for that would
            # need a human to reconnect something that was never broken.
            if response.status_code < 500:
                logger.warning(
                    "Refresh refused for %s (HTTP %s); marking it for reauth",
                    connection_id,
                    response.status_code,
                )
                await self.mark_needs_reauth(connection_id)
            else:
                logger.warning(
                    "Refresh unavailable for %s (HTTP %s)", connection_id, response.status_code
                )
            return ""

        data = response.json()
        access = data.get("access_token", "")
        if not access:
            logger.error("Refresh for %s returned no access_token", connection_id)
            return ""

        rotated = data.get("refresh_token")
        await asyncio.to_thread(
            self._store.update_connection_tokens,
            connection_id,
            access_token=encrypt(access, self._settings),
            # None leaves the stored one alone. A provider that does not rotate
            # simply omits the field, and treating that as a revocation would
            # brick the connection on its first successful refresh.
            refresh_token=encrypt(rotated, self._settings) if rotated else None,
            expires_at=_expiry_from_now(data.get("expires_in")),
        )
        logger.info("Refreshed %s connection %s", provider_name, connection_id)
        return access

    async def mark_needs_reauth(self, connection_id: str) -> None:
        self._cache.pop(connection_id, None)
        await asyncio.to_thread(self._store.set_connection_status, connection_id, "needs_reauth")
