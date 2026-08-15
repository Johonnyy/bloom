"""Settings, and the guard that keeps a half-configured install from crashing."""

from __future__ import annotations

from app.config import Settings


def _settings(**over) -> Settings:
    """Build settings without touching disk, so a developer's .env cannot fail CI."""
    return Settings(_env_file=None, **over)


def test_defaults_are_safe_to_boot_with():
    """MCP is *on* by default here, unlike every other app — but still keyless.

    The template ships ``feature_mcp=False`` because the ecosystem layer is opt-in
    for an app that would work without it. Bloom is the one service whose purpose
    is that layer, so the flag defaults on and `mcp_enabled` below is what keeps a
    fresh clone from crashing on the missing keys.
    """
    s = _settings()
    assert s.app_name == "bloom"
    assert s.feature_mcp is True
    assert s.mcp_keys == ""
    assert s.mcp_enabled is False


def test_oauth_needs_a_fernet_key_as_well_as_the_flag():
    """Storing a token without a key is a breach, not a degraded mode."""
    assert _settings(feature_oauth=True, fernet_keys="").oauth_enabled is False
    assert _settings(feature_oauth=False, fernet_keys="k").oauth_enabled is False
    assert _settings(feature_oauth=True, fernet_keys="k").oauth_enabled is True


def test_admin_keys_are_separate_from_mcp_keys():
    """Two principals, two key sets — one leaked token must not buy both."""
    s = _settings(mcp_keys="amber:a", admin_keys="aperture:b")
    assert s.mcp_keys != s.admin_keys


def test_mcp_needs_keys_as_well_as_the_flag():
    """The flag alone must not enable the server.

    agent-mcp-py refuses to build an unauthenticated app, so without this guard
    flipping the flag and forgetting the keys would take the whole service down
    at startup rather than leaving MCP quietly unmounted.
    """
    assert _settings(feature_mcp=True, mcp_keys="").mcp_enabled is False
    assert _settings(feature_mcp=True, mcp_keys="   ").mcp_enabled is False
    assert _settings(feature_mcp=False, mcp_keys="a:b").mcp_enabled is False
    assert _settings(feature_mcp=True, mcp_keys="a:b").mcp_enabled is True


def test_unrelated_env_vars_are_ignored(monkeypatch):
    """A co-tenant library's variables must not be a validation error."""
    monkeypatch.setenv("AGENT_MCP_KEYS", "someone:else")
    monkeypatch.setenv("BLOOM_UNKNOWN_KNOB", "1")
    assert _settings().mcp_keys == ""


def test_the_app_name_is_a_legal_mcp_namespace():
    """The default must satisfy agent-mcp-py, or the template fails at construction."""
    from agent_mcp.schema import validate_app_name

    validate_app_name(_settings().app_name)
