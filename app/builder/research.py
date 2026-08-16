"""The builder's eyes: search the web, and read one page.

Ported from ``amber_v2/app/tools/search.py`` and ``fetch.py``, with two deliberate
changes.

**`httpx2`, not `httpx`.** Bloom depends on `httpx2` because `mcp` v2 ships it as a
separate distribution; plain `httpx` is a dev-only dependency here, present solely
for FastAPI's `TestClient`. Importing it at runtime would work on a developer's
machine and fail in the container.

**Tavily only — no DuckDuckGo fallback.** Amber's `auto` provider degrades to
scraping DuckDuckGo's HTML when no key is set, which she documents as best-effort
and liable to break when their markup changes. That trade is right for a voice
assistant, where a mediocre search result still beats silence. It is wrong here: the
builder's output is a *configuration* that will run unattended against someone's
account, and a half-scraped page is how it ends up writing an endpoint that does not
exist. With no key, `app.config.builder_enabled` reports the builder unavailable and
the route answers 503 — refusing rather than guessing.

**`read_url` is the genuinely new attack surface**, exactly as it is in Amber, and
for the same reason: Bloom runs on a VPS beside other services, so a tool that
fetches an arbitrary URL on request is a tool that can be talked into fetching
``http://169.254.169.254/`` or a neighbour's admin port. `_check_url` refuses
non-HTTP schemes and any host resolving into a loopback, private, link-local or
otherwise reserved range — and it refuses **before** an HTTP client is constructed,
so a blocked request makes no network call at all. That ordering is the property
worth testing, and `tests/test_research_tools.py` asserts the client factory was
never called rather than matching on the message.

The residual gap is DNS rebinding: a hostname that passes the check and resolves to
something else on the connection itself. Closing it properly means pinning the
resolved address through the socket, which the client does not expose cleanly. It is
documented rather than papered over — the realistic attack, a model persuaded to
fetch a literal metadata IP or an internal hostname, is closed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlparse

from app.builder.htmltext import extract
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"

_READABLE_TYPES = ("text/html", "application/xhtml", "text/plain", "application/json")
_USER_AGENT = "Mozilla/5.0 (compatible; Bloom/0.1; agent builder)"

# Hostnames that never route anywhere legitimate from Bloom's perspective.
_BLOCKED_NAMES = ("localhost", "localhost.localdomain")
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


def _blocked_address(host: str) -> bool:
    """True if ``host`` is (or resolves to) an address Bloom must not reach."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable: let the request fail normally rather than claiming it is
        # dangerous. Nothing is reachable, so nothing is at risk.
        return False

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


def check_url(url: str) -> str | None:
    """The reason this URL cannot be fetched, or ``None`` if it is allowed."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Error: that is not a valid web address."

    if parsed.scheme not in ("http", "https"):
        return "Error: only http and https pages can be read."
    host = (parsed.hostname or "").lower()
    if not host:
        return "Error: that is not a valid web address."
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        return "Error: pages on the local network cannot be read."
    if _blocked_address(host):
        return "Error: pages on the local network cannot be read."
    return None


def https_public(url: str) -> str | None:
    """The reason this URL is unsafe as a stored API base, or ``None``.

    Stricter than :func:`check_url` by one rule — HTTPS is required, not merely
    preferred — because this is applied to a URL that will be stored and then called
    repeatedly *with a credential attached*, rather than read once.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return "must be https"
    return "resolves to a private or loopback address" if check_url(url) else None


