"""The admin-authed cost view.

The same numbers `/agent/usage` reports, behind the *other* key set. The point of
duplicating them is the key separation: Aperture must be able to draw a cost chart
without holding a token that also authorises spending.

No model is ever called, so model spend is zero throughout — what these cover is that
each of the three sources is reached, reports its own shape, and degrades honestly
when it is absent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import runtime_service
from app import trace as trace_module
from app.config import get_settings

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
MCP_TOKEN = "mcp-secret"  # noqa: S105 — likewise
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_MCP_KEYS", f"amber:{MCP_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "test-key")

    get_settings.cache_clear()
    db_module.get_store.cache_clear()
    trace_module.reset_writer()

    from app.main import app

    with TestClient(app) as c:
        yield c

    db_module.get_store().close()
    get_settings.cache_clear()
    db_module.get_store.cache_clear()
    trace_module.reset_writer()


def _seed_runs(store, *, succeeded: int = 2, failed: int = 1) -> str:
    config = store.create_config(slug="dj")
    n = 0
    for status, count in (("succeeded", succeeded), ("failed", failed)):
        for _ in range(count):
            n += 1
            run_id = f"r{n}"
            store.create_run(
                run_id=run_id, agent_config_id=config["id"], prompt="p", origin="test_run"
            )
            store.finish_run(run_id, status=status, total_cost_usd=0.01)
    return config["id"]


def test_usage_reports_runs_models_and_the_floor_caveat(client):
    _seed_runs(db_module.get_store())

    body = client.get("/admin/usage", headers=AUTH).json()

    assert body["runs"]["runs"] == 3
    assert body["runs"]["succeeded"] == 2
    assert body["runs"]["failed"] == 1
    assert body["runs"]["cost_usd"] == pytest.approx(0.03)
    assert body["runs"]["by_agent"][0]["agent"] == "dj"

    # agent_runtime's own tracker, reading Bloom's database rather than its default.
    assert body["models"]["total_cost_usd"] == 0
    assert body["models"]["by_model"] == []

    # The caveat travels with the numbers, not only in the docs: a client rendering a
    # total has to decide how to label it.
    assert "floor" in body["caveat"]


def test_counts_are_numbers_not_nulls_on_an_empty_database(client):
    """SUM(bool) is NULL over an empty table, and a JSON null where a client expects
    a number is a rendering bug at the far end."""
    body = client.get("/admin/usage", headers=AUTH).json()
    for key in ("succeeded", "failed", "cancelled", "running"):
        assert body["runs"][key] == 0
    assert body["runs"]["runs"] == 0


def test_tool_calls_are_null_rather_than_zero_when_mcp_is_not_mounted(client):
    """Zero would read as "nothing has called me"; null says "nobody could"."""
    assert client.get("/admin/usage", headers=AUTH).json()["tools"] is None


def test_tool_calls_are_reported_when_the_mcp_server_is_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_MCP_KEYS", f"amber:{MCP_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "true")

    get_settings.cache_clear()
    db_module.get_store.cache_clear()
    trace_module.reset_writer()
    from app import mcp as mcp_module

    mcp_module.get_mcp_server.cache_clear()

    from app.main import app

    try:
        with TestClient(app) as c:
            body = c.get("/admin/usage", headers=AUTH).json()
        assert body["tools"] is not None
        assert body["tools"]["totals"]["calls"] == 0
    finally:
        db_module.get_store().close()
        get_settings.cache_clear()
        db_module.get_store.cache_clear()
        trace_module.reset_writer()
        mcp_module.get_mcp_server.cache_clear()


def test_since_narrows_the_window(client):
    _seed_runs(db_module.get_store())
    future = "2099-01-01T00:00:00+00:00"
    assert client.get(f"/admin/usage?since={future}", headers=AUTH).json()["runs"]["runs"] == 0


def test_usage_requires_the_admin_key_and_refuses_the_mcp_one(client):
    """The whole reason this endpoint exists beside /agent/usage."""
    assert client.get("/admin/usage").status_code == 401
    assert (
        client.get("/admin/usage", headers={"Authorization": f"Bearer {MCP_TOKEN}"}).status_code
        == 401
    )


def test_the_registry_of_in_flight_runs_starts_empty(client):
    """A leaked task would let a later run be cancelled by an earlier run's id."""
    assert runtime_service.in_flight_ids() == []
