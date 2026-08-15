"""How an agent's connections become the brokers a run actually uses.

Two properties here are load-bearing rather than cosmetic:

* **Credential tools come before peers.** A peer server that could shadow a tool
  carrying the user's own account access would be a real escalation, not a naming
  annoyance.
* **A peer is resolved from its own stored URL and token**, not from the shared
  sync-store registry. That is what makes "paste an endpoint" mean something.

No network: `MCPClient` takes an injected session factory, so the peer half is
driven end to end without a server.
"""

from __future__ import annotations

import asyncio

import pytest
from agent_runtime import LocalToolBroker, MCPClient
from cryptography.fernet import Fernet

from app import db as db_module
from app import runtime_service
from app.config import Settings
from app.crypto import encrypt
from app.trace import RunRecorder, TraceWriter

KEY = Fernet.generate_key().decode()


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:", "feature_oauth": True, "fernet_keys": KEY}
    return Settings(**{**base, **over})


def _store_with(tmp_path, *connections) -> tuple[db_module.Store, dict]:
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj")
    for spec in connections:
        store.create_connection(attach_to=[config["id"]], **spec)
    return store, config


def _peer_spec(name, url, *, secret=None, status="active", settings=None):
    return {
        "kind": "mcp",
        "name": name,
        "config": {"url": url},
        "secret": encrypt(secret, settings) if secret else None,
        "status": status,
    }


# --- the resolver mapping ------------------------------------------------------


def test_a_peer_resolves_from_its_own_url_and_token(tmp_path):
    settings = _settings()
    store, _ = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", secret="peer-token", settings=settings),
        _peer_spec("finance", "https://finance.example", settings=settings),
    )

    names, records = runtime_service._peer_resolver(
        store.connections_for(store.list_configs()[0]["id"]), store, settings
    )

    assert names == ["amber", "finance"]
    assert records["amber"] == {"base_url": "https://amber.example", "token": "peer-token"}
    # No token stored means none sent — a peer on a trusted network is legitimate.
    assert records["finance"] == {"base_url": "https://finance.example"}
    store.close()


def test_a_peer_that_is_not_active_contributes_nothing(tmp_path):
    settings = _settings()
    store, config = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", status="revoked", settings=settings),
    )

    names, records = runtime_service._peer_resolver(
        store.connections_for(config["id"]), store, settings
    )
    assert (names, records) == ([], {})
    store.close()


def test_an_undecryptable_peer_is_skipped_rather_than_failing_the_run(tmp_path):
    """A key rotated out from under a stored token must not take the whole run down."""
    store, config = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", secret="tok", settings=_settings()),
        _peer_spec("finance", "https://finance.example", settings=_settings()),
    )
    # A different key: the ciphertext no longer decrypts.
    other = _settings(fernet_keys=Fernet.generate_key().decode())

    names, _ = runtime_service._peer_resolver(store.connections_for(config["id"]), store, other)
    assert names == ["finance"]
    store.close()


def test_a_peer_with_no_url_is_skipped(tmp_path):
    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj")
    # The API refuses this, so reaching it means a hand-edited database.
    store.create_connection(
        kind="mcp", name="broken", config={}, status="active", attach_to=[config["id"]]
    )

    names, _ = runtime_service._peer_resolver(store.connections_for(config["id"]), store, settings)
    assert names == []
    store.close()


# --- broker assembly -----------------------------------------------------------


def _brokers_of(broker):
    """Unwrap TracingBroker -> CompositeBroker into the list it was built from."""
    inner = getattr(broker, "_inner", None) or getattr(broker, "inner", None) or broker
    return getattr(inner, "brokers", None) or getattr(inner, "_brokers", None) or [inner]


def test_three_peers_become_exactly_one_client(tmp_path):
    """One client, one session cache, one teardown — not one client per peer."""
    settings = _settings()
    store, config = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", settings=settings),
        _peer_spec("finance", "https://finance.example", settings=settings),
        _peer_spec("school", "https://school.example", settings=settings),
    )

    runner, aclose = runtime_service.build_runner(
        store.get_config(config["id"]),
        recorder=RunRecorder(TraceWriter(store), "run-1"),
        settings=settings,
        store=store,
    )
    clients = [b for b in _brokers_of(runner.broker) if isinstance(b, MCPClient)]
    assert len(clients) == 1
    assert sorted(clients[0].servers) == ["amber", "finance", "school"]

    asyncio.run(aclose())
    store.close()


