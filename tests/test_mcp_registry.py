"""What Bloom can actually reach, and why an entry it cannot is a dead end.

The fixtures below are the real response shape, captured from
``GET https://registry.modelcontextprotocol.io/v0/servers?search=github&limit=2``.
Recorded rather than invented, because the whole value of this module is agreeing
with a service nobody here controls — a hand-written fixture would let the parser
drift and still pass.

The verdict matters more than the parsing. `MCPClient` speaks streamable-HTTP and
sets ``Authorization: Bearer``, so three large categories of entry are unusable no
matter how good they are, and each test below pins one of them *along with the
reason string* — because the reason is what tells the model to stop pursuing a
candidate instead of retrying it.
"""

from __future__ import annotations

import asyncio

from app.builder import mcp_registry
from app.config import Settings

# Verbatim from the live API, trimmed to the fields the parser reads.
REAL_ENTRY = {
    "server": {
        "name": "ai.smithery/Hint-Services-obsidian-github-mcp",
        "description": "Connect AI assistants to your GitHub-hosted Obsidian vault.",
        "repository": {
            "url": "https://github.com/Hint-Services/obsidian-github-mcp",
            "source": "github",
        },
        "version": "0.4.0",
        "remotes": [
            {
                "type": "streamable-http",
                "url": "https://server.smithery.ai/@Hint-Services/obsidian-github-mcp/mcp",
                "headers": [
                    {
                        "description": "Bearer token for Smithery authentication",
                        "isRequired": True,
                        "value": "Bearer {smithery_api_key}",
                        "isSecret": True,
                        "name": "Authorization",
                    }
                ],
            }
        ],
    },
    "_meta": {
        "io.modelcontextprotocol.registry/official": {
            "status": "active",
            "isLatest": True,
        }
    },
}


def _entry(**server_over) -> dict:
    server = {**REAL_ENTRY["server"], **server_over}
    return {"server": server, "_meta": REAL_ENTRY["_meta"]}


def _settings(**over) -> Settings:
    return Settings(_env_file=None, db_path=":memory:", **over)


class FakeResponse:
    def __init__(self, status_code=200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def factory(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.response


# --- the verdict -------------------------------------------------------------


def test_a_streamable_http_server_wanting_a_bearer_is_usable():
    candidate = mcp_registry.to_candidate(REAL_ENTRY)
    assert candidate is not None
    assert candidate.usable is True
    assert candidate.reason == ""
    assert candidate.url == "https://server.smithery.ai/@Hint-Services/obsidian-github-mcp/mcp"
    assert candidate.needs_token is True
    # The hint reaches the checklist, so the human knows *which* key to go and find.
    assert "Smithery" in candidate.token_hint


def test_a_package_only_entry_is_unusable_and_the_reason_names_why():
    """Bloom connects over HTTP; it does not spawn npx."""
    candidate = mcp_registry.to_candidate(
        _entry(remotes=[], packages=[{"registryType": "npm", "identifier": "some-mcp"}])
    )
    assert candidate is not None
    assert candidate.usable is False
    assert "local package" in candidate.reason
    assert "does not run local processes" in candidate.reason


def test_an_sse_only_server_is_unusable_and_the_reason_names_the_transport():
    candidate = mcp_registry.to_candidate(
        _entry(remotes=[{"type": "sse", "url": "https://example.com/sse"}])
    )
    assert candidate is not None
    assert candidate.usable is False
    assert "sse" in candidate.reason
    assert "streamable-http" in candidate.reason


def test_a_server_wanting_a_different_header_is_unusable_and_the_reason_names_it():
    """The single most confusing failure to debug at run time, so it is named here."""
    candidate = mcp_registry.to_candidate(
        _entry(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://example.com/mcp",
                    "headers": [{"name": "X-API-Key", "isRequired": True}],
                }
            ]
        )
    )
    assert candidate is not None
    assert candidate.usable is False
    assert "X-API-Key" in candidate.reason


