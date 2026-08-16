"""The connection library: everything an agent can act through.

One vocabulary for what used to be two. An agent's reach was half an
``mcp_servers`` list of bare peer names on the config row and half a set of OAuth
connections owned by that config — so a peer could not carry a credential, a
credential could not be shared, and neither could be an API key at all.

A **connection** is a row here, and there are three kinds:

* ``oauth`` — a grant Bloom obtains through the browser and refreshes for you.
* ``api_key`` — a key you paste. Same provider manifest, same synthesised tools;
  only where the secret comes from differs.
* ``mcp`` — a peer server, by URL. Its ``name`` is the tool namespace
  (``<name>__<tool>``), which is why it is validated like one.

**Connections are global and never owned.** Creating one from an agent's page
attaches it as a convenience; the row belongs to the library, any other agent can
attach the same one, and deleting an agent deletes none of them. That was already
the shape of the schema — there was a binding table and a ``shared`` flag — but the
flag could never be set, because the OAuth exchange always stamped an owner.

**No secret is ever returned.** Responses carry ``has_secret`` /
``has_client_secret`` booleans, which is what a UI actually wanted. ``client_id``
*is* returned: it is not a secret — it travels in every authorize URL — and a user
has to be able to see which app registration a connection is bound to.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.admin.deps import require_admin
from app.config import Settings, get_settings
from app.credentials import CredentialResolver, client_credentials
from app.crypto import encrypt
from app.db import ConnectionNameTaken, Store, get_store
from app.errors import ApiError
from app.oauth.flow import OAuthError, redirect_uri, start
from app.providers import Provider, get_provider, providers, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["connections"], dependencies=[Depends(require_admin)])

# Same rule as a manifest's provider name, and for the same reason: for kind='mcp'
# this string is the namespace MCPClient prefixes onto every remote tool, so a '__'
# in it makes `<server>__<tool>` split in the wrong place. 24 characters leaves
# budget for the tool half inside a 64-character function name.
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

KINDS = ("oauth", "api_key", "mcp")


class ConnectionIn(BaseModel):
    """What creating a connection takes. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="oauth | api_key | mcp")
    provider: str | None = Field(
        default=None, description="Manifest name. Required for oauth/api_key, absent for mcp."
    )
    name: str | None = Field(
        default=None,
        description="Stable handle. Defaults to the provider name; required for mcp, "
        "where it is also the tool namespace.",
    )
    label: str = ""
    config: dict = Field(
        default_factory=dict, description="Non-secret settings. For mcp: {'url': ...}."
    )
    scopes: list[str] = Field(default_factory=list)
    # Write-only, all three. They are never echoed back by any route.
    secret: str | None = Field(default=None, description="An API key, or a peer's bearer token.")
    client_id: str | None = None
    client_secret: str | None = None
    attach_to: list[str] = Field(
        default_factory=list,
        description="Agent ids to attach this to, in the same transaction as the create.",
    )

    @model_validator(mode="after")
    def _coherent(self) -> ConnectionIn:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")

        if self.kind == "mcp":
            if self.provider:
                raise ValueError("an mcp connection has no provider — it is a URL, not a manifest")
            if not self.name:
                raise ValueError("an mcp connection needs a name; it is the tool namespace")
            if not str(self.config.get("url") or "").strip():
                raise ValueError("an mcp connection needs config.url")
            if self.client_id or self.client_secret:
                raise ValueError("client credentials belong to an oauth connection, not a peer")
        else:
            if not self.provider:
                article = "an" if self.kind[0] in "aeiou" else "a"
                raise ValueError(f"{article} {self.kind} connection needs a provider")
            if self.config.get("url"):
                raise ValueError("a provider connection's endpoint comes from its manifest")

        name = self.name or self.provider or ""
        if not _NAME.match(name) or "__" in name:
            raise ValueError(
                f"name {name!r} must be lowercase, start with a letter, be at most 24 "
                "characters, and contain no '__' (it would collide with MCP's "
                "<server>__<tool> namespacing)"
            )
        self.name = name
        return self


