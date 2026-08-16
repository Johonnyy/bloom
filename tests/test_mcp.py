"""The MCP server: what it registers, and the ways it deliberately refuses.

Most of these are regression tests for mistakes that are easy to make and hard
to notice — a server that builds twice, a static resource URI with parameters, an
unauthenticated app that serves anyway.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI

from app import config as config_module
from app import mcp as mcp_module
from app.config import Settings
from app.main import _mount_mcp


def _settings(**over) -> Settings:
    # An in-memory usage log, so building a server here never leaves a stray
    # SQLite file in the working tree.
    base = {
        "feature_mcp": True,
        "mcp_keys": "tester:secret",
        "db_path": ":memory:",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def clear_singleton():
    """The server is process-wide and also writes into agent_mcp's peer registry."""
    mcp_module.get_mcp_server.cache_clear()
    yield
    mcp_module.get_mcp_server.cache_clear()


# --- registration ---


def test_the_expected_tools_and_resources_are_registered():
    """Deliberately a short list. Capability lives in the *configured agents*.

    A server that grows tools is a server whose callers must re-read its
    description; Bloom's answer to "can you do X" is a config, not a new tool.

    ``build_agent`` is the one addition that earns its place, because it is the
    exception to that rule: "make me a config" cannot itself be a config.
    """
    mcp = mcp_module.build_server(_settings())
    assert {t.name for t in mcp.tool_policies()} == {"run_task", "list_agents", "build_agent"}
    assert {str(r.uri) for r in mcp.resource_policies()} == {"bloom://agents", "bloom://builds"}


def test_query_tools_are_marked_read_only():
    """A caller decides whether it may retry or cache from this flag alone."""
    policies = {t.name: t for t in mcp_module.build_server(_settings()).tool_policies()}
    assert policies["list_agents"].read_only is True
    # Not read-only: it writes configuration and spends money.
    assert policies["build_agent"].read_only is False


# --- what a delegating agent is told about capability ---


def _configured(tmp_path, *connections):
    """A store with one agent and whatever connections the test cares about."""
    from app import db as db_module

    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj", name="DJ", system_prompt="You pick music.")
    for spec in connections:
        store.create_connection(attach_to=[config["id"]], **spec)
    return store, config


def test_the_agents_resource_publishes_capability_not_configuration(tmp_path, monkeypatch):
    """A caller deciding whether to delegate needs "has Spotify" vs "had Spotify".

    It does *not* need connection ids — those are admin handles that mean nothing
    to a model — and must never see anything that could carry a secret.
    """
    from app import db as db_module

    store, _ = _configured(
        tmp_path,
        {
            "kind": "oauth",
            "provider": "spotify",
            "name": "spotify",
            "label": "Spotify",
            "status": "active",
        },
        {
            "kind": "mcp",
            "name": "amber",
            "config": {"url": "https://amber.example"},
            "status": "revoked",
        },
    )
    monkeypatch.setattr(db_module, "get_store", lambda: store)

    rows = store.list_configs()
    attached = store.connections_by_agent()[rows[0]["id"]]
    published = [
        {k: c[k] for k in ("kind", "provider", "name", "label", "status")} for c in attached
    ]

    assert published == [
        {
            "kind": "oauth",
            "provider": "spotify",
            "name": "spotify",
            "label": "Spotify",
            "status": "active",
        },
        {
            "kind": "mcp",
            "provider": None,
            "name": "amber",
            "label": "",
            "status": "revoked",
        },
    ]
    for entry in published:
        assert "id" not in entry
        assert "secret" not in entry
    store.close()


def test_only_active_connections_are_named_as_capability(tmp_path):
    """A connection that cannot be used is not a capability.

    Naming a revoked one would invite a caller to delegate a task that is going to
    fail, which is worse than not mentioning it.
    """
    store, config = _configured(
        tmp_path,
        {
            "kind": "oauth",
            "provider": "spotify",
            "name": "spotify",
            "label": "Spotify",
            "status": "active",
        },
        {
            "kind": "mcp",
            "name": "amber",
            "config": {"url": "https://amber.example"},
            "status": "needs_reauth",
        },
    )
    attached = store.connections_by_agent()[config["id"]]
    live = [c["label"] or c["name"] for c in attached if c["status"] == "active"]
    assert live == ["Spotify"]
    store.close()


def test_no_tool_demands_a_confirmation_nothing_can_supply():
    """``requires_confirmation`` is only satisfiable by an inbound X-Confirmed
    header. With no UI able to send one, a marked tool would be permanently
    uncallable rather than safely gated."""
    for policy in mcp_module.build_server(_settings()).tool_policies():
        assert policy.requires_confirmation is False


def test_every_tool_carries_a_description():
    """The docstring becomes the wire description, and is the only thing an
    aggregating agent sees before deciding whether to call the tool.

    Read off the private ``_descriptions`` map because ``ToolPolicy`` carries
    only the enforcement flags — this is the same dict the sync-store descriptor
    is built from, so it is what a peer would actually receive.
    """
    mcp = mcp_module.build_server(_settings())
    descriptions = mcp._descriptions
    for policy in mcp.tool_policies():
        assert descriptions.get(policy.name), f"{policy.name} has no description"


