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


# --- migrating a database written before connections existed -------------------
#
# 0.2.0 refused these files instead of carrying them across, which crash-looped every
# existing install at boot. What follows is the 0.1.0 schema verbatim, so these
# assert against the thing that is actually on disk rather than against a summary
# of it.

_V010_SCHEMA = """
CREATE TABLE agent_configs (
    id               TEXT    PRIMARY KEY,
    slug             TEXT    NOT NULL UNIQUE,
    name             TEXT    NOT NULL DEFAULT '',
    system_prompt    TEXT    NOT NULL DEFAULT '',
    model_tier       TEXT    NOT NULL DEFAULT 'balanced',
    mcp_servers_json TEXT    NOT NULL DEFAULT '[]',
    max_steps        INTEGER,
    max_cost_usd     REAL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE TABLE oauth_connections (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    agent_config_id TEXT,
    access_token    BLOB,
    refresh_token   BLOB,
    expires_at      TEXT,
    scopes_json     TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    encrypted_at    TEXT,
    last_used_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE agent_config_oauth (
    agent_config_id     TEXT NOT NULL,
    oauth_connection_id TEXT NOT NULL,
    PRIMARY KEY (agent_config_id, oauth_connection_id)
);
CREATE TABLE oauth_states (
    state           TEXT PRIMARY KEY,
    agent_config_id TEXT NOT NULL,
    provider        TEXT NOT NULL,
    code_verifier   TEXT NOT NULL DEFAULT '',
    redirect_uri    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""

_T = "2026-01-01T00:00:00Z"


def _v010_database(path, *, peers="[]", owner=None):
    """A 0.1.0 file holding one agent, one credential and one binding."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(_V010_SCHEMA)
    conn.execute(
        "INSERT INTO agent_configs (id, slug, name, system_prompt, model_tier, "
        "mcp_servers_json, created_at, updated_at) VALUES ('a1','helper','Helper','be brief',"
        f"'balanced','{peers}','{_T}','{_T}')"
    )
    conn.execute(
        "INSERT INTO oauth_connections (id, provider, agent_config_id, access_token, "
        "refresh_token, scopes_json, status, created_at, updated_at) "
        "VALUES ('c1','spotify',?,?,?,'[\"read\"]','active',?,?)",
        (owner, b"cipher-access", b"cipher-refresh", _T, _T),
    )
    if owner is None:
        conn.execute(
            "INSERT INTO agent_config_oauth (agent_config_id, oauth_connection_id) "
            "VALUES ('a1','c1')"
        )
    conn.execute(
        "INSERT INTO oauth_states (state, agent_config_id, provider, created_at, expires_at) "
        f"VALUES ('s1','a1','spotify','{_T}','{_T}')"
    )
    conn.commit()
    conn.close()


def test_a_010_database_is_migrated_rather_than_refused(tmp_path):
    """The whole point: an existing install boots, keeping what it had."""
    path = str(tmp_path / "old.db")
    _v010_database(path, peers='["amber"]')

    store = db_module.Store(path, peers={"amber": {"url": "https://amber.example", "secret": None}})

    connections = {c["name"]: c for c in store.list_connections()}
    assert connections["spotify"]["kind"] == "oauth"
    assert connections["spotify"]["provider"] == "spotify"
    assert connections["spotify"]["status"] == "active"
    assert connections["spotify"]["scopes"] == ["read"]
    assert connections["spotify"]["has_secret"] is True
    # The id survives, so nothing that already pointed at this credential has to be
    # rewritten — and re-running the migration re-derives it instead of duplicating.
    assert connections["spotify"]["id"] == "c1"
    # The bytes survive too. A migration that silently emptied a token column would
    # look like a clean upgrade and fail on the next model call.
    assert store.connection_secrets("c1")["secret"] == b"cipher-access"

    assert connections["amber"]["kind"] == "mcp"
    assert connections["amber"]["provider"] is None
    assert connections["amber"]["config"] == {"url": "https://amber.example"}
    assert connections["amber"]["status"] == "active"

    # Attached, in broker order: credentials before peers, as before the change.
    assert [c["name"] for c in store.connections_for("a1")] == ["spotify", "amber"]
    store.close()