class ConnectionPatch(BaseModel):
    """Editable, non-secret fields.

    ``kind`` and ``provider`` are absent deliberately: both would invalidate the
    stored secret. So is every secret — a PATCH carrying one is a 422 rather than a
    plaintext key in a request log, and rotation goes through ``POST /{id}/secret``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    label: str | None = None
    config: dict | None = None
    scopes: list[str] | None = None

    @model_validator(mode="after")
    def _valid_name(self) -> ConnectionPatch:
        if self.name is not None and (not _NAME.match(self.name) or "__" in self.name):
            raise ValueError(f"name {self.name!r} is not a valid connection name")
        return self


class SecretIn(BaseModel):
    """Paste or rotate a connection's credentials.

    The user's secret and the app's rotate on completely different schedules — the
    first when it is reissued, the second when you roll it in the provider's
    console — so setting one leaves the other alone.

    ``client_id`` is not a secret and lives in ``config``, but it is accepted here
    because it is not independently settable: `registry.client_for` refuses to mix a
    connection's id with the environment's secret, so the two halves of an app
    registration are one intent and belong in one call. Sending the id alone is
    still allowed — that is how you correct a typo without re-reading the secret out
    of a password manager.

    **Omitted and empty are different, and only for ``client_id``.** Omitted means
    leave it alone; ``""`` means clear it, which is how a connection goes back to
    this deployment's default — a state you have to be able to return to and not
    only leave. An empty *secret* is refused outright: it is always a bug at the
    caller, and storing it would look exactly like a stored credential.
    """

    model_config = ConfigDict(extra="forbid")

    secret: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    @model_validator(mode="after")
    def _something(self) -> SecretIn:
        if not self.model_fields_set & {"secret", "client_id", "client_secret"}:
            raise ValueError("give a secret, a client_id or a client_secret")
        if self.secret == "" or self.client_secret == "":
            raise ValueError("a secret cannot be empty; omit it to leave the stored one alone")
        return self


class AttachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    position: int | None = Field(
        default=None,
        description="Broker priority. Defaults to the end, so attaching never "
        "reorders what an agent already had.",
    )


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omitted means the connection's stored scopes, then the manifest's defaults —
    # so a reconnect button can post an empty body and get the same grant back.
    scopes: list[str] | None = None


class StartOut(BaseModel):
    authorize_url: str
    state: str
    expires_at: str
    scopes: list[str]
    redirect_uri: str


class ProbeOut(BaseModel):
    """What a test told us, and which test it was.

    ``checked`` matters: "the credential decrypts" and "the provider accepted it"
    are very different claims, and reporting the weaker one as the stronger is how
    a green tick comes to mean nothing.
    """

    ok: bool
    checked: str  # token | request | session
    status: int | None = None
    detail: str = ""
    tools: list[str] = Field(default_factory=list)


class ConnectionOut(BaseModel):
    id: str
    kind: str
    provider: str | None
    name: str
    label: str
    status: str
    config: dict
    has_secret: bool
    has_client_secret: bool
    scopes: list[str]
    expires_at: str | None
    last_used_at: str | None
    created_at: str
    updated_at: str
    agent_ids: list[str]
    tools: list[str]


# --- helpers -------------------------------------------------------------------


def _tools_for(row: dict) -> list[str]:
    """What attaching this connection actually gives an agent.

    The question a picker exists to answer, and the reason it is computed here
    rather than guessed by a client: for a provider it is exactly the set
    `runtime_service` would register, scope filter included. For a peer Bloom
    cannot know without connecting, so it answers with the namespace and
    ``POST /{id}/test`` returns the real list.
    """
    if row["kind"] == "mcp":
        return [f"{row['name']}__*"]
    provider = get_provider(row.get("provider") or "")
    if provider is None:
        return []
    granted = row["scopes"] if row["kind"] == "oauth" else (row["scopes"] or None)
    return [op.tool_name(provider.name) for op in registry.operations_for(provider, granted)]


async def _out(store: Store, row: dict) -> ConnectionOut:
    agents = await asyncio.to_thread(store.agents_for_connection, row["id"])
    return ConnectionOut(**row, agent_ids=[a["id"] for a in agents], tools=_tools_for(row))


async def _require_connection(store: Store, connection_id: str) -> dict:
    row = await asyncio.to_thread(store.get_connection, connection_id)
    if row is None:
        raise ApiError(404, "not_found", f"No connection with id {connection_id!r}.")
    return row


async def _require_agent(store: Store, config_id: str) -> dict:
    row = await asyncio.to_thread(store.get_config, config_id)
    if row is None:
        raise ApiError(404, "not_found", f"No agent config with id {config_id!r}.")
    return row


def _require_provider_supporting(name: str, kind: str) -> Provider:
    provider = get_provider(name)
    if provider is None:
        known = ", ".join(sorted(providers())) or "none"
        raise ApiError(404, "not_found", f"No manifest for provider {name!r}. Known: {known}.")
    if not provider.supports(kind):
        raise ApiError(
            422,
            "unprocessable",
            f"{provider.display_name} cannot be connected with "
            f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} credential. "
            f"It supports: {', '.join(provider.auth_methods)}.",
        )
    return provider


def _require_secrets_storable() -> None:
    """Refuse to store a secret without a key to encrypt it with.

    Not a degraded mode — writing a credential in the clear is the breach the
    encryption exists to prevent — so this reports 503 rather than falling back.
    """
    if not get_settings().oauth_enabled:
        raise ApiError(
            503,
            "unavailable",
            "Storing credentials is not configured. Set BLOOM_FEATURE_OAUTH=true and "
            "BLOOM_FERNET_KEYS. A secret stored without a key is not a degraded mode, "
            "so this refuses rather than writing it in the clear.",
        )


# --- what this deployment can create -------------------------------------------
#
# Declared before /connections/{connection_id}, and it must stay that way: FastAPI
# matches in declaration order, so the other way round every call to this route
# 404s with "No connection with id 'kinds'".


@router.get("/connections/kinds")
async def connection_kinds() -> dict:
    """The kinds and providers this build offers, so a UI never ships a stale list.

    ``available`` carries the reason a kind cannot be used *before* the user picks
    it, rather than as a 503 at the moment the browser was meant to open.

    ``has_deployment_default`` says whether this box carries client credentials for
    a provider. It is a hint that lets the create form prefill — not a gate: a
    connection may bring its own, which is what makes connecting an account
    something a user can do rather than a deployment operation.

    ``redirect_uri`` is the other half of that. Registering an app at the provider
    requires pasting Bloom's callback address into their console, byte for byte — it
    is compared exactly, at both the authorize step and the token exchange — and it
    was previously discoverable only by reading `flow.redirect_uri` or by succeeding
    at a flow that cannot succeed until it is registered. Reporting it here is what
    lets a UI show the string to copy *before* anything is attempted. ``public_url``
    rides alongside so a client can tell "this provider has no browser flow" from
    "this deployment has no callback address", which are one blank field and one
    server misconfiguration.
    """
    settings = get_settings()
    reason = (
        ""
        if settings.oauth_enabled
        else "Set BLOOM_FEATURE_OAUTH=true and BLOOM_FERNET_KEYS to store credentials."
    )
    return {
        "public_url": settings.public_url,
        "kinds": [
            # A peer with no token needs no encryption key, so it stays available:
            # there is no secret to store badly.
            {"kind": "oauth", "available": settings.oauth_enabled, "reason": reason},
            {"kind": "api_key", "available": settings.oauth_enabled, "reason": reason},
            {"kind": "mcp", "available": True, "reason": ""},
        ],
        "providers": [
            {
                "name": p.name,
                "display_name": p.display_name,
                "auth": list(p.auth_methods),
                "has_deployment_default": p.has_deployment_default,
                "redirect_uri": _callback_for(p, settings),
                "client_id_env": p.client_id_env,
                "client_secret_env": p.client_secret_env,
                "scopes_default": list(p.scopes_default),
                "api_key": (
                    None
                    if p.api_key is None
                    else {"label": p.api_key.label, "help": p.api_key.help}
                ),
                "operations": [op.tool_name(p.name) for op in p.operations],
                "docs_url": p.docs_url,
            }
            for p in sorted(providers().values(), key=lambda p: p.name)
        ],
        "discovered_peers": _discovered_peers(),
    }


def _callback_for(provider: Provider, settings: Settings) -> str:
    """The address to register with this provider, or '' when there is none to give.

    Empty for two different reasons, both of which the caller can tell apart from
    ``public_url``: a provider with no browser flow has no callback at all, and a
    deployment with no ``BLOOM_PUBLIC_URL`` has nowhere to send one. Reported rather
    than raised — listing what this build offers must not fail because one provider
    cannot currently be used.
    """
    if "oauth" not in provider.auth_methods:
        return ""
    try:
        return redirect_uri(provider.name, settings)
    except OAuthError:
        return ""


def _discovered_peers() -> list[dict]:
    """Peers the sync store knows about, to prefill a URL box. Never consulted at run time.

    Imported lazily and guarded: the registry is populated by the MCP server's
    lifespan, and listing connection kinds must not depend on that having happened.
    """
    try:
        from agent_mcp.registry import known_peers, resolve

        out = []
        for name in sorted(known_peers()):
            record = resolve(name)
            base = getattr(record, "base_url", None) or (record or {}).get("base_url", "")
            out.append({"name": name, "base_url": base})
        return out
    except Exception:  # noqa: BLE001 — discovery is a convenience, never a dependency
        logger.debug("Peer discovery unavailable")
        return []


# --- the library ---------------------------------------------------------------


@router.post("/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(body: ConnectionIn) -> ConnectionOut:
    """Create a connection, optionally attaching it to agents in one transaction.

    ``attach_to`` is why this is one call rather than two: "add a connection to this
    agent" is a single intent, and splitting it invents a half-done state — a
    connection that exists but is attached to nothing — that a client would have to
    detect and recover from. An unknown agent id creates nothing at all.
    """
    store = get_store()
    if body.kind != "mcp":
        _require_provider_supporting(body.provider or "", body.kind)
    if body.secret or body.client_secret:
        _require_secrets_storable()
    for agent_id in body.attach_to:
        await _require_agent(store, agent_id)

    settings = get_settings()
    config = dict(body.config)
    if body.client_id:
        config["client_id"] = body.client_id

    # An oauth connection is `pending` until the browser comes back; everything else
    # is usable the moment it is stored. A peer with no token is legitimate on a
    # trusted network, so it is active too.
    status = (
        "pending"
        if (body.kind == "oauth" or (body.kind == "api_key" and not body.secret))
        else "active"
    )

    try:
        row = await asyncio.to_thread(
            store.create_connection,
            kind=body.kind,
            provider=body.provider,
            name=body.name or "",
            label=body.label or _default_label(body),
            config=config,
            secret=encrypt(body.secret, settings) if body.secret else None,
            client_secret=encrypt(body.client_secret, settings) if body.client_secret else None,
            scopes=body.scopes,
            status=status,
            attach_to=body.attach_to,
        )
    except ConnectionNameTaken as exc:
        raise ApiError(409, "conflict", str(exc)) from exc

    logger.info("Created %s connection %s (%s)", row["kind"], row["name"], row["id"])
    return await _out(store, row)


def _default_label(body: ConnectionIn) -> str:
    if body.provider:
        provider = get_provider(body.provider)
        return provider.display_name if provider else body.provider
    return body.name or ""


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(
    kind: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[ConnectionOut]:
    """The whole library, newest first."""
    store = get_store()
    rows = await asyncio.to_thread(
        store.list_connections, kind=kind, provider=provider, status=status
    )
    return [await _out(store, r) for r in rows]


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: str) -> ConnectionOut:
    store = get_store()
    return await _out(store, await _require_connection(store, connection_id))


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
async def update_connection(connection_id: str, body: ConnectionPatch) -> ConnectionOut:
    store = get_store()
    await _require_connection(store, connection_id)
    try:
        row = await asyncio.to_thread(
            store.update_connection, connection_id, **body.model_dump(exclude_unset=True)
        )
    except ConnectionNameTaken as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    if row is None:
        raise ApiError(404, "not_found", f"No connection with id {connection_id!r}.")
    return await _out(store, row)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    force: bool = Query(default=False, description="Detach from every agent and delete."),
) -> None:
    """Delete a connection. A 409 when it is still attached, unless forced.

    The library model's one sharp edge: a shared connection deleted without warning
    silently strips capability from agents nobody was looking at. So the refusal
    names them, and a UI can render it as a confirmation rather than an error.
    """
    store = get_store()
    await _require_connection(store, connection_id)
    agents = await asyncio.to_thread(store.agents_for_connection, connection_id)
    if agents and not force:
        raise ApiError(
            409,
            "conflict",
            f"{len(agents)} agent(s) still use this connection: "
            f"{', '.join(a['slug'] for a in agents)}. Delete it anyway with ?force=true, "
            "or detach it from each first.",
        )
    await asyncio.to_thread(store.delete_connection, connection_id)
    logger.info("Deleted connection %s (was used by %d agent(s))", connection_id, len(agents))


@router.get("/connections/{connection_id}/agents")
async def connection_agents(connection_id: str) -> list[dict]:
    """Which agents use this connection — what makes deleting one an informed choice."""
    store = get_store()
    await _require_connection(store, connection_id)
    return await asyncio.to_thread(store.agents_for_connection, connection_id)


@router.post("/connections/{connection_id}/secret", response_model=ConnectionOut)
async def set_secret(connection_id: str, body: SecretIn) -> ConnectionOut:
    """Paste or rotate a credential. Never a PATCH, so a secret cannot ride an edit.

    This is the route that makes an app registration something a user can supply
    *after* the fact. It used to be settable only at create time, which meant a
    connection Bloom made for you — every connection the builder produces — could
    never be given one, and the only remaining way to authorise it was an
    environment variable on the box: exactly the deployment operation connection-held
    client credentials exist to avoid.
    """
    _require_secrets_storable()
    store = get_store()
    row = await _require_connection(store, connection_id)
    settings = get_settings()

    if body.secret and row["kind"] == "oauth":
        raise ApiError(
            422,
            "unprocessable",
            "An OAuth connection's access token comes from the provider, not from you. "
            "Use /oauth/start to authorise it.",
        )
    if (body.client_id or body.client_secret) and row["kind"] != "oauth":
        raise ApiError(
            422,
            "unprocessable",
            f"A {row['kind']} connection has no app registration — client credentials "
            "belong to an oauth connection. Use 'secret' for its own credential.",
        )

    # Config first, so the row returned below carries both halves. `client_id` is a
    # config key rather than a column, and an empty string clears it: a connection
    # with an id and no secret would otherwise be indistinguishable from one that
    # deliberately falls back to this deployment's default.
    if body.client_id is not None:
        config = dict(row["config"] or {})
        client_id = body.client_id.strip()
        if client_id:
            config["client_id"] = client_id
        else:
            config.pop("client_id", None)
        await asyncio.to_thread(store.update_connection, connection_id, config=config)

    updated = await asyncio.to_thread(
        store.set_connection_secret,
        connection_id,
        secret=encrypt(body.secret, settings) if body.secret else None,
        client_secret=encrypt(body.client_secret, settings) if body.client_secret else None,
        # Pasting the user's key makes an api_key connection usable. Rotating the
        # *app's* secret says nothing about whether the user's grant is still good,
        # so it leaves the status alone.
        status="active" if (body.secret and row["kind"] == "api_key") else None,
    )
    logger.info("Updated secret(s) on connection %s", connection_id)
    return await _out(store, updated or row)


@router.post("/connections/{connection_id}/test", response_model=ProbeOut)
async def test_connection(connection_id: str) -> ProbeOut:
    """Prove the connection actually works, rather than merely being stored.

    This is what makes "paste a URL and a token" trustworthy: a peer is contacted
    and its real tool list comes back, and a credential makes one cheap read-only
    request. Storing a secret proves only that it decrypts.

    Never echoes a credential — not in the detail string, not in the tool list.
    """
    store = get_store()
    row = await _require_connection(store, connection_id)

    if row["kind"] == "mcp":
        return await _probe_peer(row, store)
    return await _probe_provider(row, store)


async def _probe_peer(row: dict, store: Store) -> ProbeOut:
    """Open a session against the peer and list its tools."""
    from agent_runtime import MCPClient

    from app.runtime_service import _peer_resolver

    names, records = await asyncio.to_thread(_peer_resolver, [row], store, get_settings())
    if not names:
        return ProbeOut(ok=False, checked="session", detail="This connection has no URL to reach.")

    client = MCPClient(names, resolver=records)
    try:
        # list_tools() logs and skips an unreachable server rather than raising, so
        # an empty list is the failure signal here, not an exception.
        schemas = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        logger.warning("Probe of peer %s failed: %s", row["name"], exc)
        return ProbeOut(ok=False, checked="session", detail=f"Could not reach it: {exc}")
    finally:
        await client.aclose()

    tools = [str(s.get("function", {}).get("name") or s.get("name") or "") for s in schemas]
    tools = [t for t in tools if t]
    return ProbeOut(
        ok=bool(tools),
        checked="session",
        detail=(
            f"Reached it; {len(tools)} tool(s) available."
            if tools
            else "Reached nothing. Check the URL and the token."
        ),
        tools=sorted(tools),
    )


async def _probe_provider(row: dict, store: Store) -> ProbeOut:
    """Make the manifest's declared probe request with the stored credential."""
    provider = get_provider(row["provider"] or "")
    if provider is None:
        return ProbeOut(ok=False, checked="token", detail="This provider has no manifest.")
    if row["status"] != "active":
        return ProbeOut(
            ok=False, checked="token", detail=f"The connection is {row['status']}, not active."
        )

    resolver = CredentialResolver(store, get_settings())
    cred = await resolver.credential(row["id"])
    if not cred:
        return ProbeOut(
            ok=False, checked="token", detail="No usable credential is stored for this connection."
        )
    if provider.probe is None:
        # Better than nothing and honest about which it was: the credential
        # resolves, which is all a manifest without a [probe] lets us claim.
        return ProbeOut(ok=True, checked="token", detail="A credential is stored and decrypts.")

    import httpx2

    try:
        async with httpx2.AsyncClient() as client:
            response = await client.request(
                provider.probe.method,
                provider.api_base + provider.probe.path,
                params=cred.params or None,
                headers=cred.headers,
                timeout=15.0,
            )
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        logger.warning("Probe of %s failed: %s", row["name"], exc)
        return ProbeOut(ok=False, checked="request", detail=f"Could not reach {provider.api_base}.")

    ok = response.status_code < 400
    if response.status_code in (401, 403):
        detail = f"{provider.display_name} rejected the credential (HTTP {response.status_code})."
    elif ok:
        detail = f"{provider.display_name} accepted the credential."
    else:
        detail = f"{provider.display_name} answered HTTP {response.status_code}."
    return ProbeOut(ok=ok, checked="request", status=response.status_code, detail=detail)


