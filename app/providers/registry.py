"""Provider manifests: how a connected account becomes tools a model can call.

The spec asked that adding a provider be "a config entry, not new code". Two
readings of that fail in practice, so this is a third.

*One generic ``provider_request(method, path, body)`` tool* is config-only and
unusable: its description would have to contain the whole API, the model guesses
paths, and nothing bounds a DELETE. *Hand-written Python per provider* is usable
and makes every provider a deploy. A **declarative operation manifest** is both —
each operation carries a real name, a real JSON schema and a real description, and
adding one is a TOML file.

**The token is never a tool argument, and never captured in a closure either.**
An argument would land in ``Step.tool_calls``, be replayed into the next request,
be logged at INFO by the runtime, and be persisted verbatim in Bloom's own trace —
the model would end up holding the user's Spotify token in its context. A closure
would be safe from all that but could not survive expiry mid-run. So each
synthesised tool closes over a *connection id* and asks
:class:`app.credentials.CredentialResolver` for a live token at call time.

Manifest validation is strict and happens at load, because every failure mode here
is one that would otherwise appear as a confusing runtime error:

* Tool names must satisfy `agent_mcp`'s rules — snake_case, ≤40 chars, and
  crucially **no ``__``**, which would collide with `MCPClient`'s
  ``<server>__<tool>`` namespacing.
* No parameter may live in a header, and none may be named ``authorization``,
  ``token``, ``access_token``, ``api_key`` or ``cookie``. That denylist is the
  thing standing between a model-authored argument and a secret in the log.

**Two ways to hold a credential, one way to use one.** ``auth`` declares which a
provider supports: ``oauth`` (a grant Bloom obtains and refreshes) or ``api_key``
(a key the user pastes). It changes only where the secret comes from — the
operations, their schemas, the scope filter and the denylist above are identical,
which is what makes "a new provider is a TOML file" true for both. A manifest that
says nothing is ``oauth``, so every file written before this existed is unchanged
and keeps every guarantee it had.

**Client credentials resolve connection-first, environment-second.** A connection
may carry the client id and secret of the app it was registered against; a manifest
names environment variables holding a deployment-wide default. :func:`client_for`
is the one place that order is decided. The environment used to be the only option,
which made connecting a provider a deployment operation — editing a secrets file on
the box and restarting the container — for something a user should be able to do
from a form.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_DIR = Path(__file__).resolve().parent

# agent-mcp-py's own rule, applied here even though these tools live in a local
# broker: 40 leaves room if Bloom ever re-exposes them over MCP, and `__` must
# never appear or MCPClient's namespacing becomes ambiguous.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TOOL_NAME_LEN = 40

# Parameter names a model must never be able to set. A tool argument is
# model-authored data that ends up in the trace and the runtime's INFO log.
FORBIDDEN_PARAMS = frozenset(
    {"authorization", "token", "access_token", "api_key", "apikey", "cookie", "set-cookie"}
)
ALLOWED_PARAM_LOCATIONS = frozenset({"query", "path", "body"})

_JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})

# How a connection may hold a credential for a provider. `mcp` is a connection kind
# but never a provider one — a peer server has no manifest.
AUTH_METHODS = frozenset({"oauth", "api_key"})
API_KEY_LOCATIONS = frozenset({"header", "query"})


class ManifestError(ValueError):
    """A provider manifest is malformed. Raised at load, never at call time."""


@dataclass(frozen=True)
class Param:
    name: str
    location: str  # query | path | body
    type: str = "string"
    required: bool = False
    description: str = ""
    enum: tuple[str, ...] | None = None
    items: str | None = None
    default: Any = None

    def json_schema(self) -> dict:
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.type == "array":
            schema["items"] = {"type": self.items or "string"}
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    description: str
    read_only: bool = True
    requires_confirmation: bool = False
    scopes: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()

    def tool_name(self, provider: str) -> str:
        return f"{provider}_{self.name}"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.params},
            "required": [p.name for p in self.params if p.required],
        }


@dataclass(frozen=True)
class ApiKeySpec:
    """Where a pasted key goes on an outbound request.

    Declared per provider because there is no convention worth guessing: some want
    ``Authorization: Bearer``, some a bespoke header, some a query parameter.
    """

    location: str  # header | query
    header: str = ""
    prefix: str = ""
    query_param: str = ""
    label: str = "API key"
    help: str = ""

    def apply(self, secret: str) -> tuple[dict[str, str], dict[str, str]]:
        """``(headers, params)`` for one request — the only place a key becomes wire."""
        if self.location == "header":
            return {self.header: f"{self.prefix}{secret}"}, {}
        return {}, {self.query_param: secret}


@dataclass(frozen=True)
class Probe:
    """A cheap authenticated request that proves a credential works.

    Deliberately not an ``Operation``: it takes no model-authored arguments, is
    never registered as a tool, and exists only so "test this connection" can
    answer with something better than "the token decrypts".
    """

    method: str = "GET"
    path: str = "/"


@dataclass(frozen=True)
class ClientCredentials:
    """The app credentials one connection authorises with, and where they came from."""

    client_id: str = ""
    client_secret: str = ""
    source: str = ""  # connection | environment

    def __bool__(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class Provider:
    name: str
    display_name: str
    api_base: str
    # Empty for a provider that supports only `api_key` — there is no flow to run.
    authorize_url: str = ""
    token_url: str = ""
    client_id_env: str = ""
    client_secret_env: str = ""
    auth_methods: tuple[str, ...] = ("oauth",)
    api_key: ApiKeySpec | None = None
    probe: Probe | None = None
    scopes_default: tuple[str, ...] = ()
    operations: tuple[Operation, ...] = ()
    pkce: bool = True
    refresh_rotates: bool = False
    auth_style: str = "basic"  # basic | body
    docs_url: str = ""
    revoke_url: str = ""

    def supports(self, kind: str) -> bool:
        """Whether a connection of this kind can hold a credential for this provider."""
        return kind in self.auth_methods

    @property
    def env_client_id(self) -> str:
        return os.environ.get(self.client_id_env, "") if self.client_id_env else ""

    @property
    def env_client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "") if self.client_secret_env else ""

    @property
    def has_deployment_default(self) -> bool:
        """Whether this box carries client credentials for the provider.

        A hint, not a gate: a connection that carries its own does not need one.
        Reporting it lets a create form prefill instead of asking.
        """
        return bool(self.env_client_id and self.env_client_secret)


def client_for(
    provider: Provider,
    *,
    client_id: str = "",
    client_secret: str = "",
) -> ClientCredentials:
    """Resolve the app credentials for one connection: its own first, then the box.

    The caller passes what the connection stores — ``client_id`` from its config and
    ``client_secret`` already decrypted, because decryption needs the cipher and this
    module deliberately knows nothing about it.

    Both halves must come from the same place. Mixing a connection's client id with
    the environment's secret would produce an ``invalid_client`` from the provider
    and a support question that no log answers.
    """
    if client_id and client_secret:
        return ClientCredentials(client_id, client_secret, "connection")
    if provider.has_deployment_default:
        return ClientCredentials(provider.env_client_id, provider.env_client_secret, "environment")
    return ClientCredentials()


def _param(name: str, raw: dict, *, where: str) -> Param:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: parameter {name!r} must be a table")
    if name.lower() in FORBIDDEN_PARAMS:
        raise ManifestError(
            f"{where}: parameter {name!r} is not allowed — credentials are injected "
            "by Bloom and must never be settable by the model"
        )
    location = str(raw.get("in", "query")).lower()
    if location not in ALLOWED_PARAM_LOCATIONS:
        raise ManifestError(
            f"{where}: parameter {name!r} has in={location!r}; allowed: "
            f"{', '.join(sorted(ALLOWED_PARAM_LOCATIONS))} (headers are refused outright)"
        )
    ptype = str(raw.get("type", "string"))
    if ptype not in _JSON_TYPES:
        raise ManifestError(f"{where}: parameter {name!r} has unknown type {ptype!r}")
    enum = raw.get("enum")
    return Param(
        name=name,
        location=location,
        type=ptype,
        required=bool(raw.get("required", False)),
        description=str(raw.get("description", "")),
        enum=tuple(str(e) for e in enum) if enum else None,
        items=raw.get("items"),
        default=raw.get("default"),
    )


def _operation(provider_name: str, raw: dict) -> Operation:
    name = str(raw.get("name", "")).strip()
    where = f"{provider_name}.{name or '<unnamed>'}"
    if not name:
        raise ManifestError(f"{provider_name}: an operation is missing its name")

    tool_name = f"{provider_name}_{name}"
    if "__" in tool_name:
        raise ManifestError(
            f"{where}: tool name {tool_name!r} contains '__', which collides with "
            "MCPClient's <server>__<tool> namespacing"
        )
    if not _TOOL_NAME.match(tool_name) or len(tool_name) > MAX_TOOL_NAME_LEN:
        raise ManifestError(
            f"{where}: tool name {tool_name!r} must be snake_case and at most "
            f"{MAX_TOOL_NAME_LEN} characters"
        )

    description = str(raw.get("description", "")).strip()
    if not description:
        # Not pedantry: a manifest-driven tool has no docstring, so this string is
        # the *only* thing the model sees before deciding whether to call it.
        raise ManifestError(f"{where}: an operation must carry a description")

    method = str(raw.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ManifestError(f"{where}: unsupported method {method!r}")

    params = raw.get("params") or {}
    return Operation(
        name=name,
        method=method,
        path=str(raw.get("path", "/")),
        description=description,
        read_only=bool(raw.get("read_only", method == "GET")),
        requires_confirmation=bool(raw.get("requires_confirmation", False)),
        scopes=tuple(str(s) for s in raw.get("scopes", ()) or ()),
        params=tuple(_param(k, v, where=where) for k, v in params.items()),
    )


def _auth_methods(where: str, raw: Any) -> tuple[str, ...]:
    """Parse ``auth``, accepting a bare string or a list.

    Absent means ``oauth``: every manifest written before this key existed keeps
    exactly the behaviour it had.
    """
    if raw is None:
        return ("oauth",)
    values = [raw] if isinstance(raw, str) else list(raw)
    methods = tuple(str(v).strip().lower() for v in values)
    if not methods:
        raise ManifestError(f"{where}: 'auth' is empty; omit it to mean 'oauth'")
    for method in methods:
        if method not in AUTH_METHODS:
            raise ManifestError(
                f"{where}: unknown auth method {method!r}; allowed: "
                f"{', '.join(sorted(AUTH_METHODS))}"
            )
    return methods


def _api_key_spec(where: str, raw: Any, operations: tuple[Operation, ...]) -> ApiKeySpec:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: 'auth' includes api_key, so an [api_key] table is required")
    location = str(raw.get("in", "header")).lower()
    if location not in API_KEY_LOCATIONS:
        raise ManifestError(
            f"{where}: [api_key] in={location!r}; allowed: "
            f"{', '.join(sorted(API_KEY_LOCATIONS))} (a body-borne key is not supported)"
        )
    header = str(raw.get("header", "")).strip()
    query_param = str(raw.get("query_param", "")).strip()
    if location == "header" and not header:
        raise ManifestError(f"{where}: [api_key] in='header' needs a 'header' name")
    if location == "query":
        if not query_param:
            raise ManifestError(f"{where}: [api_key] in='query' needs a 'query_param' name")
        # A declared parameter of the same name would be merged over by the
        # credential, so the model would author an argument the schema advertises
        # and silently never send it — one operation, at run time, as a wrong
        # answer. Cheaper to refuse the manifest.
        for op in operations:
            for param in op.params:
                if param.name == query_param:
                    raise ManifestError(
                        f"{where}: [api_key] query_param {query_param!r} collides with "
                        f"a parameter of operation {op.name!r}; the credential would "
                        "silently displace the model's argument"
                    )
    return ApiKeySpec(
        location=location,
        header=header,
        prefix=str(raw.get("prefix", "")),
        query_param=query_param,
        label=str(raw.get("label", "API key")),
        help=str(raw.get("help", "")),
    )


def _probe(where: str, raw: Any) -> Probe | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: [probe] must be a table")
    method = str(raw.get("method", "GET")).upper()
    if method not in {"GET", "HEAD"}:
        # A probe fires on a button press with no confirmation, so it is read-only
        # by construction rather than by the manifest author remembering.
        raise ManifestError(f"{where}: [probe] method {method!r} must be GET or HEAD")
    return Probe(method=method, path=str(raw.get("path", "/")))


def load_manifest(path: Path) -> Provider:
    """Parse and validate one manifest file."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path.name}: not valid TOML — {exc}") from exc

    name = str(raw.get("name", "")).strip()
    if not name or not re.match(r"^[a-z][a-z0-9_]{0,19}$", name):
        raise ManifestError(
            f"{path.name}: 'name' must be snake_case and at most 20 characters "
            "(it prefixes every tool this provider contributes)"
        )
    if not raw.get("api_base"):
        raise ManifestError(f"{path.name}: missing required key 'api_base'")

    auth_methods = _auth_methods(path.name, raw.get("auth"))
    # Required *because of* what this provider declares it supports, rather than
    # unconditionally: an api_key-only provider has no authorize endpoint and no
    # client credentials to name, and demanding them would mean inventing values.
    if "oauth" in auth_methods:
        for required in ("authorize_url", "token_url"):
            if not raw.get(required):
                raise ManifestError(
                    f"{path.name}: missing required key {required!r} (this provider's "
                    "'auth' includes oauth)"
                )

    operations = tuple(_operation(name, op) for op in raw.get("operations", ()) or ())
    seen: set[str] = set()
    for op in operations:
        if op.name in seen:
            raise ManifestError(f"{name}: duplicate operation {op.name!r}")
        seen.add(op.name)

    api_key = (
        _api_key_spec(path.name, raw.get("api_key"), operations)
        if "api_key" in auth_methods
        else None
    )

    return Provider(
        name=name,
        display_name=str(raw.get("display_name", name.title())),
        api_base=str(raw["api_base"]).rstrip("/"),
        authorize_url=str(raw.get("authorize_url", "")),
        token_url=str(raw.get("token_url", "")),
        client_id_env=str(raw.get("client_id_env", "")),
        client_secret_env=str(raw.get("client_secret_env", "")),
        auth_methods=auth_methods,
        api_key=api_key,
        probe=_probe(path.name, raw.get("probe")),
        scopes_default=tuple(str(s) for s in raw.get("scopes_default", ()) or ()),
        operations=operations,
        pkce=bool(raw.get("pkce", True)),
        refresh_rotates=bool(raw.get("refresh_rotates", False)),
        auth_style=str(raw.get("auth_style", "basic")),
        docs_url=str(raw.get("docs_url", "")),
        revoke_url=str(raw.get("revoke_url", "")),
    )


