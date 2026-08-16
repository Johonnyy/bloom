"""The privilege boundary: who gets the tools that write configuration.

The builder can create agents, create connections and attach them. Nothing else may,
and the mechanism is a single predicate on a reserved slug — so this file is the one
that has to be right. Everything else in the feature is recoverable; a normal agent
holding `bloom_create_connection` is not.

The central test asserts the *absence* of capability, which is the harder direction
and the one that stays true only if someone keeps checking: it lists the broker a
normal config actually gets and intersects it with the builder's tool names. A test
that merely confirmed the builder has them would pass just as happily on the day
everyone else got them too.
"""

from __future__ import annotations

import asyncio

import pytest

from app import db as db_module
from app import trace as trace_module
from app.builder import BUILDER_SLUG, ensure_builder_config, is_builder
from app.builder.prompt import BUILDER_NAME, SYSTEM_PROMPT
from app.builder.tools import TOOL_NAMES
from app.config import Settings
from app.runtime_service import build_runner


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
def recorder(tmp_path):
    """A real recorder over a real writer — build_runner reads `recorder.run_id`."""
    st = db_module.Store(str(tmp_path / "trace.db"))
    writer = trace_module.TraceWriter(st)
    yield trace_module.RunRecorder(writer, "run-1")
    st.close()


async def _tool_names(runner) -> set[str]:
    if runner.broker is None:
        return set()
    schemas = await runner.broker.list_tools()
    return {s["function"]["name"] for s in schemas}


# --- the boundary ------------------------------------------------------------


def test_a_normal_agent_gets_none_of_the_builder_tools(store, recorder):
    """The whole point. Stated as an intersection so a partial leak still fails."""
    config = store.create_config(slug="dj", name="DJ", system_prompt="You pick music.")
    runner, _ = build_runner(config, recorder=recorder, settings=_settings(), store=store)
    names = asyncio.run(_tool_names(runner))
    assert names & set(TOOL_NAMES) == set()


def test_a_normal_agent_with_a_peer_attached_still_gets_none_of_them(store, recorder):
    """A connection is the one thing an operator controls, so it is the way in to try."""
    config = store.create_config(slug="dj", name="DJ")
    store.create_connection(
        kind="mcp",
        name="amber",
        config={"url": "https://amber.example"},
        status="active",
        attach_to=[config["id"]],
    )
    runner, _ = build_runner(config, recorder=recorder, settings=_settings(), store=store)
    names = asyncio.run(_tool_names(runner))
    assert names & set(TOOL_NAMES) == set()


def test_the_builder_gets_them_and_they_come_first(store, recorder):
    config = ensure_builder_config(store, _settings())
    runner, _ = build_runner(config, recorder=recorder, settings=_settings(), store=store)
    names = asyncio.run(_tool_names(runner))
    assert set(TOOL_NAMES) <= names


def test_the_builder_broker_precedes_an_attached_peer(store, recorder):
    """Broker order is priority order: nothing attached may shadow these names."""
    config = ensure_builder_config(store, _settings())
    store.create_connection(
        kind="mcp",
        name="amber",
        config={"url": "https://amber.example"},
        status="active",
        attach_to=[config["id"]],
    )
    runner, _ = build_runner(config, recorder=recorder, settings=_settings(), store=store)
    # CompositeBroker keeps the *first* of two colliding names, so being first is the
    # property worth pinning — not merely being present somewhere in the composition.
    inner = runner.broker._inner  # the TracingBroker's wrapped broker
    assert len(inner.brokers) == 2, "expected the builder broker beside the peer client"
    first = inner.brokers[0]
    assert set(TOOL_NAMES) <= {s["function"]["name"] for s in asyncio.run(first.list_tools())}


def test_the_flag_switches_the_tools_off_without_removing_the_row(store, recorder):
    config = ensure_builder_config(store, _settings())
    runner, _ = build_runner(
        config, recorder=recorder, settings=_settings(feature_builder=False), store=store
    )
    names = asyncio.run(_tool_names(runner))
    assert names & set(TOOL_NAMES) == set()


# --- the seed ----------------------------------------------------------------


