"""The management surface: CRUD, validation, and the two ways it must say no.

No `conftest.py` — fixtures are local and duplicated on purpose, matching every
other repo in the ecosystem.

The fixture configures through the **environment plus a cache clear**, not by
monkeypatching `get_store` into a handful of modules. Seven modules now do
``from app.db import get_store``, and patching a subset is how you get a test that
passes alone and fails in a suite: the lifespan opens the real singleton while the
routes use the fake. Setting ``BLOOM_DB_PATH`` and clearing the caches exercises
the path the service actually boots through.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import trace as trace_module
from app.config import get_settings

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a fresh database, configured the way a deploy would be."""
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    # MCP off: these tests are about the management surface, and leaving it on
    # would make every one of them depend on the MCP session manager starting.
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "")

    get_settings.cache_clear()
    db_module.get_store.cache_clear()
    # The writer holds a queue bound to the previous test's event loop; reusing it
    # across cases fails with "attached to a different loop".
    trace_module.reset_writer()

    from app.main import app

    with TestClient(app) as c:
        yield c

    db_module.get_store().close()
    get_settings.cache_clear()
    db_module.get_store.cache_clear()
    trace_module.reset_writer()


def test_a_config_survives_a_full_create_read_update_delete_round_trip(client):
    created = client.post(
        "/admin/agents",
        headers=AUTH,
        json={
            "slug": "spotify-dj",
            "name": "Spotify DJ",
            "system_prompt": "You pick music.",
            "model_tier": "balanced",
            "mcp_servers": ["amber"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"] == "spotify-dj"
    assert body["mcp_servers"] == ["amber"]
    assert body["oauth_connections"] == []
    config_id = body["id"]

    fetched = client.get(f"/admin/agents/{config_id}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json() == body

    listed = client.get("/admin/agents", headers=AUTH)
    assert [c["id"] for c in listed.json()] == [config_id]

    patched = client.patch(
        f"/admin/agents/{config_id}", headers=AUTH, json={"system_prompt": "You pick GOOD music."}
    )
    assert patched.status_code == 200
    assert patched.json()["system_prompt"] == "You pick GOOD music."
    # Untouched fields survive a partial update.
    assert patched.json()["slug"] == "spotify-dj"

    assert client.delete(f"/admin/agents/{config_id}", headers=AUTH).status_code == 204
    assert client.get(f"/admin/agents/{config_id}", headers=AUTH).status_code == 404


def test_a_duplicate_slug_is_a_conflict_not_a_second_row(client):
    payload = {"slug": "finance-ops"}
    assert client.post("/admin/agents", headers=AUTH, json=payload).status_code == 201

    clash = client.post("/admin/agents", headers=AUTH, json=payload)
    assert clash.status_code == 409
    assert clash.json()["error"] == "conflict"
    assert "finance-ops" in clash.json()["message"]

    assert len(client.get("/admin/agents", headers=AUTH).json()) == 1


def test_an_unknown_model_tier_is_rejected_at_edit_time(client):
    bad = client.post("/admin/agents", headers=AUTH, json={"slug": "aa", "model_tier": "gigantic"})
    assert bad.status_code == 422
    assert bad.json()["error"] == "unprocessable"

    # A literal vendor/model id is the documented escape hatch and must pass.
    ok = client.post(
        "/admin/agents",
        headers=AUTH,
        json={"slug": "bb", "model_tier": "anthropic/claude-sonnet-4.6"},
    )
    assert ok.status_code == 201, ok.text

    # And it must be rejected on a PATCH too, not only on create.
    patched = client.patch(
        f"/admin/agents/{ok.json()['id']}", headers=AUTH, json={"model_tier": "gigantic"}
    )
    assert patched.status_code == 422


def test_a_malformed_slug_is_rejected(client):
    for slug in ("Capitalised", "has space", "1leading-digit", "trailing_underscore"):
        response = client.post("/admin/agents", headers=AUTH, json={"slug": slug})
        assert response.status_code == 422, f"{slug!r} should not be accepted"


def test_every_admin_route_requires_a_bearer_token(client):
    assert client.get("/admin/agents").status_code == 401
    assert client.post("/admin/agents", json={"slug": "x"}).status_code == 401
    assert client.get("/admin/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # A bare token without the scheme is not a token.
    assert client.get("/admin/agents", headers={"Authorization": ADMIN_TOKEN}).status_code == 401


def test_errors_use_the_ecosystem_envelope_not_fastapis_detail(client):
    body = client.get("/admin/agents/nope", headers=AUTH).json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "not_found"


def test_health_is_shallow_by_default_and_reads_a_row_when_deep(client):
    shallow = client.get("/health").json()
    assert shallow["status"] == "ok"
    assert "database" not in shallow

    deep = client.get("/health?deep=true").json()
    assert deep["status"] == "ok"
    assert deep["database"] == "ok"
    assert deep["agents"] == 0
