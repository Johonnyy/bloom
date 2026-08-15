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


def test_every_shipped_manifest_loads_and_names_its_tools_legally():
    """Generalised over the directory rather than named per provider.

    A manifest is meant to be addable without touching Python; a test that names
    each one would be the one place you still had to.
    """
    shipped = registry_module.providers()
    assert {"spotify", "github"} <= set(shipped)
    assert shipped["spotify"].display_name == "Spotify"
    assert "spotify_play" in {op.tool_name("spotify") for op in shipped["spotify"].operations}

    for provider in shipped.values():
        assert provider.operations, f"{provider.name} declares no operations"
        for op in provider.operations:
            name = op.tool_name(provider.name)
            assert len(name) <= registry_module.MAX_TOOL_NAME_LEN
            assert "__" not in name
            assert registry_module._TOOL_NAME.match(name)


def test_a_provider_declares_which_kinds_of_connection_can_hold_its_credential():
    shipped = registry_module.providers()
    assert shipped["spotify"].supports("oauth")
    assert not shipped["spotify"].supports("api_key")
    assert shipped["github"].supports("oauth")
    assert shipped["github"].supports("api_key")
    # `mcp` is a connection kind but never a provider one — a peer has no manifest.
    assert not shipped["github"].supports("mcp")


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


def test_no_scopes_at_all_means_unscoped_not_empty():
    """``None`` and ``[]`` must not mean the same thing.

    An API key's permissions live in the provider's console and Bloom cannot read
    them, so filtering would hide capability the key may well have. An empty list
    still means what it always did.
    """
    provider = registry_module.providers()["spotify"]
    unscoped = {op.name for op in registry_module.operations_for(provider, None)}
    assert unscoped == {op.name for op in provider.operations}
    assert "play" in unscoped
    assert "play" not in {op.name for op in registry_module.operations_for(provider, [])}


# --- how a provider says it can be authenticated ------------------------------


_API_KEY_HEAD = """
name = "demo"
api_base = "https://api"
auth = "api_key"
[api_key]
in = "header"
header = "Authorization"
prefix = "Bearer "
"""


def test_an_api_key_provider_needs_no_authorize_endpoint(tmp_path):
    """There is no flow to run, so demanding its URLs would mean inventing values."""
    provider = registry_module.load_manifest(
        _write(
            tmp_path,
            _API_KEY_HEAD
            + """
[[operations]]
name = "go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
""",
        )
    )
    assert provider.auth_methods == ("api_key",)
    assert provider.authorize_url == ""
    assert provider.api_key is not None
    assert provider.api_key.location == "header"


def test_a_manifest_that_says_nothing_about_auth_is_still_oauth(tmp_path):
    """Every file written before `auth` existed keeps exactly the guarantees it had."""
    provider = registry_module.load_manifest(
        _write(
            tmp_path,
            _HEAD
            + """
[[operations]]
name = "go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
""",
        )
    )
    assert provider.auth_methods == ("oauth",)
    assert provider.api_key is None

    without_urls = _write(tmp_path, 'name = "demo"\napi_base = "https://api"\n')
    with pytest.raises(registry_module.ManifestError, match="authorize_url"):
        registry_module.load_manifest(without_urls)


def test_api_key_auth_without_the_table_that_says_where_the_key_goes_is_refused(tmp_path):
    path = _write(tmp_path, 'name = "demo"\napi_base = "https://api"\nauth = "api_key"\n')
    with pytest.raises(registry_module.ManifestError, match=r"\[api_key\] table is required"):
        registry_module.load_manifest(path)


def test_an_unknown_auth_method_is_refused(tmp_path):
    path = _write(tmp_path, 'name = "demo"\napi_base = "https://api"\nauth = "magic"\n')
    with pytest.raises(registry_module.ManifestError, match="unknown auth method"):
        registry_module.load_manifest(path)


def test_a_key_in_the_query_may_not_share_a_name_with_a_declared_parameter(tmp_path):
    """Otherwise the credential is merged over the model's argument.

    The schema would advertise a parameter, the model would author it, and it would
    silently never be sent — one operation, at run time, as a wrong answer. Cheaper
    to refuse the manifest.
    """
    path = _write(
        tmp_path,
        """
name = "demo"
api_base = "https://api"
auth = "api_key"
[api_key]
in = "query"
query_param = "key"

[[operations]]
name = "go"
method = "GET"
path = "/go"
description = "d"
[operations.params]
key = { in = "query", type = "string" }
""",
    )
    with pytest.raises(registry_module.ManifestError, match="collides"):
        registry_module.load_manifest(path)


