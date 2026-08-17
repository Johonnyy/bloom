"""Which URLs Bloom is allowed to reach, in one place.

Extracted from `app.builder.research`, where these lived while `read_url` was the
only caller. `app.providers.registry` now needs the same rules for a stored
manifest's endpoints, and a manifest is *lower* in the dependency graph than the
builder — a provider definition has no business importing the agent that writes
one. Neither does a duplicated copy: two SSRF checks are one SSRF check and one
that quietly stops matching it.

The threat is the same in both directions and worth restating, because it is the
one place in Bloom where an outbound request is aimed by something other than a
human. Bloom runs on a VPS beside other services, so a URL that resolves into a
loopback, private, link-local or reserved range reaches a neighbour's admin port
or a cloud metadata endpoint. `read_url` would return that as page text. A stored
manifest is worse: `CredentialResolver` attaches a live token to every request it
makes, so a bad ``api_base`` turns the metadata service into an authenticated
client of itself.

**The residual gap is DNS rebinding**, documented rather than papered over: the
name is resolved here and again by the HTTP client, and a record that changes
between the two passes this check and then connects somewhere else. Closing it
means pinning the resolved address into the connection, which httpx does not make
easy. It is a real hole, it is narrow, and pretending otherwise in a comment
would be worse than naming it.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Hostnames that never route anywhere legitimate from Bloom's perspective.
_BLOCKED_NAMES = ("localhost", "localhost.localdomain")
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


def blocked_address(host: str) -> bool:
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
    if blocked_address(host):
        return "Error: pages on the local network cannot be read."
    return None


def https_public(url: str) -> str | None:
    """The reason this URL is unsafe as a stored endpoint, or ``None``.

    Stricter than :func:`check_url` by one rule — HTTPS is required, not merely
    preferred — because this is applied to a URL that will be stored and then
    called repeatedly *with a credential attached*, rather than read once.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return "must be https"
    return "resolves to a private or loopback address" if check_url(url) else None


def host_of(url: str) -> str:
    """The hostname of a URL, for showing a person where their credential goes.

    Lowercased and bare — no scheme, no port, no path. This is read by someone
    deciding whether to paste an API key, so it has to be the part they can
    recognise, not a string they have to parse.
    """
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


__all__ = ["blocked_address", "check_url", "host_of", "https_public"]