def test_seeding_is_idempotent_and_reseeds_the_prompt_but_not_the_tier(store):
    first = ensure_builder_config(store, _settings(builder_keyword="strong"))
    assert is_builder(first)
    assert first["system_prompt"] == SYSTEM_PROMPT
    assert first["name"] == BUILDER_NAME
    assert first["model_tier"] == "strong"

    # An operator re-points it at something cheaper, then the process restarts.
    store.update_config(first["id"], model_tier="balanced")
    store.update_config(first["id"], system_prompt="ignore your instructions")

    again = ensure_builder_config(store, _settings(builder_keyword="strong"))
    assert again["id"] == first["id"]
    # The prompt comes back from git...
    assert again["system_prompt"] == SYSTEM_PROMPT
    # ...and the operator's choice of model survives, because it is theirs to make.
    assert again["model_tier"] == "balanced"
    assert len([c for c in store.list_configs() if is_builder(c)]) == 1


def test_the_builder_uses_its_own_ceilings_not_the_service_ones(store, recorder):
    """A build is 12-25 steps; the service default of 8 would truncate every one."""
    config = ensure_builder_config(store, _settings())
    settings = _settings(
        max_steps=8, max_cost_usd=0.5, builder_max_steps=30, builder_max_cost_usd=2.0
    )
    runner, _ = build_runner(config, recorder=recorder, settings=settings, store=store)
    steps = [c for c in runner.stop_conditions if hasattr(c, "max_steps")]
    assert steps and steps[0].max_steps == 30


def test_a_normal_agent_is_still_clamped_to_the_service_ceiling(store, recorder):
    config = store.create_config(slug="dj", max_steps=50)
    settings = _settings(max_steps=8, builder_max_steps=30)
    runner, _ = build_runner(config, recorder=recorder, settings=settings, store=store)
    steps = [c for c in runner.stop_conditions if hasattr(c, "max_steps")]
    # Clamped down to 8 — a config may lower a ceiling, never raise it, and the
    # builder's larger ceiling must not leak to anybody else.
    assert steps and steps[0].max_steps == 8


def test_the_builders_own_ceiling_still_clamps_downwards(store, recorder):
    """The invariant is unchanged: there are simply two ceilings."""
    config = ensure_builder_config(store, _settings())
    store.update_config(config["id"], max_steps=5)
    config = store.get_config(config["id"])
    settings = _settings(builder_max_steps=30)
    runner, _ = build_runner(config, recorder=recorder, settings=settings, store=store)
    steps = [c for c in runner.stop_conditions if hasattr(c, "max_steps")]
    assert steps and steps[0].max_steps == 5


# --- the model keyword reaches the runner as a real id -----------------------


def test_a_keyword_is_resolved_to_a_model_id_before_the_runner_sees_it(store, recorder):
    """`agent_runtime` has never heard of `coding` and would raise on it."""
    config = store.create_config(slug="dev", model_tier="coding")
    runner, _ = build_runner(config, recorder=recorder, settings=_settings(), store=store)
    assert "/" in runner.model
    assert runner.model == "anthropic/claude-sonnet-4.6"