def test_a_probe_must_be_read_only(tmp_path):
    """It fires on a button press with no confirmation step."""
    path = _write(
        tmp_path,
        _HEAD
        + """
[probe]
method = "DELETE"
path = "/nope"
""",
    )
    with pytest.raises(registry_module.ManifestError, match="must be GET or HEAD"):
        registry_module.load_manifest(path)


def test_a_key_is_presented_where_the_manifest_says(tmp_path):
    header = registry_module.ApiKeySpec(location="header", header="X-Api-Key", prefix="Token ")
    assert header.apply("abc") == ({"X-Api-Key": "Token abc"}, {})

    query = registry_module.ApiKeySpec(location="query", query_param="apikey")
    assert query.apply("abc") == ({}, {"apikey": "abc"})


# --- which app registration a connection authorises against -------------------


def test_a_connections_own_client_credentials_win_over_the_deployments(monkeypatch):
    monkeypatch.setenv("BLOOM_OAUTH_GITHUB_CLIENT_ID", "box-id")
    monkeypatch.setenv("BLOOM_OAUTH_GITHUB_CLIENT_SECRET", "box-secret")
    provider = registry_module.providers()["github"]

    own = registry_module.client_for(provider, client_id="own-id", client_secret="own-secret")
    assert (own.client_id, own.client_secret, own.source) == ("own-id", "own-secret", "connection")

    fallback = registry_module.client_for(provider)
    assert (fallback.client_id, fallback.source) == ("box-id", "environment")


def test_a_half_supplied_client_credential_falls_back_rather_than_mixing(monkeypatch):
    """Mixing one source's id with another's secret is an `invalid_client` no log explains."""
    monkeypatch.setenv("BLOOM_OAUTH_GITHUB_CLIENT_ID", "box-id")
    monkeypatch.setenv("BLOOM_OAUTH_GITHUB_CLIENT_SECRET", "box-secret")
    provider = registry_module.providers()["github"]

    resolved = registry_module.client_for(provider, client_id="own-id")
    assert (resolved.client_id, resolved.client_secret) == ("box-id", "box-secret")


def test_no_client_credentials_anywhere_is_falsey(monkeypatch):
    monkeypatch.delenv("BLOOM_OAUTH_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLOOM_OAUTH_GITHUB_CLIENT_SECRET", raising=False)
    provider = registry_module.providers()["github"]

    assert not registry_module.client_for(provider)
    assert not provider.has_deployment_default


# --- the synthesised tools ----------------------------------------------------


class FakeResolver:
    """Hands out pre-baked credentials, counting forced refreshes.

    ``kind`` decides what a tool gets back and whether a 401 is worth retrying —
    the one thing the caller still branches on.
    """

    def __init__(self, tokens: list[str], kind: str = "oauth") -> None:
        self.tokens = list(tokens)
        self.kind = kind
        self.forced = 0
        self.reauth: list[str] = []

    async def credential(self, connection_id, *, force_refresh=False):
        if force_refresh:
            self.forced += 1
        token = self.tokens.pop(0) if self.tokens else ""
        if not token:
            return credentials_module.NO_CREDENTIAL
        if self.kind == "api_key":
            return credentials_module.Credential({"X-Api-Key": token}, {}, refreshable=False)
        return credentials_module.Credential(
            {"Authorization": f"Bearer {token}"}, {}, refreshable=True
        )

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
    connection = store.create_connection(
        kind="oauth",
        provider="spotify",
        name="spotify",
        secret=encrypt("access-0", settings),
        refresh_token=encrypt(refresh, settings) if refresh else None,
        expires_at=expires.isoformat(),
        scopes=["user-modify-playback-state"],
        status="active",
        attach_to=[config["id"]],
    )
    return store, config, connection


def test_a_valid_token_is_returned_without_touching_the_network(tmp_path):
    settings = _settings()
    store, _, connection = _connected_store(tmp_path, settings, expires_in_s=3600)
    client = FakeClient([])
    resolver = credentials_module.CredentialResolver(
        store, settings, http_client_factory=client.factory
    )

    assert asyncio.run(resolver.secret(connection["id"])) == "access-0"
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
        assert asyncio.run(resolver.secret(connection["id"])) == "access-1"
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
    assert decrypt(secrets["secret"], settings) == "access-1"
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
        asyncio.run(resolver.secret(connection["id"]))
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

    assert asyncio.run(resolver.secret(connection["id"])) == ""
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
        assert asyncio.run(resolver.secret(connection["id"])) == ""
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
                resolver.secret(connection["id"]),
                resolver.secret(connection["id"]),
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