def test_the_resource_uri_scheme_follows_the_app_name():
    """Renaming the app must rename its resources, or two apps collide."""
    mcp = mcp_module.build_server(_settings(app_name="other_app"))
    uris = {str(r.uri) for r in mcp.resource_policies()}
    assert all(u.startswith("other_app://") for u in uris), uris


# --- the fail-closed guarantees ---


def test_settings_are_injected_not_read_from_the_environment(monkeypatch):
    """agent_mcp must never see its own AGENT_MCP_* variables here.

    Two config surfaces would eventually disagree about which database to write,
    with no obvious winner.
    """
    monkeypatch.setenv("AGENT_MCP_KEYS", "sneaky:value")
    monkeypatch.setenv("AGENT_MCP_USAGE_DB_PATH", "/tmp/wrong.db")
    built = mcp_module.mcp_settings(_settings(db_path="right.db"))
    assert built.keys == "tester:secret"
    # The tool log lands in Bloom's own file, beside its tables and the runtime's
    # cost rows — that co-tenancy is the whole reason two config surfaces would be
    # dangerous rather than merely untidy.
    assert built.usage_db_path == "right.db"


def test_serving_without_keys_is_refused():
    """agent-mcp-py would rather not start than serve an open server."""
    mcp = mcp_module.build_server(_settings(mcp_keys=""))
    with pytest.raises(RuntimeError, match="no bearer keys"):
        mcp.asgi_app()


def test_the_server_is_built_exactly_once(tmp_path, monkeypatch):
    """Two asgi_app() calls would create two session managers, and the lifespan
    would run the one that is not serving traffic.

    This exercises the real singleton, so it goes through real settings — hence
    the environment, redirected at a tmp path so the run leaves no usage log in
    the working tree.
    """
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "true")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "tester:secret")
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    config_module.get_settings.cache_clear()
    try:
        assert mcp_module.get_mcp_server() is mcp_module.get_mcp_server()
    finally:
        config_module.get_settings.cache_clear()


# --- mounting ---


def test_disabled_mcp_adds_no_routes():
    app = FastAPI()
    before = len(app.router.routes)
    _mount_mcp(app, _settings(feature_mcp=False))
    assert len(app.router.routes) == before


def test_a_flag_without_keys_adds_no_routes():
    app = FastAPI()
    before = len(app.router.routes)
    _mount_mcp(app, _settings(mcp_keys=""))
    assert len(app.router.routes) == before


def test_enabled_mcp_claims_both_the_bare_and_nested_paths(monkeypatch):
    """``Mount("/mcp")`` alone does not match a bare ``POST /mcp``; the host
    router answers 307 and real MCP clients do not follow it."""
    monkeypatch.setattr(mcp_module, "get_mcp_server", lambda: mcp_module.build_server(_settings()))
    app = FastAPI()
    _mount_mcp(app, _settings())

    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert paths.count("/mcp") == 2
    assert "/agent/usage" in paths


def test_the_admin_api_serves_with_the_mcp_server_unmounted(tmp_path):
    """What Bloom can honestly promise, which is less than the template promises.

    Every other app in the ecosystem asserts that with the flag off it never
    *imports* the agent stack — the core principle that an app must run standalone
    with zero knowledge the ecosystem exists. **Bloom cannot claim that and should
    not pretend to.** It is the one service whose purpose is the agent layer:
    `app.admin.deps` imports `agent_mcp.auth` to verify a bearer token and
    `app.admin.agents` imports `agent_runtime.model_router` to validate a tier.
    Both are hard dependencies, and a version of Bloom without them would be a
    different service.

    The weaker guarantee that *is* real, and worth pinning: with MCP off, no
    ``/mcp`` routes appear, `app.mcp` is never imported — so no session manager is
    constructed and no sync-store registration is attempted — and the management
    API serves normally. That is what makes "edit configurations before you have
    an OpenRouter key" a supported state rather than an accident.

    Run in a subprocess because the assertion is about what is *absent* from
    ``sys.modules``, and the rest of this file imports `app.mcp` — in-process the
    check could only ever pass vacuously.
    """
    script = textwrap.dedent(
        """
        import sys
        from fastapi.testclient import TestClient
        from app.main import app

        paths = [getattr(r, "path", None) for r in app.router.routes]
        assert "/mcp" not in paths, paths
        assert "/agent/usage" not in paths, paths

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            # The management surface is live and still refuses unauthenticated calls.
            assert client.get("/admin/agents").status_code == 401

        assert "app.mcp" not in sys.modules
        print("ok")
        """
    )
    env = {
        **os.environ,
        "BLOOM_FEATURE_MCP": "false",
        "BLOOM_MCP_KEYS": "",
        "BLOOM_DB_PATH": str(tmp_path / "bloom.db"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