@lru_cache
def providers() -> dict[str, Provider]:
    """Every manifest in ``app/providers``, by name.

    A malformed manifest is fatal for that provider only — it is logged and
    skipped, so one bad file cannot stop the service from starting and taking
    every other provider down with it.
    """
    found: dict[str, Provider] = {}
    for path in sorted(MANIFEST_DIR.glob("*.toml")):
        try:
            provider = load_manifest(path)
        except ManifestError:
            logger.exception("Ignoring provider manifest %s", path.name)
            continue
        found[provider.name] = provider
    logger.info("Loaded %d provider manifest(s): %s", len(found), ", ".join(sorted(found)) or "-")
    return found


def get_provider(name: str) -> Provider | None:
    return providers().get(name)


# --- turning an operation into something the model can call ------------------


def _split_args(op: Operation, args: dict) -> tuple[str, dict, dict]:
    """Sort model-supplied arguments into path, query and body by declaration.

    Anything not declared is dropped rather than forwarded. A model that invents a
    parameter should get the documented call, not an opaque 400 from the provider.
    """
    path = op.path
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    by_name = {p.name: p for p in op.params}
    for key, value in (args or {}).items():
        param = by_name.get(key)
        if param is None or value is None:
            continue
        if param.location == "path":
            path = path.replace("{" + key + "}", str(value))
        elif param.location == "query":
            query[key] = value
        else:
            body[key] = value
    for param in op.params:
        if param.default is not None and param.location == "query" and param.name not in query:
            query[param.name] = param.default
    return path, query, body