def _oauth_connection(client, agent=None, **over) -> dict:
    body = {"kind": "oauth", "provider": "spotify", **over}
    if agent is not None:
        body["attach_to"] = [agent["id"]]
    response = client.post("/admin/connections", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_a_new_oauth_connection_is_pending_until_the_browser_comes_back(client):
    """`pending` has been in the status enum since day one and was never written."""
    connection = _oauth_connection(client)
    assert connection["status"] == "pending"
    assert connection["has_secret"] is False
    # It is in the library immediately, attached to nothing.
    assert connection["agent_ids"] == []


def test_start_returns_an_authorize_url_carrying_state_and_a_pkce_challenge(client):
    connection = _oauth_connection(client, _agent(client))
    response = client.post(
        f"/admin/connections/{connection['id']}/oauth/start", headers=AUTH, json={}
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
    connection = _oauth_connection(client, agent)
    state = client.post(
        f"/admin/connections/{connection['id']}/oauth/start", headers=AUTH, json={}
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

    monkeypatch.setattr("app.admin.oauth_callback.exchange", patched)

    # No AUTH header on purpose.
    response = client.get(f"/admin/oauth/spotify/callback?code=abc&state={state}")
    assert response.status_code == 200
    assert "Spotify connected" in response.text
    # The page fires the deep link *and* tells the user what to do, because the
    # protocol handler does not exist yet.
    assert "aperture://oauth-complete?provider=spotify&status=success" in response.text
    assert "close this tab" in response.text

    status = client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).json()
    assert status[0]["provider"] == "spotify"
    assert status[0]["status"] == "active"
    assert status[0]["scopes"] == ["user-modify-playback-state"]
    # A boolean, never a value.
    assert status[0]["has_secret"] is True
    assert "secret" not in status[0]
    assert "refresh_token" not in status[0]


def test_one_authorised_account_serves_a_second_agent_without_reconnecting(client, monkeypatch):
    """The point of the library: approve once, attach anywhere."""
    first, second = _agent(client), None
    second = client.post("/admin/agents", headers=AUTH, json={"slug": "party"}).json()
    connection = _oauth_connection(client, first)

    # Authorise it the once.
    store = db_module.get_store()
    store.set_connection_secret(
        connection["id"], secret=encrypt("at", get_settings()), status="active"
    )

    attached = client.post(
        f"/admin/agents/{second['id']}/connections",
        headers=AUTH,
        json={"connection_id": connection["id"]},
    )
    assert attached.status_code == 201, attached.text
    assert [c["id"] for c in attached.json()] == [connection["id"]]
    assert attached.json()[0]["status"] == "active"


def test_a_state_can_only_be_used_once(client):
    connection = _oauth_connection(client, _agent(client))
    state = client.post(
        f"/admin/connections/{connection['id']}/oauth/start", headers=AUTH, json={}
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


def test_revoking_zeroes_the_users_credential_but_keeps_the_row(client):
    agent = _agent(client)
    connection = _oauth_connection(client, agent, client_id="cid", client_secret="csec")
    store = db_module.get_store()
    settings = get_settings()
    store.set_connection_secret(
        connection["id"],
        secret=encrypt("at", settings),
        refresh_token=encrypt("rt", settings),
        status="active",
    )

    revoked = client.post(f"/admin/connections/{connection['id']}/revoke", headers=AUTH)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"

    secrets = store.connection_secrets(connection["id"])
    assert not secrets["secret"]
    assert not secrets["refresh_token"]
    # The app registration is still yours: clearing it would mean re-entering it to
    # reconnect something you only disconnected.
    assert secrets["client_secret"]

    # And the row survives, so the agent's page still shows "Spotify — disconnected".
    still_there = client.get(f"/admin/agents/{agent['id']}/connections", headers=AUTH).json()
    assert [c["status"] for c in still_there] == ["revoked"]


def test_the_kinds_endpoint_reports_what_this_deployment_can_create(client):
    body = client.get("/admin/connections/kinds", headers=AUTH).json()

    assert {k["kind"] for k in body["kinds"]} == {"oauth", "api_key", "mcp"}
    assert all(k["available"] for k in body["kinds"])

    spotify = next(p for p in body["providers"] if p["name"] == "spotify")
    assert spotify["auth"] == ["oauth"]
    assert spotify["has_deployment_default"] is True
    assert spotify["client_id_env"] == "BLOOM_OAUTH_SPOTIFY_CLIENT_ID"
    assert "spotify_play" in spotify["operations"]

    github = next(p for p in body["providers"] if p["name"] == "github")
    assert github["auth"] == ["oauth", "api_key"]
    assert github["api_key"]["label"] == "Personal access token"


def test_a_deployment_without_credentials_is_a_hint_not_a_gate(client, monkeypatch):
    """It used to grey out the Connect button. A connection may carry its own now."""
    monkeypatch.delenv("BLOOM_OAUTH_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLOOM_OAUTH_SPOTIFY_CLIENT_SECRET", raising=False)

    body = client.get("/admin/connections/kinds", headers=AUTH).json()
    spotify = next(p for p in body["providers"] if p["name"] == "spotify")
    assert spotify["has_deployment_default"] is False

    # With neither its own nor the box's: a 422 naming the field, at the moment the
    # form is submitted, rather than a 503 when the browser was meant to open.
    connection = _oauth_connection(client)
    refused = client.post(
        f"/admin/connections/{connection['id']}/oauth/start", headers=AUTH, json={}
    )
    assert refused.status_code == 422, refused.text
    assert "client credentials" in refused.json()["message"]

    # With its own, the same flow starts fine and authorises against them.
    own = _oauth_connection(client, name="spotify2", client_id="own-id", client_secret="own-secret")
    started = client.post(f"/admin/connections/{own['id']}/oauth/start", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    assert parse_qs(urlparse(started.json()["authorize_url"]).query)["client_id"] == ["own-id"]


def test_storing_a_secret_is_refused_when_no_encryption_key_is_configured(client, monkeypatch):
    """Storing a credential without a key is a breach, not a degraded mode."""
    connection = _oauth_connection(client)
    monkeypatch.setenv("BLOOM_FERNET_KEYS", "")
    get_settings.cache_clear()

    refused = client.post(
        f"/admin/connections/{connection['id']}/oauth/start", headers=AUTH, json={}
    )
    assert refused.status_code == 503
    assert refused.json()["error"] == "unavailable"


def test_a_connection_for_an_unknown_provider_is_a_404(client):
    response = client.post(
        "/admin/connections", headers=AUTH, json={"kind": "oauth", "provider": "nope"}
    )
    assert response.status_code == 404


def test_a_provider_that_does_not_support_the_kind_says_what_it_does_support(client):
    response = client.post(
        "/admin/connections", headers=AUTH, json={"kind": "api_key", "provider": "spotify"}
    )
    assert response.status_code == 422, response.text
    assert "oauth" in response.json()["message"]


# --- the refresh sweep --------------------------------------------------------


def test_the_sweep_refreshes_only_what_is_close_to_expiry(tmp_path):
    from app.oauth.refresh import refresh_due

    settings = _settings(oauth_refresh_skew_s=600)
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="dj")

    def _connect(name, seconds, **over):
        expires = (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0)
        return store.create_connection(
            kind="oauth",
            provider="spotify",
            name=name,
            secret=encrypt("a", settings),
            refresh_token=encrypt("r", settings),
            expires_at=expires.isoformat(),
            scopes=[],
            status="active",
            attach_to=[config["id"]],
            **over,
        )

    soon = _connect("soon", 60)
    later = _connect("later", 86400)

    class OneShotResolver:
        def __init__(self):
            self.seen = []

        async def secret(self, connection_id, *, force_refresh=False):
            self.seen.append(connection_id)
            return "new"

    resolver = OneShotResolver()
    assert asyncio.run(refresh_due(store, settings, resolver=resolver)) == 1
    assert resolver.seen == [soon["id"]]
    assert later["id"] not in resolver.seen
    store.close()


def test_the_sweep_leaves_api_keys_alone(tmp_path):
    """A static key has no expiry and nothing to refresh with.

    Sweeping one would mark a perfectly good credential `needs_reauth` for failing
    to do something it cannot do.
    """
    from app.oauth.refresh import refresh_due

    settings = _settings(oauth_refresh_skew_s=600)
    store = db_module.Store(str(tmp_path / "bloom.db"))
    expires = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
    store.create_connection(
        kind="api_key",
        provider="github",
        name="github",
        secret=encrypt("ghp_x", settings),
        expires_at=expires.isoformat(),
        status="active",
    )

    class Boom:
        async def secret(self, connection_id, *, force_refresh=False):
            raise AssertionError("an api_key connection must never be swept")

    assert asyncio.run(refresh_due(store, settings, resolver=Boom())) == 0
    store.close()
