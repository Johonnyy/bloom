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
class Provider:
    name: str
    display_name: str
    authorize_url: str
    token_url: str
    api_base: str
    client_id_env: str
    client_secret_env: str
    scopes_default: tuple[str, ...] = ()
    operations: tuple[Operation, ...] = ()
    pkce: bool = True
    refresh_rotates: bool = False
    auth_style: str = "basic"  # basic | body
    docs_url: str = ""
    revoke_url: str = ""

    @property
    def client_id(self) -> str:
        return os.environ.get(self.client_id_env, "")

    @property
    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "")

    @property
    def configured(self) -> bool:
        """Whether this provider can actually start a flow.

        A manifest ships with the image; the credentials do not. Reporting the
        difference is what lets Aperture grey out "Connect Spotify" with a reason
        instead of failing at the redirect.
        """
        return bool(self.client_id and self.client_secret)


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
    for required in ("authorize_url", "token_url", "api_base", "client_id_env"):
        if not raw.get(required):
            raise ManifestError(f"{path.name}: missing required key {required!r}")

    operations = tuple(_operation(name, op) for op in raw.get("operations", ()) or ())
    seen: set[str] = set()
    for op in operations:
        if op.name in seen:
            raise ManifestError(f"{name}: duplicate operation {op.name!r}")
        seen.add(op.name)

    return Provider(
        name=name,
        display_name=str(raw.get("display_name", name.title())),
        authorize_url=str(raw["authorize_url"]),
        token_url=str(raw["token_url"]),
        api_base=str(raw["api_base"]).rstrip("/"),
        client_id_env=str(raw["client_id_env"]),
        client_secret_env=str(raw.get("client_secret_env", "")),
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


def operations_for(provider: Provider, granted_scopes: Iterable[str]) -> list[Operation]:
    """Operations this grant can actually perform.

    Filtering by scope is not cosmetic: offering a tool the token cannot authorise
    means the model spends a step, gets a 403, and has to recover — when the
    information needed to avoid that was available before the run started.
    """
    granted = set(granted_scopes or ())
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
    granted_scopes: Iterable[str] = (),
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
    """Build the callable for one operation, closed over the *connection id*."""

    async def call(**kwargs: Any) -> str:
        token = await resolver.access_token(connection_id)
        if not token:
            return (
                f"{provider.display_name} is not connected, or its authorisation has "
                "expired. Ask the user to reconnect it in Aperture."
            )

        path, query, body = _split_args(op, kwargs)
        url = provider.api_base + path

        import httpx2

        factory = http_client_factory or httpx2.AsyncClient
        async with factory() as client:
            response = await client.request(
                op.method,
                url,
                params=query or None,
                json=body or None,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

        if response.status_code == 401:
            # One retry after a forced refresh: a token can lapse between the
            # resolver's expiry check and the provider receiving the request.
            token = await resolver.access_token(connection_id, force_refresh=True)
            if token:
                async with factory() as client:
                    response = await client.request(
                        op.method,
                        url,
                        params=query or None,
                        json=body or None,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30.0,
                    )
            if response.status_code == 401:
                await resolver.mark_needs_reauth(connection_id)
                return (
                    f"{provider.display_name} rejected the stored authorisation. "
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