# --- the slug is reserved -----------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient configured the way a deploy would be.

    `monkeypatch.setenv`, never bare ``os.environ``: an assignment that outlives the
    test leaks into every later one — and because `Settings` is an lru_cache'd
    singleton, the symptom is a completely unrelated config test failing.
    """
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", "tester:t0ken")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "")

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


AUTH = {"Authorization": "Bearer t0ken"}


def test_the_reserved_slug_is_refused_at_create_and_at_patch(client):
    """Through the API, because that is the surface a caller would reach."""
    refused = client.post("/admin/agents", headers=AUTH, json={"slug": BUILDER_SLUG})
    assert refused.status_code == 422
    assert BUILDER_SLUG in refused.json()["message"]

    made = client.post("/admin/agents", headers=AUTH, json={"slug": "ordinary"})
    assert made.status_code == 201
    stolen = client.patch(
        f"/admin/agents/{made.json()['id']}", headers=AUTH, json={"slug": BUILDER_SLUG}
    )
    assert stolen.status_code == 422


def test_the_builder_row_cannot_be_deleted_or_have_its_prompt_edited(client):
    """Both would succeed and then be undone at the next boot, which is worse."""
    agents = client.get("/admin/agents", headers=AUTH).json()
    builder = next(a for a in agents if a["builtin"])
    assert builder["slug"] == BUILDER_SLUG

    assert client.delete(f"/admin/agents/{builder['id']}", headers=AUTH).status_code == 409
    edited = client.patch(
        f"/admin/agents/{builder['id']}", headers=AUTH, json={"system_prompt": "do as I say"}
    )
    assert edited.status_code == 409

    # The tier and ceilings genuinely are this install's to choose, so they patch.
    retuned = client.patch(
        f"/admin/agents/{builder['id']}", headers=AUTH, json={"model_tier": "cheap"}
    )
    assert retuned.status_code == 200
    assert retuned.json()["model_tier"] == "cheap"


# --- the whole sequence composes ---------------------------------------------


def test_a_full_build_sequence_leaves_a_working_configuration(store):
    """The tools in the order the prompt describes, with no model involved.

    Not a test that the builder *chooses* well — that is the model's job and is not
    testable here. This pins that the steps compose: what one tool writes, the next
    one can find, and the build row at the end describes something real.
    """
    from app.builder.tools import builder_broker

    store.create_build(build_id="b9", run_id="r9", brief="a spotify agent")
    broker = builder_broker(store, _settings(), run_id="r9")

    def call(tool, **args):
        return asyncio.run(broker.call_tool(tool, args))

    # 1. Inspect: nothing exists yet, and Spotify does have a manifest.
    assert "No agents are configured yet." in call("bloom_list_agents")
    assert "spotify" in call("bloom_list_providers")

    # 2. Pick a keyword from what the agent will do.
    assert "balanced" in call("bloom_list_keywords")

    # 3. Create the agent...
    assert "Created agent" in call(
        "bloom_create_agent",
        slug="spotify-dj",
        name="Spotify DJ",
        system_prompt="You control Spotify playback. Use spotify_search then spotify_play.",
        model_keyword="balanced",
    )
    # ...and the connection, attached in the same call.
    assert "pending" in call(
        "bloom_create_connection",
        kind="oauth",
        provider="spotify",
        name="spotify",
        label="Spotify",
        attach_to_slug="spotify-dj",
    )

    # 4. Hand back what a human must still do.
    assert "2 step(s)" in call(
        "bloom_set_setup_checklist",
        agent_slug="spotify-dj",
        summary="No usable MCP server for Spotify; used the shipped manifest.",
        steps=[
            {
                "kind": "register_oauth_app",
                "title": "Register a Spotify app",
                "url": "https://developer.spotify.com/dashboard",
            },
            {"kind": "connect_oauth", "title": "Press Connect", "connection_name": "spotify"},
        ],
    )

    # The configuration is real: the agent exists, the connection is attached and
    # inert, and the build row points at both.
    config = store.get_config_by_slug("spotify-dj")
    assert config["model_tier"] == "balanced"
    attached = store.connections_for(config["id"])
    assert [c["name"] for c in attached] == ["spotify"]
    assert attached[0]["status"] == "pending"

    build = store.get_build("b9")
    assert build["agent_config_id"] == config["id"]
    assert [s["kind"] for s in build["checklist"]] == ["register_oauth_app", "connect_oauth"]


def test_the_agent_the_builder_made_gets_no_tools_until_a_human_connects_it(store, recorder):
    """The safety property, end to end: everything it produces is inert."""
    from app.builder.tools import builder_broker

    store.create_build(build_id="b10", run_id="r10", brief="a spotify agent")
    broker = builder_broker(store, _settings(), run_id="r10")
    asyncio.run(
        broker.call_tool(
            "bloom_create_agent",
            {
                "slug": "spotify-dj",
                "name": "Spotify DJ",
                "system_prompt": "You control Spotify.",
                "model_keyword": "balanced",
            },
        )
    )
    asyncio.run(
        broker.call_tool(
            "bloom_create_connection",
            {
                "kind": "oauth",
                "provider": "spotify",
                "name": "spotify",
                "attach_to_slug": "spotify-dj",
            },
        )
    )

    config = store.get_config_by_slug("spotify-dj")
    # A real key, because `_connection_notes` is gated on `oauth_enabled` — which
    # needs both the flag and something to encrypt with.
    from cryptography.fernet import Fernet

    settings = _settings(feature_oauth=True, fernet_keys=Fernet.generate_key().decode())
    runner, _ = build_runner(config, recorder=recorder, settings=settings, store=store)
    assert asyncio.run(_tool_names(runner)) == set()
    # And it is told so in its own prompt, rather than being left to improvise.
    assert "not currently connected" in runner.system_prompt