async def web_search(
    query: str,
    *,
    max_results: int = 5,
    settings: Settings | None = None,
    client_factory: Any = None,
) -> str:
    """Search the web through Tavily. Returns prose, never raises.

    Formatted answer-first with sourced snippets *and their URLs*, which is what
    makes `read_url` usable as a second hop: the model needs somewhere to go next,
    and a snippet with no link is a dead end.
    """
    settings = settings or get_settings()
    query = (query or "").strip()
    if not query:
        return "Error: web_search needs something to search for."
    if not settings.search_api_key.strip():
        return (
            "Error: web search is not configured on this Bloom (BLOOM_SEARCH_API_KEY "
            "is empty), so the service cannot be researched."
        )

    limit = max(1, min(int(max_results or settings.search_max_results), 10))

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    try:
        async with factory() as client:
            response = await client.post(
                TAVILY_URL,
                json={
                    "api_key": settings.search_api_key.strip(),
                    "query": query,
                    "max_results": limit,
                    "include_answer": True,
                    "search_depth": "basic",
                },
                timeout=settings.search_timeout_s,
            )
    except Exception as exc:  # noqa: BLE001 — a tool reports, it does not raise
        logger.warning("web_search failed for %r: %s", query, exc)
        return f"Error: the search could not be completed ({type(exc).__name__})."

    if response.status_code >= 400:
        # The key is in the request body, so the status is safe to report; the body
        # is not echoed, because an auth error from Tavily can quote it back.
        return f"Error: the search provider answered HTTP {response.status_code}."

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return "Error: the search provider returned something unreadable."

    return _format_results(query, payload)


def _format_results(query: str, payload: dict) -> str:
    answer = str((payload or {}).get("answer") or "").strip()
    results = (payload or {}).get("results") or []

    lines: list[str] = []
    if answer:
        lines.append(answer)
        lines.append("")

    for item in results if isinstance(results, list) else ():
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = " ".join(str(item.get("content") or "").split())[:400]
        if not url:
            continue
        lines.append(f"- {title or url}\n  {url}\n  {snippet}")

    if not lines:
        return f"No results for {query!r}."
    return "\n".join(lines).strip()


async def read_url(
    url: str, *, settings: Settings | None = None, client_factory: Any = None
) -> str:
    """Read one page's main text. Returns prose, never raises."""
    settings = settings or get_settings()
    url = (url or "").strip()
    if not url:
        return "Error: read_url needs a web address."

    # Checked before a client exists, so a refused URL is never fetched at all.
    refusal = check_url(url)
    if refusal:
        logger.warning("read_url refused: %s", url)
        return refusal

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    max_bytes = settings.read_url_max_bytes

    try:
        async with factory(
            timeout=settings.read_url_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return f"Error: that page returned HTTP {resp.status_code}."

                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith(_READABLE_TYPES):
                    kind = content_type.split(";")[0].strip() or "that file type"
                    return f"Error: that link is {kind}, which cannot be read as text."

                # Stream with a hard cap rather than trusting Content-Length: a
                # missing or lying header must not be able to pull an unbounded body
                # into memory.
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if max_bytes > 0 and size >= max_bytes:
                        break
                body = b"".join(chunks).decode(
                    getattr(resp, "encoding", None) or "utf-8", errors="replace"
                )
    except Exception as exc:  # noqa: BLE001 — a tool reports, it does not raise
        logger.warning("read_url failed for %s: %s", url, exc)
        return f"Error: that page could not be opened ({type(exc).__name__})."

    title, text = _readable(body, url)
    if not text:
        return "Error: that page opened but had no readable text on it."

    limit = settings.read_url_max_chars
    truncated = limit > 0 and len(text) > limit
    if truncated:
        text = text[:limit]

    header = f"{title} — {url}" if title else url
    suffix = "\n\n[truncated — this is the start of the page]" if truncated else ""
    return f"{header}\n\n{text}{suffix}"


def _readable(body: str, url: str) -> tuple[str, str]:
    """Extract title and text, pretty-printing JSON rather than flattening it.

    API documentation is frequently *served* as JSON — an OpenAPI document, a
    registry entry — and running that through an HTML parser yields a wall of
    punctuation. Detecting it costs one parse attempt and makes those pages usable.
    """
    stripped = body.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return "", json.dumps(json.loads(body), indent=2)[:200_000]
        except ValueError:
            pass
    return extract(body)