def test_credential_tools_are_offered_before_peers(tmp_path):
    """A peer must never be able to shadow a tool carrying the user's own access.

    Order is the whole mechanism: `CompositeBroker` resolves a duplicate name to
    the first broker that claims it.
    """
    settings = _settings()
    store, config = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", settings=settings),
        {
            "kind": "api_key",
            "provider": "github",
            "name": "github",
            "secret": encrypt("ghp_x", settings),
            "status": "active",
        },
    )

    runner, aclose = runtime_service.build_runner(
        store.get_config(config["id"]),
        recorder=RunRecorder(TraceWriter(store), "run-1"),
        settings=settings,
        store=store,
    )
    brokers = _brokers_of(runner.broker)
    # Two, explicitly: without this the assertions below would also pass if the
    # unwrap helper had silently fallen back to a single-element list.
    assert len(brokers) == 2
    assert isinstance(brokers[0], LocalToolBroker)
    assert isinstance(brokers[1], MCPClient)

    asyncio.run(aclose())
    store.close()


def test_an_agent_with_no_connections_still_builds(tmp_path):
    settings = _settings()
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="bare")

    runner, aclose = runtime_service.build_runner(
        config, recorder=RunRecorder(TraceWriter(store), "run-1"), settings=settings, store=store
    )
    assert runner is not None
    asyncio.run(aclose())
    store.close()


def test_the_composite_is_still_torn_down(tmp_path):
    """`AgentRunner` closes a broker only when it built one itself.

    Passing `broker=` makes its own `finally` dead code, so an MCPClient's session
    stack and HTTP client leak once per run unless Bloom closes it.
    """
    settings = _settings()
    store, config = _store_with(
        tmp_path, _peer_spec("amber", "https://amber.example", settings=settings)
    )

    runner, aclose = runtime_service.build_runner(
        store.get_config(config["id"]),
        recorder=RunRecorder(TraceWriter(store), "run-1"),
        settings=settings,
        store=store,
    )
    closed = []
    for broker in _brokers_of(runner.broker):
        if isinstance(broker, MCPClient):
            original = broker.aclose

            async def spy(_original=original):
                closed.append(True)
                await _original()

            broker.aclose = spy

    asyncio.run(aclose())
    assert closed == [True]
    store.close()


# --- what the model is told about what it cannot use ---------------------------


def test_an_unusable_connection_becomes_a_line_in_the_prompt(tmp_path):
    """Silence would be worse: the model improvises, usually by claiming it worked."""
    settings = _settings()
    store, config = _store_with(
        tmp_path,
        _peer_spec("amber", "https://amber.example", status="revoked", settings=settings),
        {
            "kind": "oauth",
            "provider": "spotify",
            "name": "spotify",
            "status": "needs_reauth",
            "scopes": [],
        },
    )

    notes = runtime_service._connection_notes(store.connections_for(config["id"]), settings)
    assert "Spotify is not currently connected (needs_reauth)" in notes
    assert "amber__* tools are unavailable" in notes
    store.close()


def test_an_active_connection_produces_no_note(tmp_path):
    settings = _settings()
    store, config = _store_with(
        tmp_path, _peer_spec("amber", "https://amber.example", settings=settings)
    )
    assert runtime_service._connection_notes(store.connections_for(config["id"]), settings) == ""
    store.close()


# --- namespacing ---------------------------------------------------------------


def test_a_peers_tools_arrive_namespaced_by_its_connection_name():
    """`<name>__<tool>`, which is why a connection name is validated like one."""

    class FakeTool:
        def __init__(self, name):
            self.name = name
            self.description = "d"
            self.inputSchema = {"type": "object", "properties": {}}

    class FakeSession:
        async def list_tools(self):
            return type("Listing", (), {"tools": [FakeTool("now_playing")]})()

    class FakeFactory:
        def __init__(self, url, headers):
            self.headers = headers

        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *exc):
            return False

    client = MCPClient(
        ["amber"],
        resolver={"amber": {"base_url": "https://amber.example", "token": "tok"}},
        session_factory=FakeFactory,
    )
    schemas = asyncio.run(client.list_tools())
    names = [s.get("function", {}).get("name") or s.get("name") for s in schemas]
    assert names == ["amber__now_playing"]
    asyncio.run(client.aclose())


@pytest.mark.parametrize("name", ["amber", "finance_app", "school-2"])
def test_valid_namespaces_survive_a_round_trip(tmp_path, name):
    settings = _settings()
    store, config = _store_with(tmp_path, _peer_spec(name, "https://x.example", settings=settings))
    names, _ = runtime_service._peer_resolver(store.connections_for(config["id"]), store, settings)
    assert names == [name]
    assert "__" not in name
    store.close()