@router.post("/connections/{connection_id}/revoke", response_model=ConnectionOut)
async def revoke_connection(connection_id: str) -> ConnectionOut:
    """Zero the user's credential and mark the connection revoked.

    The row survives so a UI can still show "Spotify — disconnected" instead of the
    provider silently vanishing, and the *client* secret survives with it: the app
    registration is still yours, and clearing it would mean re-entering it to
    reconnect something you only disconnected.
    """
    store = get_store()
    await _require_connection(store, connection_id)
    await asyncio.to_thread(store.revoke_connection, connection_id)
    logger.info("Revoked connection %s", connection_id)
    return await _out(store, await _require_connection(store, connection_id))


@router.post("/connections/{connection_id}/oauth/start", response_model=StartOut)
async def start_oauth(connection_id: str, body: StartIn | None = None) -> StartOut:
    """Begin (or repeat) the browser flow for one connection.

    Repeating it on a live connection is allowed and changes nothing until the
    callback succeeds: opening the authorize page and abandoning the tab is a thing
    people do, and it must not break a connection that already works.
    """
    _require_secrets_storable()
    store = get_store()
    row = await _require_connection(store, connection_id)
    if row["kind"] != "oauth":
        raise ApiError(
            422, "unprocessable", f"A {row['kind']} connection has no browser flow to run."
        )
    provider = _require_provider_supporting(row["provider"], "oauth")

    secrets_row = await asyncio.to_thread(store.connection_secrets, connection_id)
    client = client_credentials(secrets_row or {}, get_settings())
    scopes = (body.scopes if body else None) or row["scopes"] or None

    try:
        return StartOut(
            **await asyncio.to_thread(
                start, store, provider, connection_id, scopes=scopes, client=client
            )
        )
    except OAuthError as exc:
        # 422 rather than 503: with client credentials on the connection this is a
        # blank field in a form the user just filled in, not missing deployment
        # configuration only an operator could supply.
        raise ApiError(422, "unprocessable", str(exc)) from exc