def test_an_optional_extra_header_does_not_make_a_server_unusable():
    """`isRequired: false` is a suggestion; only a required header can block us."""
    candidate = mcp_registry.to_candidate(
        _entry(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://example.com/mcp",
                    "headers": [{"name": "X-Trace-Id", "isRequired": False}],
                }
            ]
        )
    )
    assert candidate is not None
    assert candidate.usable is True


def test_a_usable_remote_is_preferred_over_an_unusable_one_in_the_same_entry():
    """An entry offering both should be reported usable, not judged on the first."""
    candidate = mcp_registry.to_candidate(
        _entry(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://example.com/keyed",
                    "headers": [{"name": "X-API-Key", "isRequired": True}],
                },
                {"type": "streamable-http", "url": "https://example.com/open", "headers": []},
            ]
        )
    )
    assert candidate is not None
    assert candidate.usable is True
    assert candidate.url == "https://example.com/open"


def test_a_server_with_no_remotes_and_no_packages_is_unusable():
    candidate = mcp_registry.to_candidate(_entry(remotes=[]))
    assert candidate is not None
    assert candidate.usable is False
    assert "no remote" in candidate.reason


# --- the digest the model reads ----------------------------------------------


def test_the_digest_leads_with_the_verdict_for_an_unusable_server():
    candidate = mcp_registry.to_candidate(
        _entry(remotes=[{"type": "sse", "url": "https://example.com/sse"}])
    )
    assert candidate is not None
    assert "UNUSABLE:" in candidate.digest()


def test_the_digest_gives_a_usable_server_its_endpoint():
    candidate = mcp_registry.to_candidate(REAL_ENTRY)
    assert candidate is not None
    digest = candidate.digest()
    assert "endpoint: https://server.smithery.ai" in digest
    # The repository is the only publisher signal there is, so it is always shown.
    assert "repository: https://github.com/Hint-Services" in digest


# --- the search call ----------------------------------------------------------


def test_search_hits_the_documented_path_and_parses_the_envelope():
    client = FakeClient(FakeResponse(payload={"servers": [REAL_ENTRY], "metadata": {"count": 1}}))
    candidates, error = asyncio.run(
        mcp_registry.search("github", settings=_settings(), client_factory=client.factory)
    )
    assert error == ""
    assert [c.name for c in candidates] == ["ai.smithery/Hint-Services-obsidian-github-mcp"]
    assert client.calls[0]["url"].endswith("/v0/servers")
    assert client.calls[0]["params"]["search"] == "github"


def test_a_deleted_entry_is_dropped():
    deleted = {
        "server": REAL_ENTRY["server"],
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deleted"}},
    }
    client = FakeClient(FakeResponse(payload={"servers": [deleted]}))
    candidates, error = asyncio.run(
        mcp_registry.search("github", settings=_settings(), client_factory=client.factory)
    )
    assert error == ""
    assert candidates == []


def test_an_unreachable_registry_is_prose_rather_than_an_exception():
    class Boom(FakeClient):
        async def get(self, url, **kw):
            raise OSError("connection refused")

    client = Boom(FakeResponse())
    candidates, error = asyncio.run(
        mcp_registry.search("github", settings=_settings(), client_factory=client.factory)
    )
    assert candidates == []
    assert error.startswith("Error:")
    assert "OSError" in error


def test_get_returns_the_named_entry_and_says_so_when_there_is_none():
    client = FakeClient(FakeResponse(payload={"servers": [REAL_ENTRY]}))
    found, error = asyncio.run(
        mcp_registry.get(
            "ai.smithery/Hint-Services-obsidian-github-mcp",
            settings=_settings(),
            client_factory=client.factory,
        )
    )
    assert error == ""
    assert found is not None and found.usable

    client = FakeClient(FakeResponse(payload={"servers": [REAL_ENTRY]}))
    missing, error = asyncio.run(
        mcp_registry.get("nobody/nothing", settings=_settings(), client_factory=client.factory)
    )
    assert missing is None
    assert "No registry entry" in error
