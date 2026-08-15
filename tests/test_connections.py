"""The connection library: what it means for a connection to be shared.

The tests that matter most here are the ownership ones. Bloom's schema always had
a binding table and a ``shared`` flag, and the flag could never be set — the OAuth
exchange stamped an owning agent every time, so deleting an agent deleted its
credentials. These pin the inversion: a connection is a library entry, and the only
thing deleting an agent removes is the attachment.

No network. Nothing here needs a provider to exist.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db as db_module
from app import trace as trace_module
from app.config import get_settings

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
KEY = Fernet.generate_key().decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
    monkeypatch.setenv("BLOOM_FEATURE_OAUTH", "true")
    monkeypatch.setenv("BLOOM_FERNET_KEYS", KEY)
    monkeypatch.setenv("BLOOM_PUBLIC_URL", "https://bloom.example")
    monkeypatch.setenv("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", "csec")

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


def _agent(client, slug: str) -> dict:
    response = client.post("/admin/agents", headers=AUTH, json={"slug": slug})
    assert response.status_code == 201, response.text
    return response.json()


def _peer(client, name="amber", url="https://amber.example", **over) -> dict:
    body = {"kind": "mcp", "name": name, "config": {"url": url}, **over}
    response = client.post("/admin/connections", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --- the library model ---------------------------------------------------------


def test_a_connection_made_from_an_agent_is_attached_and_in_the_library(client):
    """One call, because "add a connection to this agent" is one intent.

    Two would invent a half-done state — a connection that exists but is attached
    to nothing — that every client would have to detect and recover from.
    """
    agent = _agent(client, "dj")
    connection = _peer(client, attach_to=[agent["id"]])

    assert connection["agent_ids"] == [agent["id"]]
    library = client.get("/admin/connections", headers=AUTH).json()
    assert [c["id"] for c in library] == [connection["id"]]
    attached = client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).json()
    assert [c["id"] for c in attached] == [connection["id"]]


def test_deleting_an_agent_leaves_its_connections_alive(client):
    """The exact inversion of what deleting an agent used to do.

    A connection is not a possession of whichever agent created it. Deleting that
    agent must not revoke a credential another agent is using — or one you would
    only have to authorise again.
    """
    first, second = _agent(client, "dj"), _agent(client, "party")
    connection = _peer(client, attach_to=[first["id"], second["id"]])

    assert client.delete(f"/admin/agents/{first['id']}", headers=AUTH).status_code == 204

    assert client.get(f"/admin/connections/{connection['id']}", headers=AUTH).status_code == 200
    survivors = client.get(f"/admin/agents/{second['id']}/connections", headers=AUTH).json()
    assert [c["id"] for c in survivors] == [connection["id"]]


def test_one_connection_serves_two_agents_and_detaching_one_leaves_the_other(client):
    first, second = _agent(client, "dj"), _agent(client, "party")
    connection = _peer(client, attach_to=[first["id"], second["id"]])

    detached = client.delete(
        f"/admin/agents/{first['id']}/connections/{connection['id']}", headers=AUTH
    )
    assert detached.status_code == 204

    assert client.get(f"/admin/agents/{first['id']}/connections", headers=AUTH).json() == []
    kept = client.get(f"/admin/agents/{second['id']}/connections", headers=AUTH).json()
    assert [c["id"] for c in kept] == [connection["id"]]
    # And the connection itself is untouched.
    assert client.get(f"/admin/connections/{connection['id']}", headers=AUTH).status_code == 200


def test_attaching_an_existing_connection_is_how_reuse_works(client):
    first, second = _agent(client, "dj"), _agent(client, "party")
    connection = _peer(client, attach_to=[first["id"]])

    attached = client.post(
        f"/admin/agents/{second['id']}/connections",
        headers=AUTH,
        json={"connection_id": connection["id"]},
    )
    assert attached.status_code == 201, attached.text
    assert [c["id"] for c in attached.json()] == [connection["id"]]

    users = client.get(f"/admin/connections/{connection['id']}/agents", headers=AUTH).json()
    assert sorted(a["slug"] for a in users) == ["dj", "party"]


def test_deleting_an_attached_connection_names_who_would_lose_it(client):
    """The library model's one sharp edge, so it asks rather than surprises."""
    agent = _agent(client, "dj")
    connection = _peer(client, attach_to=[agent["id"]])

    refused = client.delete(f"/admin/connections/{connection['id']}", headers=AUTH)
    assert refused.status_code == 409, refused.text
    assert "dj" in refused.json()["message"]
    assert client.get(f"/admin/connections/{connection['id']}", headers=AUTH).status_code == 200

    forced = client.delete(f"/admin/connections/{connection['id']}?force=true", headers=AUTH)
    assert forced.status_code == 204
    assert client.get(f"/admin/connections/{connection['id']}", headers=AUTH).status_code == 404
    # And the agent's list no longer mentions it.
    assert client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).json() == []


