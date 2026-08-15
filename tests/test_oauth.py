"""Provider manifests, encryption, the handshake, and token refresh.

No network. Every HTTP call goes through an injected client factory, so the tests
drive the real code paths — including the 401-refresh-retry and the rotating
refresh token — without a provider being involved.

The manifest validation tests are the important ones: every rule they pin exists
because breaking it produces a failure somewhere far away from the file that
caused it, usually inside a model's context.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import credentials as credentials_module
from app import db as db_module
from app import trace as trace_module
from app.config import Settings, get_settings
from app.crypto import EncryptionUnavailable, UndecryptableToken, decrypt, encrypt
from app.providers import registry as registry_module

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:", "feature_oauth": True, "fernet_keys": KEY_A}
    return Settings(**{**base, **over})


# --- a stand-in for httpx2.AsyncClient ---------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Returns queued responses and records every request it was given."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def factory(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.requests.append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        )
        return self.responses.pop(0) if self.responses else FakeResponse(200, {})

    async def post(self, url, *, data=None, headers=None, timeout=None):
        self.requests.append({"method": "POST", "url": url, "data": data, "headers": headers})
        return self.responses.pop(0) if self.responses else FakeResponse(200, {})


# --- encryption ---------------------------------------------------------------


def test_a_token_round_trips_and_is_not_stored_in_the_clear():
    settings = _settings()
    blob = encrypt("super-secret-token", settings)
    assert b"super-secret-token" not in blob
    assert decrypt(blob, settings) == "super-secret-token"


def test_an_empty_value_stores_as_empty_rather_than_as_noise():
    settings = _settings()
    assert encrypt("", settings) == b""
    assert decrypt(b"", settings) == ""
    assert decrypt(None, settings) == ""


def test_a_rotated_key_still_decrypts_what_the_old_one_wrote():
    """The reason MultiFernet is used from day one: the head encrypts, all decrypt,
    so prepending a key is a restart rather than a migration."""
    old = _settings(fernet_keys=KEY_A)
    blob = encrypt("token", old)

    rotated = _settings(fernet_keys=f"{KEY_B},{KEY_A}")
    assert decrypt(blob, rotated) == "token"
    # New writes use the head key, which the old configuration cannot read.
    fresh = encrypt("token", rotated)
    with pytest.raises(UndecryptableToken):
        decrypt(fresh, old)


def test_no_key_is_a_clear_error_that_does_not_echo_the_value():
    with pytest.raises(EncryptionUnavailable) as exc:
        encrypt("token", _settings(fernet_keys=""))
    assert "BLOOM_FERNET_KEYS" in str(exc.value)

    with pytest.raises(EncryptionUnavailable) as exc:
        encrypt("token", _settings(fernet_keys="not-a-fernet-key"))
    assert "not-a-fernet-key" not in str(exc.value)


# --- manifests ----------------------------------------------------------------


def test_the_shipped_spotify_manifest_loads_and_names_its_tools_legally():
    provider = registry_module.providers()["spotify"]
    assert provider.display_name == "Spotify"
    names = {op.tool_name("spotify") for op in provider.operations}
    assert "spotify_play" in names
    for name in names:
        assert len(name) <= registry_module.MAX_TOOL_NAME_LEN
        assert "__" not in name
        assert registry_module._TOOL_NAME.match(name)


def _write(tmp_path, body: str):
    path = tmp_path / "x.toml"
    path.write_text(body, encoding="utf-8")
    return path


_HEAD = """
name = "demo"
authorize_url = "https://a"
token_url = "https://t"
api_base = "https://api"
client_id_env = "DEMO_ID"
"""


def test_a_parameter_that_could_carry_a_credential_is_refused(tmp_path):
    """This denylist is what keeps a secret out of Step.tool_calls, out of the
    runtime's INFO log, and out of Bloom's own persisted trace."""
    for name in ("authorization", "token", "access_token", "api_key", "cookie"):
        path = _write(
            tmp_path,
            _HEAD
            + f"""