MAX_TOOL_RESULT = 4000


def _summarise(status: int, text: str) -> str:
    """What the model gets back.

    Truncated, because a 200 KB API response is mostly padding for a decision that
    hinges on a few fields — and it is also persisted into the trace. Request
    headers are never echoed: the Authorization header is right there.
    """
    body = text.strip()
    if len(body) > MAX_TOOL_RESULT:
        body = body[:MAX_TOOL_RESULT] + f"… [truncated, {len(text)} characters total]"
    if status >= 400:
        return f"HTTP {status}: {body or '(empty response)'}"
    return body or f"HTTP {status}: (empty response — the request succeeded)"


def operations_for(provider: Provider, granted_scopes: Iterable[str] | None) -> list[Operation]:
    """Operations this credential can actually perform.

    Filtering by scope is not cosmetic: offering a tool the token cannot authorise
    means the model spends a step, gets a 403, and has to recover — when the
    information needed to avoid that was available before the run started.

    ``None`` means *unscoped*, and is not the same as an empty list. An API key's
    permissions are set in the provider's own console and Bloom has no way to read
    them, so filtering would hide capability the key may well have. An operator who
    knows a key is narrow says so by storing scopes on the connection; an empty list
    still means "nothing beyond the unscoped operations", as it always did.
    """
    if granted_scopes is None:
        return list(provider.operations)
    granted = set(granted_scopes)
    usable, skipped = [], []
    for op in provider.operations:
        if set(op.scopes) <= granted:
            usable.append(op)
        else:
            skipped.append(op.name)
    if skipped:
        logger.info(
            "%s: hiding %s — not covered by the granted scopes",
            provider.name,
            ", ".join(skipped),
        )
    return usable