def test_attach_to_an_unknown_agent_creates_nothing(client):
    """The whole reason create-and-attach is one transaction."""
    refused = client.post(
        "/admin/connections",
        headers=AUTH,
        json={
            "kind": "mcp",
            "name": "amber",
            "config": {"url": "https://amber.example"},
            "attach_to": ["nope"],
        },
    )
    assert refused.status_code == 404
    assert client.get("/admin/connections", headers=AUTH).json() == []


def test_a_second_connection_for_the_same_provider_on_one_agent_is_refused(client):
    """Provider tools are named `<provider>_<operation>`, checked at manifest load.

    Two would collide on tool name and the broker would silently keep whichever
    came first, so the refusal happens where a human can see it.
    """
    agent = _agent(client, "dj")
    first = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "oauth", "provider": "spotify", "attach_to": [agent["id"]]},
    ).json()
    second = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "oauth", "provider": "spotify", "name": "spotify2"},
    ).json()
    assert first["id"] != second["id"]

    clash = client.post(
        f"/admin/agents/{agent['id']}/connections",
        headers=AUTH,
        json={"connection_id": second["id"]},
    )
    assert clash.status_code == 409, clash.text
    assert "spotify" in clash.json()["message"]


def test_attaching_appends_rather_than_reordering(client):
    """Broker order is observable, so it should be a decision, not an accident."""
    agent = _agent(client, "dj")
    _peer(client, name="amber", attach_to=[agent["id"]])
    second = _peer(client, name="finance", url="https://finance.example")
    client.post(
        f"/admin/agents/{agent['id']}/connections",
        headers=AUTH,
        json={"connection_id": second["id"]},
    )

    attached = client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).json()
    assert [c["name"] for c in attached] == ["amber", "finance"]


# --- names, and why they are validated like tool namespaces --------------------


def test_a_duplicate_name_is_a_conflict(client):
    _peer(client, name="amber")
    clash = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "mcp", "name": "amber", "config": {"url": "https://other.example"}},
    )
    assert clash.status_code == 409
    assert clash.json()["error"] == "conflict"


def test_a_name_containing_a_double_underscore_is_refused(client):
    """For kind='mcp' the name is MCPClient's `<server>__<tool>` prefix.

    A '__' in it makes that split land in the wrong place, so a remote tool would
    be dispatched to the wrong server — or to none.
    """
    refused = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "mcp", "name": "am__ber", "config": {"url": "https://a.example"}},
    )
    assert refused.status_code == 422
    assert "__" in refused.json()["message"]


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"kind": "mcp", "name": "a"}, "config.url"),
        (
            {"kind": "mcp", "name": "a", "config": {"url": "u"}, "provider": "spotify"},
            "no provider",
        ),
        ({"kind": "oauth"}, "needs a provider"),
        ({"kind": "wat", "provider": "spotify"}, "kind must be one of"),
    ],
)
def test_an_incoherent_connection_is_refused_with_the_reason(client, body, expected):
    response = client.post("/admin/connections", headers=AUTH, json=body)
    assert response.status_code == 422, response.text
    assert expected in response.json()["message"]


# --- secrets never come back out -----------------------------------------------


def test_no_response_ever_carries_a_secret(client):
    agent = _agent(client, "dj")
    connection = client.post(
        "/admin/connections",
        headers=AUTH,
        json={
            "kind": "api_key",
            "provider": "github",
            "secret": "ghp_supersecret",
            "client_secret": "app-secret",
            "client_id": "app-id",
            "attach_to": [agent["id"]],
        },
    ).json()

    bodies = [
        client.get("/admin/connections", headers=AUTH).text,
        client.get(f"/admin/connections/{connection['id']}", headers=AUTH).text,
        client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).text,
    ]
    for body in bodies:
        assert "ghp_supersecret" not in body
        assert "app-secret" not in body

    # What a UI actually wanted from a secret column: whether one is set.
    assert connection["has_secret"] is True
    assert connection["has_client_secret"] is True
    # The client id is not a secret — it travels in every authorize URL — and the
    # user has to be able to see which app a connection is bound to.
    assert connection["config"]["client_id"] == "app-id"


def test_a_patch_carrying_a_secret_is_refused(client):
    """Otherwise a plaintext key ends up in a request log on an ordinary edit."""
    connection = _peer(client)
    refused = client.patch(
        f"/admin/connections/{connection['id']}", headers=AUTH, json={"secret": "oops"}
    )
    assert refused.status_code == 422
    assert "secret" in refused.json()["message"]