def test_migration_retires_the_old_shapes(tmp_path):
    """The legacy tables and column go, or `_config_row` hands pydantic a stranger."""
    path = str(tmp_path / "old.db")
    _v010_database(path, peers='["amber"]')
    store = db_module.Store(path)

    with store._lock:  # noqa: SLF001 — asserting on the schema itself
        tables = {
            r["name"]
            for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(agent_configs)")}
        states = {r["name"] for r in store._conn.execute("PRAGMA table_info(oauth_states)")}
    assert "oauth_connections" not in tables
    assert "agent_config_oauth" not in tables
    assert "mcp_servers_json" not in columns
    # Rebuilt, not left in its old shape: a flow now belongs to a connection.
    assert "connection_id" in states and "agent_config_id" not in states

    assert store.get_config("a1")["slug"] == "helper"
    store.close()


def test_the_ownership_column_becomes_an_edge_too(tmp_path):
    """0.1.0 recorded reach two ways. Reading only one of them would drop credentials."""
    path = str(tmp_path / "owned.db")
    _v010_database(path, owner="a1")  # no row in agent_config_oauth at all

    store = db_module.Store(path)
    assert [c["id"] for c in store.connections_for("a1")] == ["c1"]
    store.close()


def test_a_peer_with_no_configured_url_survives_as_pending(tmp_path):
    """Dropping it would erase the only record that the agent was wired to it."""
    path = str(tmp_path / "nourl.db")
    _v010_database(path, peers='["ghost"]')

    store = db_module.Store(path, peers={})  # nothing in BLOOM_MCP_PEERS
    ghost = next(c for c in store.list_connections() if c["name"] == "ghost")
    assert ghost["status"] == "pending"
    assert ghost["config"] == {"url": ""}
    assert [c["name"] for c in store.connections_for("a1")] == ["spotify", "ghost"]
    store.close()


def test_migrating_is_idempotent(tmp_path):
    """Re-opening the file must not duplicate a thing — including after a crash."""
    path = str(tmp_path / "twice.db")
    _v010_database(path, peers='["amber"]')

    first = db_module.Store(path, peers={"amber": {"url": "https://amber.example"}})
    before = [(c["id"], c["name"]) for c in first.list_connections()]
    first.close()

    second = db_module.Store(path, peers={"amber": {"url": "https://amber.example"}})
    assert [(c["id"], c["name"]) for c in second.list_connections()] == before
    assert len(second.connections_for("a1")) == 2
    second.close()


def test_two_credentials_for_one_provider_get_distinct_names(tmp_path):
    """`name` is UNIQUE and, for peers, a tool namespace. A collision must not insert."""
    import sqlite3

    path = str(tmp_path / "dupe.db")
    _v010_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO oauth_connections (id, provider, access_token, scopes_json, status, "
        f"created_at, updated_at) VALUES ('c2','spotify',NULL,'[]','active','{_T}','{_T}')"
    )
    conn.commit()
    conn.close()

    store = db_module.Store(path)
    assert sorted(c["name"] for c in store.list_connections()) == ["spotify", "spotify-2"]
    store.close()


def test_a_peer_name_the_namespace_rule_rejects_is_repaired_not_dropped(tmp_path):
    """0.1.0 never validated these names; `<name>__<tool>` now depends on them."""
    path = str(tmp_path / "odd.db")
    _v010_database(path, peers='["9 Live!"]')

    store = db_module.Store(path)
    names = {c["name"] for c in store.list_connections()}
    assert names == {"spotify", "c-9-live"}
    # The original is kept as the label, so the row still says where it came from.
    peer = next(c for c in store.list_connections() if c["kind"] == "mcp")
    assert peer["label"] == "9 Live!"
    store.close()


def test_a_fresh_database_is_fine(tmp_path):
    store = db_module.Store(str(tmp_path / "new.db"))
    assert store.list_connections() == []
    store.close()


def test_a_fresh_database_is_stamped_so_migrations_do_not_re_run(tmp_path):
    """A file with no version row reads as 0, exactly like a 0.1.0 one."""
    path = str(tmp_path / "stamp.db")
    store = db_module.Store(path)
    with store._lock:  # noqa: SLF001
        row = store._conn.execute("SELECT MAX(version) AS v FROM bloom_schema_version").fetchone()
    assert row["v"] == len(db_module._MIGRATIONS)
    store.close()