# --- what one agent has attached ------------------------------------------------


@router.get("/agents/{config_id}/connections", response_model=list[ConnectionOut])
async def agent_connections(config_id: str) -> list[ConnectionOut]:
    """This agent's connections, in broker order."""
    store = get_store()
    await _require_agent(store, config_id)
    rows = await asyncio.to_thread(store.connections_for, config_id)
    return [await _out(store, r) for r in rows]


@router.post("/agents/{config_id}/connections", response_model=list[ConnectionOut], status_code=201)
async def attach_connection(config_id: str, body: AttachIn) -> list[ConnectionOut]:
    """Attach an existing library connection to an agent.

    Refused with a 409 when the agent already has a connection for the same
    provider: provider tools are named ``<provider>_<operation>``, checked when the
    manifest loads, so two would collide on tool name and the broker would silently
    keep whichever came first.
    """
    store = get_store()
    await _require_agent(store, config_id)
    row = await _require_connection(store, connection_id=body.connection_id)

    if row["provider"]:
        existing = await asyncio.to_thread(store.connections_for, config_id)
        clash = next(
            (c for c in existing if c["provider"] == row["provider"] and c["id"] != row["id"]),
            None,
        )
        if clash is not None:
            raise ApiError(
                409,
                "conflict",
                f"This agent already has a {row['provider']} connection "
                f"({clash['name']}). Two would produce the same tool names, so detach "
                "that one first.",
            )

    await asyncio.to_thread(
        store.attach_connection, config_id, body.connection_id, position=body.position
    )
    return await agent_connections(config_id)


@router.delete("/agents/{config_id}/connections/{connection_id}", status_code=204)
async def detach_connection(config_id: str, connection_id: str) -> None:
    """Unbind one connection from one agent. The connection itself survives."""
    store = get_store()
    await _require_agent(store, config_id)
    if not await asyncio.to_thread(store.detach_connection, config_id, connection_id):
        raise ApiError(
            404, "not_found", f"Agent {config_id!r} has no connection {connection_id!r}."
        )
    logger.info("Detached connection %s from agent %s", connection_id, config_id)
