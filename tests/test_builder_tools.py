"""What the builder may write, and the shapes it cannot express.

The load-bearing test here is the schema one. "The builder never holds a credential"
is enforced by there being nowhere to put one — no authoring tool has a parameter a
secret could go in — and the thing that would quietly break it is somebody later
adding a helpful ``secret=`` argument. So the assertion is made against the
*registered JSON schema*, not against behaviour: it fails the moment the shape
changes, which is before anything has run.

Everything else here is the ordinary contract: a tool returns prose, never raises,
and turns its own failures into something the model can act on within the same run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import db as db_module
from app.builder.tools import builder_broker
from app.config import Settings
from app.providers.registry import FORBIDDEN_PARAMS


def _settings(**over) -> Settings:
    base = {
        "_env_file": None,
        "db_path": ":memory:",
        "openrouter_api_key": "sk-test",
        "search_api_key": "tvly-test",
    }
    return Settings(**{**base, **over})


@pytest.fixture
def store(tmp_path):
    s = db_module.Store(str(tmp_path / "bloom.db"))
    yield s
    s.close()


@pytest.fixture
def broker(store):
    build = store.create_build(build_id="b1", run_id="r1", brief="a spotify agent")
    assert build["status"] == "running"
    return builder_broker(store, _settings(), run_id="r1")


def call(_broker, _tool, **args) -> str:
    """Invoke a tool by name.

    The leading underscores are not style — several of these tools take an argument
    literally called ``name``, and a helper with a plain ``name`` parameter collides
    with it.
    """
    return asyncio.run(_broker.call_tool(_tool, args))


# --- the structural guarantee -------------------------------------------------


def test_no_builder_tool_accepts_anything_shaped_like_a_credential(broker):
    """A tool argument is model-authored data that reaches the trace and the logs.

    `FORBIDDEN_PARAMS` is the denylist a provider manifest is validated against for
    exactly this reason; the builder's own tools are held to it too, plus the words
    a well-meaning future edit would actually reach for.
    """
    banned = set(FORBIDDEN_PARAMS) | {
        "secret",
        "client_secret",
        "password",
        "credential",
        "bearer",
        "refresh_token",
    }
    for schema in asyncio.run(broker.list_tools()):
        properties = set(schema["function"]["parameters"].get("properties", {}))
        assert properties & banned == set(), schema["function"]["name"]


def test_the_authoring_tools_are_not_marked_read_only(broker):
    """A caller decides whether it may retry from this flag alone."""
    flags = {
        s["function"]["name"]: s["x_agent"]["read_only"] for s in asyncio.run(broker.list_tools())
    }
    assert flags["bloom_create_agent"] is False
    assert flags["bloom_create_connection"] is False
    assert flags["web_search"] is True
    assert flags["bloom_list_providers"] is True


def test_nothing_requires_confirmation_because_nothing_could_satisfy_it(broker):
    """See app/mcp.py: no caller in this ecosystem can send X-Confirmed yet."""
    for schema in asyncio.run(broker.list_tools()):
        assert schema["x_agent"]["requires_confirmation"] is False


# --- creating an agent --------------------------------------------------------


def test_creating_an_agent_records_it_on_the_build_row_as_it_happens(broker, store):
    """So a run that dies before the checklist still leaves a build naming what it made."""
    out = call(
        broker,
        "bloom_create_agent",
        slug="spotify-dj",
        name="Spotify DJ",
        system_prompt="You pick music.",
        model_keyword="balanced",
    )
    assert "Created agent" in out
    build = store.get_build("b1")
    assert build["agent_slug"] == "spotify-dj"
    assert build["agent_config_id"] == store.get_config_by_slug("spotify-dj")["id"]


def test_the_reserved_slug_is_refused_as_prose_so_the_model_can_retry(broker):
    out = call(
        broker,
        "bloom_create_agent",
        slug="bloom-builder",
        name="x",
        system_prompt="y",
        model_keyword="balanced",
    )
    assert "reserved" in out
    assert "Created" not in out


def test_an_unknown_keyword_is_refused_and_points_at_the_list(broker):
    out = call(
        broker,
        "bloom_create_agent",
        slug="x",
        name="x",
        system_prompt="y",
        model_keyword="galaxy-brain",
    )
    assert "bloom_list_keywords" in out


def test_a_duplicate_slug_is_reported_rather_than_raised(broker, store):
    store.create_config(slug="taken")
    out = call(
        broker,
        "bloom_create_agent",
        slug="taken",
        name="x",
        system_prompt="y",
        model_keyword="cheap",
    )
    assert "already exists" in out


def test_an_agent_with_no_prompt_is_refused(broker):
    out = call(
        broker, "bloom_create_agent", slug="x", name="x", system_prompt="  ", model_keyword="cheap"
    )
    assert "needs a system prompt" in out


# --- creating a connection ----------------------------------------------------


def test_every_connection_the_builder_makes_starts_pending(broker, store):
    """Including a peer, whose 'tokenless means active' default is wrong here."""
    call(
        broker,
        "bloom_create_connection",
        kind="mcp",
        name="notion",
        url="https://mcp.notion.com/mcp",
        endpoint=True,
    )
    row = store.get_connection_by_name("notion")
    assert row["status"] == "pending"
    # And the endpoint flag is recorded, which is what stops MCPClient appending
    # /mcp/ to a URL that already ends in it.
    assert row["config"] == {"url": "https://mcp.notion.com/mcp", "endpoint": True}


def test_a_peer_url_must_be_public_https(broker, store):
    for url in ("http://mcp.example.com/mcp", "https://127.0.0.1/mcp", "https://localhost/mcp"):
        out = call(broker, "bloom_create_connection", kind="mcp", name="p", url=url)
        assert "Refusing that URL" in out, url
    assert store.get_connection_by_name("p") is None


def test_a_provider_with_no_manifest_is_refused_with_what_is_available(broker, store):
    out = call(broker, "bloom_create_connection", kind="api_key", provider="notion", name="notion")
    assert "no manifest" in out.lower()
    # Naming what *is* available is what lets the model choose the other branch.
    assert "spotify" in out
    assert store.get_connection_by_name("notion") is None


def test_a_shipped_provider_connects_and_inherits_its_default_scopes(broker, store):
    out = call(
        broker,
        "bloom_create_connection",
        kind="oauth",
        provider="spotify",
        name="spotify",
        label="Spotify",
    )
    assert "pending" in out
    row = store.get_connection_by_name("spotify")
    assert row["provider"] == "spotify"
    assert "user-modify-playback-state" in row["scopes"]


def test_a_provider_that_does_not_support_the_kind_is_refused(broker):
    """Spotify has no static-key mode; offering one would fail at the first call."""
    out = call(
        broker, "bloom_create_connection", kind="api_key", provider="spotify", name="spotify"
    )
    assert "cannot be connected with a api_key credential" in out


def test_creating_a_connection_can_attach_it_in_the_same_call(broker, store):
    call(
        broker,
        "bloom_create_agent",
        slug="dj",
        name="DJ",
        system_prompt="You pick music.",
        model_keyword="balanced",
    )
    call(
        broker,
        "bloom_create_connection",
        kind="oauth",
        provider="spotify",
        name="spotify",
        attach_to_slug="dj",
    )
    config = store.get_config_by_slug("dj")
    assert [c["name"] for c in store.connections_for(config["id"])] == ["spotify"]


def test_a_bad_connection_name_is_refused_before_anything_is_written(broker, store):
    out = call(
        broker, "bloom_create_connection", kind="mcp", name="Has__Bad", url="https://x.com/mcp"
    )
    assert "__" in out
    assert store.list_connections() == []


# --- attaching ----------------------------------------------------------------


def test_a_second_connection_for_one_provider_is_refused_on_the_same_agent(broker, store):
    config = store.create_config(slug="dj")
    store.create_connection(
        kind="oauth", provider="spotify", name="spotify", attach_to=[config["id"]]
    )
    store.create_connection(kind="oauth", provider="spotify", name="spotify-alt")
    out = call(broker, "bloom_attach_connection", agent_slug="dj", connection_name="spotify-alt")
    assert "already has a spotify connection" in out


# --- the checklist ------------------------------------------------------------


def test_the_checklist_is_stored_and_an_unknown_kind_becomes_manual(broker, store):
    out = call(
        broker,
        "bloom_set_setup_checklist",
        agent_slug="spotify-dj",
        summary="Used the shipped Spotify manifest.",
        steps=[
            {
                "kind": "register_oauth_app",
                "title": "Register a Spotify app",
                "url": "https://developer.spotify.com/dashboard",
            },
            {"kind": "invent_a_kind", "title": "Do a thing"},
            {"kind": "manual", "detail": "no title, so dropped"},
        ],
    )
    assert "2 step(s)" in out
    build = store.get_build("b1")
    assert [s["kind"] for s in build["checklist"]] == ["register_oauth_app", "manual"]
    assert build["summary"] == "Used the shipped Spotify manifest."


def test_an_empty_checklist_is_refused_so_a_build_cannot_finish_silently(broker):
    out = call(broker, "bloom_set_setup_checklist", agent_slug="x", summary="s", steps=[])
    assert "at least one step" in out


def test_a_step_url_that_is_not_http_is_dropped_rather_than_rendered(broker, store):
    call(
        broker,
        "bloom_set_setup_checklist",
        agent_slug="x",
        summary="s",
        steps=[{"kind": "manual", "title": "Click", "url": "javascript:alert(1)"}],
    )
    assert store.get_build("b1")["checklist"][0]["url"] == ""


# --- inspection ---------------------------------------------------------------


def test_listing_providers_names_the_tools_each_would_contribute(broker):
    out = call(broker, "bloom_list_providers")
    assert "spotify" in out and "spotify_play" in out


def test_listing_keywords_gives_the_model_what_each_is_for(broker):
    out = call(broker, "bloom_list_keywords")
    assert "coding" in out and "Writing, reading and fixing code" in out


def test_listing_agents_hides_the_builder_from_itself(broker, store):
    from app.builder import ensure_builder_config

    ensure_builder_config(store, _settings())
    store.create_config(slug="dj", system_prompt="You pick music.")
    out = call(broker, "bloom_list_agents")
    assert "dj" in out
    assert "bloom-builder" not in out


def test_a_tool_that_fails_returns_a_string_rather_than_raising(broker):
    """The LocalToolBroker contract: a broken tool must not take down the turn."""
    out = call(broker, "bloom_attach_connection", agent_slug="ghost", connection_name="nope")
    assert isinstance(out, str)
    assert "No agent named" in out


def test_the_registered_schemas_are_valid_json_schema_objects(broker):
    """They are sent to a provider verbatim; a malformed one is a 400 at run time."""
    for schema in asyncio.run(broker.list_tools()):
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert isinstance(params.get("properties", {}), dict)
        json.dumps(params)  # must round-trip
