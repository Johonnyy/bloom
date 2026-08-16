"""Searching the official MCP Registry, and deciding what Bloom can actually reach.

The registry (``https://registry.modelcontextprotocol.io``) is the reason MCP-first
is a *procedure* rather than a hope: it is one REST call that answers "does this
service publish an MCP server", deterministically, instead of asking a model to
recall it.

``GET /v0/servers?search=<q>&limit=<n>`` returns::

    {"servers": [{"server": {"name", "description", "version",
                             "repository": {"url", "source"},
                             "remotes": [{"type", "url", "headers": [...]}],
                             "packages": [...]},
                  "_meta": {"io.modelcontextprotocol.registry/official":
                            {"status", "isLatest", ...}}}],
     "metadata": {"nextCursor", "count"}}

**The usability verdict is computed here, not left to the model.** Bloom reaches a
peer through `agent_runtime.MCPClient`, which speaks streamable-HTTP and sets
``Authorization: Bearer`` — and nothing else. That makes three large categories of
registry entry unreachable no matter how good they are:

* an entry with only ``packages`` is a local process (``npx``, ``uvx``, a container).
  Bloom does not spawn processes;
* a remote of type ``sse`` is a different transport;
* a remote requiring a header under any other name — ``X-API-Key`` is common — cannot
  be authenticated, because the client has no way to send it.

A model told merely to "prefer official servers" attaches one of these and reports
success, and the failure surfaces much later as an agent whose tools silently do not
exist. So each result carries ``usable`` and, when false, a ``reason`` naming which
of these it hit. The reason is written to be read by the model: it is what tells it
to stop pursuing this candidate rather than retry it.

**Trust is explicitly not computed.** Anyone can publish here — searching "spotify"
returns third-party trend-data servers, not Spotify — and there is no field that
settles it. The digest surfaces the publisher namespace and the repository URL as
weak signals and the prompt tells the model to judge; the real protection is that
nothing the builder creates works until a human attaches a credential to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

TIMEOUT_S = 15.0

#: The transport `MCPClient` speaks. Anything else is unreachable from Bloom.
SUPPORTED_TRANSPORT = "streamable-http"

#: The one header the client can set. Compared case-insensitively.
SUPPORTED_HEADER = "authorization"

_OFFICIAL_META = "io.modelcontextprotocol.registry/official"


@dataclass(frozen=True)
class Candidate:
    """One registry entry, judged against what Bloom can reach."""

    name: str
    description: str
    version: str
    repository: str
    url: str = ""
    usable: bool = False
    reason: str = ""
    needs_token: bool = False
    token_hint: str = ""
    latest: bool = True
    status: str = "active"
    transports: tuple[str, ...] = field(default_factory=tuple)

    def digest(self) -> str:
        """One entry as the model should read it: verdict first, then why."""
        head = f"- {self.name} (v{self.version or '?'})"
        if not self.usable:
            return f"{head}\n  UNUSABLE: {self.reason}\n  {self.description}"[:900]
        auth = (
            f"a bearer token is required — {self.token_hint or 'no hint given'}"
            if self.needs_token
            else "none declared"
        )
        lines = [head, f"  endpoint: {self.url}", f"  auth: {auth}"]
        if self.repository:
            lines.append(f"  repository: {self.repository}")
        if self.description:
            lines.append(f"  {self.description}")
        return "\n".join(lines)[:900]


def _headers_of(remote: dict) -> list[dict]:
    raw = remote.get("headers")
    return [h for h in raw if isinstance(h, dict)] if isinstance(raw, list) else []


def _judge(server: dict) -> tuple[str, bool, str, bool, str, tuple[str, ...]]:
    """``(url, usable, reason, needs_token, token_hint, transports)`` for one entry."""
    remotes = server.get("remotes")
    remotes = [r for r in remotes if isinstance(r, dict)] if isinstance(remotes, list) else []
    transports = tuple(str(r.get("type") or "").strip() for r in remotes if r.get("type"))

    if not remotes:
        packages = server.get("packages") or []
        detail = "it publishes only a local package" if packages else "it publishes no remote"
        return (
            "",
            False,
            f"{detail} (stdio/npx/container). Bloom connects over HTTP and does not "
            "run local processes, so this cannot be reached.",
            False,
            "",
            transports,
        )

    supported = [r for r in remotes if str(r.get("type") or "").strip() == SUPPORTED_TRANSPORT]
    if not supported:
        offered = ", ".join(sorted(set(transports))) or "none"
        return (
            "",
            False,
            f"its transport is {offered}; Bloom speaks {SUPPORTED_TRANSPORT} only.",
            False,
            "",
            transports,
        )

    # Prefer a remote whose auth Bloom can satisfy, rather than judging only the
    # first: an entry offering both an anonymous and a keyed endpoint should be
    # reported usable.
    fallback_reason = ""
    for remote in supported:
        url = str(remote.get("url") or "").strip()
        if not url:
            continue
        required = [h for h in _headers_of(remote) if h.get("isRequired", True)]
        unsupported = [
            str(h.get("name") or "?")
            for h in required
            if str(h.get("name") or "").strip().lower() != SUPPORTED_HEADER
        ]
        if unsupported:
            fallback_reason = (
                f"it requires the header {', '.join(unsupported)}, and Bloom can only "
                "send Authorization: Bearer. This server cannot be authenticated here."
            )
            continue
        bearer = [h for h in required if str(h.get("name") or "").lower() == SUPPORTED_HEADER]
        hint = str(bearer[0].get("description") or "").strip() if bearer else ""
        return (url, True, "", bool(bearer), hint, transports)

    return (
        "",
        False,
        fallback_reason or "it declares no reachable endpoint URL.",
        False,
        "",
        transports,
    )


def to_candidate(entry: dict) -> Candidate | None:
    """Turn one ``servers[]`` element into a judged :class:`Candidate`."""
    server = entry.get("server") if isinstance(entry, dict) else None
    if not isinstance(server, dict) or not server.get("name"):
        return None

    meta = ((entry.get("_meta") or {}).get(_OFFICIAL_META)) or {}
    repository = ""
    repo = server.get("repository")
    if isinstance(repo, dict):
        repository = str(repo.get("url") or "").strip()

    url, usable, reason, needs_token, hint, transports = _judge(server)
    return Candidate(
        name=str(server["name"]),
        description=" ".join(str(server.get("description") or "").split())[:300],
        version=str(server.get("version") or ""),
        repository=repository,
        url=url,
        usable=usable,
        reason=reason,
        needs_token=needs_token,
        token_hint=hint,
        latest=bool(meta.get("isLatest", True)),
        status=str(meta.get("status") or "active"),
        transports=transports,
    )


async def search(
    query: str,
    *,
    limit: int = 10,
    settings: Settings | None = None,
    client_factory: Any = None,
) -> tuple[list[Candidate], str]:
    """``(candidates, error)``. Never raises; an error is prose for the model."""
    settings = settings or get_settings()
    query = (query or "").strip()
    if not query:
        return [], "Error: mcp_registry_search needs something to search for."

    base = settings.mcp_registry_url.strip().rstrip("/")

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    try:
        async with factory() as client:
            response = await client.get(
                f"{base}/v0/servers",
                params={"search": query, "limit": max(1, min(int(limit or 10), 30))},
                timeout=TIMEOUT_S,
            )
    except Exception as exc:  # noqa: BLE001 — a tool reports, it does not raise
        logger.warning("MCP registry search failed for %r: %s", query, exc)
        return [], f"Error: the MCP registry could not be reached ({type(exc).__name__})."

    if response.status_code >= 400:
        return [], f"Error: the MCP registry answered HTTP {response.status_code}."

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return [], "Error: the MCP registry returned something unreadable."

    entries = (payload or {}).get("servers")
    if not isinstance(entries, list):
        return [], "Error: the MCP registry returned an unexpected shape."

    found = [c for c in (to_candidate(e) for e in entries) if c is not None]
    # Deleted entries are noise; a non-latest version of something also present is
    # a duplicate the model would have to reason about for no benefit.
    return [c for c in found if c.status != "deleted"], ""


async def get(
    name: str, *, settings: Settings | None = None, client_factory: Any = None
) -> tuple[Candidate | None, str]:
    """One entry by exact name. ``(candidate, error)``.

    The search digest is clipped, and the decision of whether to attach a server to
    an account deserves the full record — so this exists as its own hop rather than
    making search verbose for every result.
    """
    settings = settings or get_settings()
    name = (name or "").strip()
    if not name:
        return None, "Error: mcp_registry_get needs a server name."

    candidates, error = await search(
        name, limit=30, settings=settings, client_factory=client_factory
    )
    if error:
        return None, error
    for candidate in candidates:
        if candidate.name == name:
            return candidate, ""
    return None, f"No registry entry named {name!r}. Use mcp_registry_search to find one."
