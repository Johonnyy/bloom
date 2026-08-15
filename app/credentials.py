"""Live credentials, resolved at the moment a tool is called.

The obvious design — decrypt every bound secret when the runner is built, hand the
plaintext to the tools — is wrong in a way that only shows up in production. A run
can last minutes; an access token can expire during it; and the failure lands
halfway through a task the user asked for. So nothing is decrypted up front. Each
synthesised tool holds a *connection id* and calls :meth:`CredentialResolver
.credential` when it fires, which reads, decrypts, and refreshes if needed.

**Two kinds of secret, one shape at the call site.** An ``oauth`` connection holds
a grant Bloom obtained and can refresh; an ``api_key`` connection holds a key the
user pasted, which never expires and has nothing to refresh with. They differ only
here: :meth:`credential` returns a :class:`Credential` with the secret already
placed in the header or query parameter the provider's manifest asks for, so
nothing downstream branches on where it came from.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.crypto import UndecryptableToken, decrypt, encrypt
from app.db import Store, get_store
from app.providers import ClientCredentials, client_for, get_provider

logger = logging.getLogger(__name__)

# How close to expiry counts as expired. The cost of refreshing early is one HTTP
# call; the cost of refreshing late is a task failing mid-run.
EXPIRY_SKEW_S = 60


@dataclass(frozen=True)
class Credential:
    """How to present one connection's secret on one outbound request.

    A value object rather than a token: the plaintext is already embedded in the
    header or parameter it belongs in, so a caller cannot accidentally log "the
    token" — there is no field called that.

    ``refreshable`` is the one thing a caller still needs to know about the kind:
    a 401 on a grant is worth one forced refresh and a retry, and a 401 on a static
    key is worth nothing but a clear message.
    """

    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    refreshable: bool = False

    def __bool__(self) -> bool:
        return bool(self.headers or self.params)


NO_CREDENTIAL = Credential()


def client_credentials(row: dict, settings: Settings) -> ClientCredentials:
    """The app registration a connection authorises against.

    Its own if it carries one — ``client_id`` in the connection's config, the
    secret in its own encrypted column — and this deployment's environment
    otherwise. ``row`` is a :meth:`Store.connection_secrets` projection.

    This lives here rather than in `app.providers` because resolving it needs the
    cipher, and the registry deliberately knows nothing about encryption.
    """
    provider = get_provider(str(row.get("provider") or ""))
    if provider is None:
        return ClientCredentials()
    try:
        secret = decrypt(row.get("client_secret"), settings)
    except UndecryptableToken:
        logger.exception("Client secret for connection %s cannot be decrypted", row.get("id"))
        secret = ""
    config = row.get("config") or {}
    return client_for(
        provider,
        client_id=str(config.get("client_id") or ""),
        client_secret=secret,
    )


def _present(row: dict, plaintext: str) -> Credential:
    """Put a decrypted secret where its provider expects to receive it."""
    if not plaintext:
        return NO_CREDENTIAL
    kind = row.get("kind")
    if kind == "oauth":
        return Credential({"Authorization": f"Bearer {plaintext}"}, {}, refreshable=True)
    if kind == "api_key":
        provider = get_provider(str(row.get("provider") or ""))
        if provider is None or provider.api_key is None:
            logger.warning(
                "Connection %s is an API key for %r, which declares no [api_key] block",
                row.get("id"),
                row.get("provider"),
            )
            return NO_CREDENTIAL
        headers, params = provider.api_key.apply(plaintext)
        return Credential(headers, params, refreshable=False)
    # kind='mcp' never reaches a synthesised tool: a peer's bearer is presented by
    # MCPClient at session open, not per call.
    return NO_CREDENTIAL


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
    """Reads, refreshes and hands out the secrets of stored connections."""

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
        # Cached alongside the plaintext so a cache hit can still say how to present
        # it. Holds no secret: the projection's ciphertext columns are never read
        # from here, only `kind` and `provider`.
        self._rows: dict[str, dict] = {}

    def _lock_for(self, connection_id: str) -> asyncio.Lock:
        return self._locks.setdefault(connection_id, asyncio.Lock())

    async def secret(self, connection_id: str, *, force_refresh: bool = False) -> str:
        """The usable plaintext, or ``""`` when the connection cannot provide one.

        Returning empty rather than raising is deliberate: the caller is a tool,
        and a tool that raises kills a turn where a sentence — "Spotify needs
        reconnecting" — lets the model tell the user something useful.
        """
        plaintext, _ = await self._resolve(connection_id, force_refresh=force_refresh)
        return plaintext

    async def credential(self, connection_id: str, *, force_refresh: bool = False) -> Credential:
        """The plaintext, already placed where its provider expects it."""
        plaintext, row = await self._resolve(connection_id, force_refresh=force_refresh)
        if not plaintext or row is None:
            return NO_CREDENTIAL
        return _present(row, plaintext)

    async def _resolve(
        self, connection_id: str, *, force_refresh: bool = False
    ) -> tuple[str, dict | None]:
        """``(plaintext, row)``. The row rides along so callers need no second read."""
        if not force_refresh:
            cached = self._cache.get(connection_id)
            if cached and cached[1] > time.monotonic():
                return cached[0], self._rows.get(connection_id)

        async with self._lock_for(connection_id):
            # Re-read inside the lock: another caller may have refreshed while we
            # waited, and using its result is the whole point of serialising.
            if not force_refresh:
                cached = self._cache.get(connection_id)
                if cached and cached[1] > time.monotonic():
                    return cached[0], self._rows.get(connection_id)

            row = await asyncio.to_thread(self._store.connection_secrets, connection_id)
            if row is None:
                logger.warning("No such connection: %s", connection_id)
                return "", None
            if row["status"] in {"revoked", "needs_reauth"}:
                return "", row

            try:
                plaintext = decrypt(row["secret"], self._settings)
                refresh = decrypt(row["refresh_token"], self._settings)
            except UndecryptableToken:
                logger.exception("Connection %s cannot be decrypted", connection_id)
                await self.mark_needs_reauth(connection_id)
                return "", row

            # Only a grant goes stale. An API key has no expiry and nothing to
            # refresh with, so an `expires_at` on one is honoured if an operator set
            # it and never invented if they did not.
            expiry = _parse_expiry(row["expires_at"])
            stale = (force_refresh and row["kind"] == "oauth") or (
                expiry is not None
                and expiry <= datetime.now(UTC) + timedelta(seconds=EXPIRY_SKEW_S)
            )

            if stale and refresh:
                plaintext = await self._refresh(connection_id, row, refresh)
            elif stale and not refresh:
                # Expired with no way back. Say so once, in the status, rather than
                # letting every tool call discover it by 401.
                logger.warning("Connection %s expired and cannot be refreshed", connection_id)
                await asyncio.to_thread(self._store.set_connection_status, connection_id, "expired")
                return "", row

            if plaintext:
                # Deliberately short: this exists to avoid decrypting once per tool
                # call within a single run, not to hold a secret across runs.
                self._cache[connection_id] = (plaintext, time.monotonic() + 30)
                self._rows[connection_id] = row
                await asyncio.to_thread(self._store.touch_connection, connection_id)
            return plaintext, row

    async def _refresh(self, connection_id: str, row: dict, refresh_token: str) -> str:
        """Exchange a refresh token. Returns the new access token, or ``""``.

        Authorises with *this connection's* app registration, falling back to the
        deployment's — the same resolution the flow used to obtain the grant, and
        it must be, since a refresh token is only valid for the client that issued
        it.
        """
        provider_name = str(row.get("provider") or "")
        provider = get_provider(provider_name)
        creds = client_credentials(row, self._settings) if provider else ClientCredentials()
        if provider is None or not creds:
            logger.error(
                "Cannot refresh %s: provider %r is unknown or has no client credentials",
                connection_id,
                provider_name,
            )
            return ""

        payload = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if provider.auth_style == "basic":
            raw = f"{creds.client_id}:{creds.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            payload["client_id"] = creds.client_id
            payload["client_secret"] = creds.client_secret

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
        self._rows.pop(connection_id, None)
        await asyncio.to_thread(self._store.set_connection_status, connection_id, "needs_reauth")