def test_pasting_a_key_is_what_makes_an_api_key_connection_usable(client):
    created = client.post(
        "/admin/connections", headers=AUTH, json={"kind": "api_key", "provider": "github"}
    ).json()
    assert created["status"] == "pending"
    assert created["has_secret"] is False

    updated = client.post(
        f"/admin/connections/{created['id']}/secret", headers=AUTH, json={"secret": "ghp_x"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "active"
    assert updated.json()["has_secret"] is True


def test_an_oauth_access_token_cannot_be_pasted(client):
    """It comes from the provider. Accepting one would invite pasting a stale token."""
    connection = client.post(
        "/admin/connections", headers=AUTH, json={"kind": "oauth", "provider": "spotify"}
    ).json()
    refused = client.post(
        f"/admin/connections/{connection['id']}/secret", headers=AUTH, json={"secret": "at"}
    )
    assert refused.status_code == 422
    assert "oauth/start" in refused.json()["message"]


def test_rotating_a_client_secret_leaves_the_users_grant_alone(client):
    """They rotate on completely different schedules."""
    connection = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "oauth", "provider": "spotify", "client_id": "a", "client_secret": "b"},
    ).json()
    store = db_module.get_store()
    from app.crypto import encrypt

    store.set_connection_secret(
        connection["id"], secret=encrypt("user-grant", get_settings()), status="active"
    )

    client.post(
        f"/admin/connections/{connection['id']}/secret",
        headers=AUTH,
        json={"client_secret": "rolled"},
    )

    from app.crypto import decrypt

    secrets = store.connection_secrets(connection["id"])
    assert decrypt(secrets["secret"], get_settings()) == "user-grant"
    assert decrypt(secrets["client_secret"], get_settings()) == "rolled"
    assert store.get_connection(connection["id"])["status"] == "active"


# --- a peer needs no encryption key at all -------------------------------------


def test_a_tokenless_peer_is_active_immediately(client):
    """A peer on a trusted network has no secret to store badly."""
    assert _peer(client)["status"] == "active"


def test_what_a_connection_gives_an_agent_is_reported_before_attaching(client):
    """The question a picker exists to answer: what do I get?"""
    peer = _peer(client, name="amber")
    assert peer["tools"] == ["amber__*"]

    spotify = client.post(
        "/admin/connections",
        headers=AUTH,
        json={"kind": "oauth", "provider": "spotify", "scopes": ["user-modify-playback-state"]},
    ).json()
    assert "spotify_play" in spotify["tools"]
    # Scope-filtered exactly as the runner would: no scope, no tool.
    assert "spotify_now_playing" not in spotify["tools"]


def test_an_api_key_is_unscoped_rather_than_empty(client):
    """Its permissions live in the provider's console and Bloom cannot read them."""
    created = client.post(
        "/admin/connections", headers=AUTH, json={"kind": "api_key", "provider": "github"}
    ).json()
    assert "github_whoami" in created["tools"]
    assert "github_list_issues" in created["tools"]


# --- auth ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/connections"),
        ("post", "/admin/connections"),
        ("get", "/admin/connections/kinds"),
        ("get", "/admin/connections/x"),
        ("delete", "/admin/connections/x"),
        ("post", "/admin/connections/x/test"),
        ("post", "/admin/connections/x/revoke"),
    ],
)
def test_every_connection_route_needs_a_bearer_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_kinds_is_not_read_as_a_connection_id(client):
    """Route order: declared after /connections/{id} this would 404 every time."""
    assert client.get("/admin/connections/kinds", headers=AUTH).status_code == 200


# --- the database this version cannot read -------------------------------------


def test_a_database_from_before_connections_is_refused_at_startup(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op against an old file.

    Without this the mismatch surfaces deep inside a route, as a pydantic error
    about a column nobody has heard of. Bloom has never been deployed, so the
    honest handling is to say what happened and where the delete button is.
    """
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE oauth_connections (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(db_module.SchemaTooOld) as caught:
        db_module.Store(path)

    message = str(caught.value)
    assert "oauth_connections" in message
    assert path in message
    assert "-wal" in message


def test_a_legacy_column_on_agent_configs_is_caught_too(tmp_path):
    import sqlite3

    path = str(tmp_path / "old2.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE agent_configs (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL DEFAULT '', "
        "model_tier TEXT NOT NULL DEFAULT 'balanced', mcp_servers_json TEXT NOT NULL DEFAULT '[]', "
        "max_steps INTEGER, max_cost_usd REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(db_module.SchemaTooOld, match="mcp_servers_json"):
        db_module.Store(path)


def test_a_fresh_database_is_fine(tmp_path):
    store = db_module.Store(str(tmp_path / "new.db"))
    assert store.list_connections() == []
    store.close()