def register_operations(
    broker: Any,
    provider: Provider,
    connection_id: str,
    resolver: Any,
    *,
    granted_scopes: Iterable[str] | None = (),
    http_client_factory: Any = None,
) -> int:
    """Register this connection's operations on a ``LocalToolBroker``.

    Returns how many were registered. ``http_client_factory`` exists so a test can
    drive the whole path without a network; production leaves it ``None`` and gets
    `httpx2`.
    """
    count = 0
    for op in operations_for(provider, granted_scopes):
        broker.register(
            op.tool_name(provider.name),
            op.description,
            op.input_schema(),
            _make_caller(provider, op, connection_id, resolver, http_client_factory),
            read_only=op.read_only,
            requires_confirmation=op.requires_confirmation,
        )
        count += 1
    return count


def _make_caller(
    provider: Provider,
    op: Operation,
    connection_id: str,
    resolver: Any,
    http_client_factory: Any,
):
    """Build the callable for one operation, closed over the *connection id*.

    The connection id and not the secret: that is what lets a token expire mid-run
    and be refreshed instead of failing the task, and it is why the plaintext never
    outlives one request.
    """

    async def call(**kwargs: Any) -> str:
        cred = await resolver.credential(connection_id)
        if not cred:
            return (
                f"{provider.display_name} is not connected, or its authorisation has "
                "expired. Ask the user to reconnect it in Aperture."
            )

        path, query, body = _split_args(op, kwargs)
        url = provider.api_base + path

        import httpx2

        factory = http_client_factory or httpx2.AsyncClient

        async def send(credential):
            async with factory() as client:
                return await client.request(
                    op.method,
                    url,
                    # Credential parameters merged last. `_split_args` already drops
                    # anything undeclared and FORBIDDEN_PARAMS refuses a
                    # credential-shaped parameter name at manifest load, so this
                    # cannot actually be reached — belt and braces on the one thing
                    # that must never be model-settable.
                    params={**query, **credential.params} or None,
                    json=body or None,
                    headers=credential.headers,
                    timeout=30.0,
                )

        response = await send(cred)

        if response.status_code == 401:
            # One retry after a forced refresh, but only for a credential that can
            # be refreshed: a grant can lapse between the resolver's expiry check
            # and the provider receiving the request, while a static key that was
            # just rejected will be rejected again.
            if cred.refreshable:
                cred = await resolver.credential(connection_id, force_refresh=True)
                if cred:
                    response = await send(cred)
            if response.status_code == 401:
                await resolver.mark_needs_reauth(connection_id)
                return (
                    f"{provider.display_name} rejected the stored credential. "
                    "Ask the user to reconnect it in Aperture."
                )

        return _summarise(response.status_code, _text_of(response))

    return call


def _text_of(response: Any) -> str:
    """Prefer compact JSON over raw text, without assuming either."""
    try:
        return json.dumps(response.json(), separators=(",", ":"))
    except Exception:  # noqa: BLE001 — a non-JSON body is normal, not an error
        return getattr(response, "text", "") or ""