[[operations]]
name = "go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
{name} = {{ in = "query", type = "string" }}
""",
        )
        with pytest.raises(registry_module.ManifestError, match="not allowed"):
            registry_module.load_manifest(path)


def test_a_header_parameter_is_refused_outright(tmp_path):
    path = _write(
        tmp_path,
        _HEAD
        + """
[[operations]]
name = "go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
x_custom = { in = "header", type = "string" }
""",
    )
    with pytest.raises(registry_module.ManifestError, match="headers are refused"):
        registry_module.load_manifest(path)


def test_a_tool_name_that_would_collide_with_mcp_namespacing_is_refused(tmp_path):
    """`__` is MCPClient's <server>__<tool> separator; a tool containing one makes
    that namespacing ambiguous."""
    path = _write(
        tmp_path,
        _HEAD
        + """
[[operations]]
name = "_go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
""",
    )
    with pytest.raises(registry_module.ManifestError, match="__"):
        registry_module.load_manifest(path)


def test_an_operation_without_a_description_is_refused(tmp_path):
    """A manifest-driven tool has no docstring, so this string is the only thing
    the model sees before deciding whether to call it."""
    path = _write(
        tmp_path,
        _HEAD
        + """
[[operations]]
name = "go"
method = "GET"
path = "/go"
[operations.params]
""",
    )
    with pytest.raises(registry_module.ManifestError, match="description"):
        registry_module.load_manifest(path)


def test_operations_are_hidden_when_the_grant_lacks_their_scope():
    """Offering a tool the token cannot authorise means the model spends a step to
    learn something that was knowable before the run started."""
    provider = registry_module.providers()["spotify"]

    read_only = {op.name for op in registry_module.operations_for(provider, [])}
    assert "search" in read_only
    assert "play" not in read_only

    with_write = {
        op.name for op in registry_module.operations_for(provider, ["user-modify-playback-state"])
    }
    assert "play" in with_write


# --- the synthesised tools ----------------------------------------------------


class FakeResolver:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(tokens)
        self.forced = 0
        self.reauth: list[str] = []

    async def access_token(self, connection_id, *, force_refresh=False):
        if force_refresh:
            self.forced += 1
        return self.tokens.pop(0) if self.tokens else ""

    async def mark_needs_reauth(self, connection_id):
        self.reauth.append(connection_id)


def _spotify_broker(resolver, client, scopes=("user-modify-playback-state",)):
    from agent_runtime import LocalToolBroker

    broker = LocalToolBroker()
    registry_module.register_operations(
        broker,
        registry_module.providers()["spotify"],
        "conn-1",
        resolver,
        granted_scopes=scopes,
        http_client_factory=client.factory,
    )
    return broker


def test_a_synthesised_tool_calls_the_api_with_the_token_in_the_header_only():
    """The token reaches the provider and appears nowhere the model can see —
    not in the arguments, not in the result."""
    client = FakeClient([FakeResponse(200, {"is_playing": True})])
    resolver = FakeResolver(["live-token"])
    broker = _spotify_broker(resolver, client)

    result = asyncio.run(broker.call_tool("spotify_play", {"uris": ["spotify:track:1"]}))

    request = client.requests[0]
    assert request["method"] == "PUT"
    assert request["url"] == "https://api.spotify.com/v1/me/player/play"
    assert request["json"] == {"uris": ["spotify:track:1"]}
    assert request["headers"]["Authorization"] == "Bearer live-token"
    assert "live-token" not in result


def test_query_and_body_parameters_are_sorted_by_the_manifest():
    client = FakeClient([FakeResponse(200, {"tracks": {"items": []}})])
    broker = _spotify_broker(FakeResolver(["t"]), client, scopes=())

    asyncio.run(broker.call_tool("spotify_search", {"q": "creep", "type": "track"}))

    request = client.requests[0]
    assert request["params"] == {"q": "creep", "type": "track", "limit": 10}
    assert request["json"] is None


def test_an_undeclared_argument_is_dropped_rather_than_forwarded():
    """A model that invents a parameter should get the documented call, not an
    opaque 400 from the provider."""
    client = FakeClient([FakeResponse(200, {})])
    broker = _spotify_broker(FakeResolver(["t"]), client, scopes=())

    asyncio.run(broker.call_tool("spotify_search", {"q": "x", "type": "track", "made_up": 1}))
    assert "made_up" not in (client.requests[0]["params"] or {})


def test_a_401_triggers_exactly_one_forced_refresh_and_one_retry():
    client = FakeClient([FakeResponse(401, None, "expired"), FakeResponse(200, {"ok": True})])
    resolver = FakeResolver(["stale-token", "fresh-token"])
    broker = _spotify_broker(resolver, client)

    result = asyncio.run(broker.call_tool("spotify_pause", {}))

    assert resolver.forced == 1
    assert len(client.requests) == 2
    assert client.requests[1]["headers"]["Authorization"] == "Bearer fresh-token"
    assert "ok" in result


def test_a_second_401_marks_the_connection_for_reauth_and_says_so_in_words():
    """A tool that raises kills the turn; a sentence lets the model tell the user
    what to actually do."""
    client = FakeClient([FakeResponse(401, None, "no"), FakeResponse(401, None, "no")])
    resolver = FakeResolver(["a", "b"])
    broker = _spotify_broker(resolver, client)

    result = asyncio.run(broker.call_tool("spotify_pause", {}))

    assert resolver.reauth == ["conn-1"]
    assert "reconnect" in result.lower()


def test_a_disconnected_connection_answers_with_prose_not_an_exception():
    broker = _spotify_broker(FakeResolver([]), FakeClient([]))
    result = asyncio.run(broker.call_tool("spotify_pause", {}))
    assert "not connected" in result.lower() or "expired" in result.lower()


def test_an_error_status_is_reported_without_echoing_the_request_headers():
    client = FakeClient([FakeResponse(404, None, "No active device found")])
    broker = _spotify_broker(FakeResolver(["t"]), client)
    result = asyncio.run(broker.call_tool("spotify_pause", {}))
    assert result.startswith("HTTP 404")
    assert "Authorization" not in result


# --- the credential resolver --------------------------------------------------


def _connected_store(tmp_path, settings, *, expires_in_s: int, refresh: str | None = "r0"):
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj")
    expires = (datetime.now(UTC) + timedelta(seconds=expires_in_s)).replace(microsecond=0)
    connection = store.upsert_connection(
        provider="spotify",
        agent_config_id=config["id"],
        access_token=encrypt("access-0", settings),
        refresh_token=encrypt(refresh, settings) if refresh else None,
        expires_at=expires.isoformat(),
        scopes=["user-modify-playback-state"],
    )
    return store, config, connection


def test_a_valid_token_is_returned_without_touching_the_network(tmp_path):
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=3600)
    client = FakeClient([])
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    assert asyncio.run(resolver.access_token(connection["id"])) == "access-0"
    assert client.requests == []
    store.close()


def test_a_token_inside_the_skew_window_is_refreshed_before_it_is_handed_out(tmp_path):
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=10)
    client = FakeClient([FakeResponse(200, {"access_token": "access-1", "expires_in": 3600})])
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    monkey = {"BLOOM_OAUTH_SPOTIFY_CLIENT_ID": "id", "BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET": "sec"}
    import os

    os.environ.update(monkey)
    try:
        assert asyncio.run(resolver.access_token(connection["id"])) == "access-1"
    finally:
        for key in monkey:
            os.environ.pop(key, None)

    # Client credentials went in the Basic header, per the manifest's auth_style.
    sent = client.requests[0]
    assert sent["data"]["grant_type"] == "refresh_token"
    assert sent["headers"]["Authorization"].startswith("Basic ")
    assert base64.b64decode(sent["headers"]["Authorization"][6:]).decode() == "id:sec"

    # And the new token is what is stored, encrypted.
    secrets = store.connection_secrets(connection["id"])
    assert decrypt(secrets["access_token"], settings) == "access-1"
    store.close()


def test_a_refresh_that_omits_a_new_refresh_token_keeps_the_stored_one(tmp_path):
    """Providers that do not rotate simply omit the field. Treating that as a
    revocation would brick the connection on its first successful refresh."""
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=10)
    client = FakeClient([FakeResponse(200, {"access_token": "a1", "expires_in": 60})])
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    import os

    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_ID"] = "id"
    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET"] = "sec"
    try:
        asyncio.run(resolver.access_token(connection["id"]))
    finally:
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", None)
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", None)

    secrets = store.connection_secrets(connection["id"])
    assert decrypt(secrets["refresh_token"], settings) == "r0"
    store.close()


def test_an_expired_connection_with_no_refresh_token_is_marked_expired(tmp_path):
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=-10, refresh=None)
    resolver = credentials_module.CredentialResolver(store, settings)

    assert asyncio.run(resolver.access_token(connection["id"])) == ""
    assert store.get_connection(connection["id"])["status"] == "expired"
    store.close()


def test_a_5xx_during_refresh_does_not_condemn_the_connection(tmp_path):
    """A provider having a bad day must not make a human reconnect something that
    was never broken. Only a 4xx means the grant itself is gone."""
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=10)
    client = FakeClient([FakeResponse(503, None, "try later")])
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    import os

    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_ID"] = "id"
    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET"] = "sec"
    try:
        assert asyncio.run(resolver.access_token(connection["id"])) == ""
    finally:
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", None)
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", None)

    assert store.get_connection(connection["id"])["status"] == "active"
    store.close()


def test_concurrent_callers_refresh_once_not_twice(tmp_path):
    """For a provider with rotating refresh tokens, losing this race permanently
    breaks the grant — the loser presents a token the provider already retired."""
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=10)
    client = FakeClient(
        [FakeResponse(200, {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600})]
    )
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    import os

    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_ID"] = "id"
    os.environ["BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET"] = "sec"
    try:

        async def both():
            return await asyncio.gather(
                resolver.access_token(connection["id"]),
                resolver.access_token(connection["id"]),
            )

        assert asyncio.run(both()) == ["a1", "a1"]
    finally:
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", None)
        os.environ.pop("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", None)

    token_posts = [
        r for r in client.requests if r["url"] == "https://accounts.spotify.com/api/token"
    ]
    assert len(token_posts) == 1
    store.close()


# --- the handshake ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
    monkeypatch.setenv("BLOOM_FEATURE_OAUTH", "true")
    monkeypatch.setenv("BLOOM_FERNET_KEYS", KEY_A)
    monkeypatch.setenv("BLOOM_PUBLIC_URL", "https://bloom.example")
    monkeypatch.setenv("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", "client-secret")

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


def _agent(client) -> dict:
    return client.post("/admin/agents", headers=AUTH, json={"slug": "dj"}).json()


def test_start_returns_an_authorize_url_carrying_state_and_a_pkce_challenge(client):
    agent = _agent(client)
    response = client.post(
        f"/admin/agents/{agent['id']}/oauth/spotify/start", headers=AUTH, json={}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    parsed = urlparse(body["authorize_url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.spotify.com"
    assert query["client_id"] == ["client-id"]
    assert query["state"] == [body["state"]]
    assert query["code_challenge_method"] == ["S256"]
    # The verifier itself must never travel through the browser.
    assert "code_verifier" not in query
    assert query["redirect_uri"] == ["https://bloom.example/admin/oauth/spotify/callback"]


def test_the_callback_is_reachable_without_a_bearer_token(client, monkeypatch):
    """It has to be: the provider redirects a browser, which carries no token."""
    agent = _agent(client)
    state = client.post(
        f"/admin/agents/{agent['id']}/oauth/spotify/start", headers=AUTH, json={}
    ).json()["state"]

    fake = FakeClient(
        [
            FakeResponse(
                200,
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                    "scope": "user-modify-playback-state",
                },
            )
        ]
    )
    import app.oauth.flow as flow_module

    real_exchange = flow_module.exchange

    async def patched(store, provider, code, state_row, **kw):
        return await real_exchange(
            store, provider, code, state_row, http_client_factory=fake.factory
        )

    monkeypatch.setattr("app.admin.oauth_routes.exchange", patched)

    # No AUTH header on purpose.
    response = client.get(f"/admin/oauth/spotify/callback?code=abc&state={state}")
    assert response.status_code == 200
    assert "Spotify connected" in response.text
    # The page fires the deep link *and* tells the user what to do, because the
    # protocol handler does not exist yet.
    assert "aperture://oauth-complete?provider=spotify&status=success" in response.text
    assert "close this tab" in response.text

    status = client.get(f"/admin/agents/{agent['id']}/oauth", headers=AUTH).json()
    assert status[0]["provider"] == "spotify"
    assert status[0]["status"] == "active"
    assert status[0]["scopes"] == ["user-modify-playback-state"]
    # No token value, ever.
    assert "access_token" not in status[0]


def test_a_state_can_only_be_used_once(client):
    agent = _agent(client)
    state = client.post(
        f"/admin/agents/{agent['id']}/oauth/spotify/start", headers=AUTH, json={}
    ).json()["state"]

    store = db_module.get_store()
    assert store.consume_oauth_state(state) is not None

    replayed = client.get(f"/admin/oauth/spotify/callback?code=abc&state={state}")
    assert replayed.status_code == 400
    assert "expired or was already used" in replayed.text


def test_the_callback_answers_a_denied_authorization_with_prose(client):
    response = client.get("/admin/oauth/spotify/callback?error=access_denied")
    assert response.status_code == 400
    assert "not completed" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_disconnecting_zeroes_the_tokens_but_keeps_the_row(client, monkeypatch):
    agent = _agent(client)
    store = db_module.get_store()
    settings = get_settings()
    store.upsert_connection(
        provider="spotify",
        agent_config_id=agent["id"],
        access_token=encrypt("at", settings),
        refresh_token=encrypt("rt", settings),
        expires_at=None,
        scopes=["user-modify-playback-state"],
    )

    assert (
        client.delete(f"/admin/agents/{agent['id']}/oauth/spotify", headers=AUTH).status_code == 204
    )

    status = client.get(f"/admin/agents/{agent['id']}/oauth", headers=AUTH).json()
    assert status[0]["status"] == "revoked"
    secrets = store.connection_secrets(status[0]["id"])
    assert not secrets["access_token"]
    assert not secrets["refresh_token"]


def test_the_provider_list_reports_whether_credentials_are_actually_configured(client):
    body = client.get("/admin/oauth/providers", headers=AUTH).json()
    spotify = next(p for p in body if p["name"] == "spotify")
    assert spotify["configured"] is True
    assert "spotify_play" in spotify["operations"]


def test_oauth_routes_refuse_when_no_encryption_key_is_configured(client, monkeypatch):
    """Storing a token without a key is a breach, not a degraded mode."""
    agent = _agent(client)
    monkeypatch.setenv("BLOOM_FERNET_KEYS", "")
    get_settings.cache_clear()

    refused = client.post(f"/admin/agents/{agent['id']}/oauth/spotify/start", headers=AUTH, json={})
    assert refused.status_code == 503
    assert refused.json()["error"] == "unavailable"


def test_starting_a_flow_for_an_unknown_provider_is_a_404(client):
    agent = _agent(client)
    response = client.post(f"/admin/agents/{agent['id']}/oauth/nope/start", headers=AUTH, json={})
    assert response.status_code == 404


# --- the refresh sweep --------------------------------------------------------


def test_the_sweep_refreshes_only_what_is_close_to_expiry(tmp_path):
    from app.oauth.refresh import refresh_due

    settings = _settings(oauth_refresh_skew_s=600)
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj")

    def _connect(provider_suffix, seconds):
        expires = (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0)
        return store.upsert_connection(
            provider="spotify",
            agent_config_id=config["id"] + provider_suffix,
            access_token=encrypt("a", settings),
            refresh_token=encrypt("r", settings),
            expires_at=expires.isoformat(),
            scopes=[],
        )

    soon = _connect("-soon", 60)
    later = _connect("-later", 86400)

    class OneShotResolver:
        def __init__(self):
            self.seen = []

        async def access_token(self, connection_id, *, force_refresh=False):
            self.seen.append(connection_id)
            return "new"

    resolver = OneShotResolver()
    assert asyncio.run(refresh_due(store, settings, resolver=resolver)) == 1
    assert resolver.seen == [soon["id"]]
    assert later["id"] not in resolver.seen
    store.close()
