"""Pasted keys, and the ways they differ from a grant.

The point of the `api_key` kind is that it changes exactly one thing — where the
secret comes from. The manifest, the synthesised tools, their schemas and the
credential denylist are all shared with OAuth, which is what makes "a new provider
is a TOML file" true for both.

The two places it genuinely differs are pinned here: a static key is **unscoped**
(its permissions live in the provider's console, where Bloom cannot read them) and
a 401 on one is **not worth retrying** (a key that was just rejected will be
rejected again).
"""

from __future__ import annotations

import asyncio

from agent_runtime import LocalToolBroker
from cryptography.fernet import Fernet

from app import credentials as credentials_module
from app import db as db_module
from app.config import Settings
from app.crypto import encrypt
from app.providers import registry as registry_module

KEY = Fernet.generate_key().decode()


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:", "feature_oauth": True, "fernet_keys": KEY}
    return Settings(**{**base, **over})


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def factory(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        self.requests.append({"method": method, "url": url, **kw})
        return self.responses.pop(0) if self.responses else FakeResponse(200, {})


def _github_broker(resolver, client, *, scopes=None):
    broker = LocalToolBroker()
    registry_module.register_operations(
        broker,
        registry_module.providers()["github"],
        "conn-1",
        resolver,
        granted_scopes=scopes,
        http_client_factory=client.factory,
    )
    return broker


class FakeResolver:
    """An api_key credential: a header, and nothing to refresh with."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)
        self.forced = 0
        self.reauth: list[str] = []

    async def credential(self, connection_id, *, force_refresh=False):
        if force_refresh:
            self.forced += 1
        key = self.keys.pop(0) if self.keys else ""
        if not key:
            return credentials_module.NO_CREDENTIAL
        return credentials_module.Credential(
            {"Authorization": f"Bearer {key}"}, {}, refreshable=False
        )

    async def mark_needs_reauth(self, connection_id):
        self.reauth.append(connection_id)


# --- how a key reaches the provider --------------------------------------------


def test_a_pasted_key_is_sent_in_the_header_the_manifest_names():
    client = FakeClient([FakeResponse(200, {"login": "johnny"})])
    broker = _github_broker(FakeResolver(["ghp_x"]), client)

    result = asyncio.run(broker.call_tool("github_whoami", {}))

    request = client.requests[0]
    assert request["headers"]["Authorization"] == "Bearer ghp_x"
    assert request["url"] == "https://api.github.com/user"
    # And it appears nowhere the model can see.
    assert "ghp_x" not in result


def test_a_key_in_the_query_travels_as_a_parameter():
    spec = registry_module.ApiKeySpec(location="query", query_param="apikey")
    assert spec.apply("abc") == ({}, {"apikey": "abc"})


def test_a_credential_parameter_cannot_be_displaced_by_a_model_argument():
    """Merged last, so even a colliding name loses.

    The manifest loader already refuses that collision, so this is belt and braces
    on the one thing that must never be model-settable.
    """
    from app.providers.registry import Operation, Param

    op = Operation(
        name="go", method="GET", path="/go", description="d", params=(Param("apikey", "query"),)
    )
    _, query, _ = registry_module._split_args(op, {"apikey": "model-supplied"})
    cred = credentials_module.Credential({}, {"apikey": "real"}, refreshable=False)
    assert {**query, **cred.params} == {"apikey": "real"}


# --- the one behavioural difference from a grant -------------------------------


def test_a_401_on_a_static_key_is_not_retried():
    """One request, zero refreshes, then a sentence.

    A grant can lapse between the resolver's expiry check and the provider seeing
    the request, so a forced refresh and a retry are worth it. A key that was just
    rejected will be rejected again, and retrying only spends another call.
    """
    client = FakeClient([FakeResponse(401, None, "bad credentials")])
    resolver = FakeResolver(["ghp_x", "ghp_x"])
    broker = _github_broker(resolver, client)

    result = asyncio.run(broker.call_tool("github_whoami", {}))

    assert len(client.requests) == 1
    assert resolver.forced == 0
    assert resolver.reauth == ["conn-1"]
    assert "reconnect" in result.lower()


def test_a_403_does_not_condemn_the_connection():
    """On most APIs a 403 is per-operation permission, not a dead credential.

    Marking it for reauth would make a human reconnect something that works.
    """
    client = FakeClient([FakeResponse(403, None, "forbidden")])
    resolver = FakeResolver(["ghp_x"])
    broker = _github_broker(resolver, client)

    result = asyncio.run(broker.call_tool("github_whoami", {}))

    assert resolver.reauth == []
    assert result.startswith("HTTP 403")


# --- scoping -------------------------------------------------------------------


def test_a_key_is_unscoped_unless_an_operator_narrows_it():
    provider = registry_module.providers()["github"]

    unscoped = {op.name for op in registry_module.operations_for(provider, None)}
    assert unscoped == {op.name for op in provider.operations}
    assert "list_issues" in unscoped

    narrowed = {op.name for op in registry_module.operations_for(provider, ["read:user"])}
    assert "whoami" in narrowed
    assert "list_issues" not in narrowed


def test_the_broker_offers_an_api_key_connection_unscoped(tmp_path):
    """End to end through `runtime_service`, since that is where the choice is made."""
    from app import runtime_service
    from app.trace import RunRecorder, TraceWriter

    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dev")
    store.create_connection(
        kind="api_key",
        provider="github",
        name="github",
        secret=encrypt("ghp_x", settings),
        status="active",
        scopes=[],
        attach_to=[config["id"]],
    )

    runner, aclose = runtime_service.build_runner(
        store.get_config(config["id"]),
        recorder=RunRecorder(TraceWriter(store), "run-1"),
        settings=settings,
        store=store,
    )
    names = {s.get("function", {}).get("name") for s in asyncio.run(runner.broker.list_tools())}
    assert {"github_whoami", "github_search_repos", "github_list_issues"} <= names

    asyncio.run(aclose())
    store.close()


# --- presentation ---------------------------------------------------------------


def test_the_resolver_presents_a_key_where_its_provider_wants_it(tmp_path):
    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    connection = store.create_connection(
        kind="api_key",
        provider="github",
        name="github",
        secret=encrypt("ghp_x", settings),
        status="active",
    )
    resolver = credentials_module.CredentialResolver(store, settings)

    cred = asyncio.run(resolver.credential(connection["id"]))
    assert cred.headers == {"Authorization": "Bearer ghp_x"}
    assert cred.refreshable is False
    store.close()


def test_a_grant_is_presented_as_a_bearer_and_is_refreshable(tmp_path):
    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    connection = store.create_connection(
        kind="oauth",
        provider="spotify",
        name="spotify",
        secret=encrypt("at", settings),
        status="active",
    )
    resolver = credentials_module.CredentialResolver(store, settings)

    cred = asyncio.run(resolver.credential(connection["id"]))
    assert cred.headers == {"Authorization": "Bearer at"}
    assert cred.refreshable is True
    store.close()


def test_a_peer_bearer_never_becomes_a_tool_credential(tmp_path):
    """`kind='mcp'` is presented by MCPClient at session open, not per call."""
    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    connection = store.create_connection(
        kind="mcp",
        name="amber",
        config={"url": "https://amber.example"},
        secret=encrypt("tok", settings),
        status="active",
    )
    resolver = credentials_module.CredentialResolver(store, settings)

    assert not asyncio.run(resolver.credential(connection["id"]))
    # The plaintext is still reachable for the peer resolver that does need it.
    assert asyncio.run(resolver.secret(connection["id"])) == "tok"
    store.close()
