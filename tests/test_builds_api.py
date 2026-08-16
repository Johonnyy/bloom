"""The build surface: starting one, watching it, and ticking its steps off.

A build is a *run*, which is the design decision everything here depends on: the row
is committed before the model is called, the stream URL points at the existing SSE
endpoint, and Aperture reuses its trace renderer unchanged. So these tests care about
the seam — that an id and a stream URL come back immediately, and that the checklist
survives the round trip — rather than about the model, which is never called.

`start_build` is faked at the module boundary. Driving a real run would need an
OpenRouter key and a network, and the thing worth testing here is the contract
between the route and the row, not the loop underneath it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import trace as trace_module
from app.config import get_settings

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("BLOOM_SEARCH_API_KEY", "tvly-test")

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


def settled(client, build_id: str) -> dict:
    """Read a build once it has stopped running.

    The route answers 202 while the work continues behind it, so a test that reads
    immediately is racing its own background task. Polling here rather than sleeping
    keeps the test fast and states what it is waiting for.
    """
    for _ in range(50):
        body = client.get(f"/admin/builds/{build_id}", headers=AUTH).json()
        if body.get("status") != "running":
            return body
    raise AssertionError(f"build {build_id} never settled")


@pytest.fixture
def no_model(monkeypatch):
    """Replace the run with something that records what a real builder would."""
    from app.builder import service as service_module

    made = {"n": 0}

    async def fake_start_build(brief, *, build_id=None, run_id=None, store=None, **kw):
        from app.db import get_store, new_id

        store = store or get_store()
        build_id = build_id or new_id()
        run_id = run_id or new_id()
        await asyncio.to_thread(store.create_build, build_id=build_id, run_id=run_id, brief=brief)
        made["n"] += 1
        # A unique slug per call: two builds in one test would otherwise collide on
        # the UNIQUE index, which is real behaviour but not what these tests are for.
        slug = "spotify-dj" if made["n"] == 1 else f"spotify-dj-{made['n']}"
        config = await asyncio.to_thread(store.create_config, slug=slug, name="Spotify DJ")
        return await asyncio.to_thread(
            store.update_build,
            build_id,
            agent_config_id=config["id"],
            agent_slug=slug,
            status="needs_setup",
            summary="Used the shipped Spotify manifest; no usable MCP server exists.",
            checklist=[
                {
                    "kind": "register_oauth_app",
                    "title": "Register a Spotify app",
                    "url": "https://developer.spotify.com/dashboard",
                    "detail": "",
                    "connection_name": "spotify",
                    "env_name": "",
                    "done_when": "you have a client id and secret",
                },
                {
                    "kind": "connect_oauth",
                    "title": "Press Connect",
                    "url": "",
                    "detail": "",
                    "connection_name": "spotify",
                    "env_name": "",
                    "done_when": "",
                },
            ],
        )

    monkeypatch.setattr(service_module, "start_build", fake_start_build)
    import app.admin.builder as builder_routes

    monkeypatch.setattr(builder_routes, "start_build", fake_start_build)
    return fake_start_build


# --- starting -----------------------------------------------------------------


def test_a_build_answers_immediately_with_somewhere_to_watch_it(client, no_model):
    """202, not 200: a synchronous call could not hand you an id before it ended."""
    started = client.post("/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"})
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["status"] == "running"
    # The stream is the existing run endpoint — that reuse is the whole point.
    assert body["stream_url"] == f"/admin/runs/{body['run_id']}/events"
    assert body["trace_url"] == f"/admin/builds/{body['build_id']}"


def test_the_build_settles_and_carries_its_checklist(client, no_model):
    started = client.post(
        "/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"}
    ).json()

    body = settled(client, started["build_id"])
    assert body["status"] == "needs_setup"
    assert body["agent_slug"] == "spotify-dj"
    assert [s["kind"] for s in body["checklist"]] == ["register_oauth_app", "connect_oauth"]
    assert body["done"] == []


def test_an_empty_brief_is_refused_before_anything_is_spent(client, no_model):
    refused = client.post("/admin/builder/build", headers=AUTH, json={"brief": "x"})
    assert refused.status_code == 422


def test_a_build_needs_a_search_key_and_says_which_one_is_missing(client, monkeypatch, no_model):
    """503 with the reason, because every cause is a setup problem, not a failure."""
    monkeypatch.setenv("BLOOM_SEARCH_API_KEY", "")
    get_settings.cache_clear()
    refused = client.post("/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"})
    assert refused.status_code == 503
    assert "BLOOM_SEARCH_API_KEY" in refused.json()["message"]


def test_a_build_without_an_openrouter_key_is_also_503(client, monkeypatch, no_model):
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    refused = client.post("/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"})
    assert refused.status_code == 503
    assert "BLOOM_OPENROUTER_API_KEY" in refused.json()["message"]


# --- reading and ticking ------------------------------------------------------


def test_a_step_can_be_ticked_off_and_ticking_twice_is_not_an_error(client, no_model):
    started = client.post(
        "/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"}
    ).json()
    build_id = started["build_id"]
    settled(client, build_id)

    first = client.post(f"/admin/builds/{build_id}/steps/0/done", headers=AUTH)
    assert first.status_code == 200
    assert first.json()["done"] == [0]

    again = client.post(f"/admin/builds/{build_id}/steps/0/done", headers=AUTH)
    assert again.json()["done"] == [0]

    second = client.post(f"/admin/builds/{build_id}/steps/1/done", headers=AUTH)
    assert second.json()["done"] == [0, 1]


def test_ticking_a_step_that_does_not_exist_is_a_404_naming_how_many_there_are(client, no_model):
    started = client.post(
        "/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"}
    ).json()
    settled(client, started["build_id"])
    missing = client.post(f"/admin/builds/{started['build_id']}/steps/9/done", headers=AUTH)
    assert missing.status_code == 404
    assert "2 step(s)" in missing.json()["message"]


def test_builds_are_listed_newest_first_and_filterable_by_status(client, no_model):
    for _ in range(2):
        started = client.post(
            "/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"}
        ).json()
        settled(client, started["build_id"])
    listed = client.get("/admin/builds", headers=AUTH, params={"status": "needs_setup"})
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    assert client.get("/admin/builds", headers=AUTH, params={"status": "ready"}).json() == []


def test_an_unknown_build_uses_the_ecosystem_error_envelope(client):
    body = client.get("/admin/builds/nope", headers=AUTH).json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "not_found"


def test_deleting_a_build_leaves_the_agent_it_created_alone(client, no_model):
    """Deleting the record of how something was made is not deleting the thing."""
    started = client.post(
        "/admin/builder/build", headers=AUTH, json={"brief": "a spotify agent"}
    ).json()
    settled(client, started["build_id"])
    assert client.delete(f"/admin/builds/{started['build_id']}", headers=AUTH).status_code == 204
    assert client.get(f"/admin/builds/{started['build_id']}", headers=AUTH).status_code == 404

    agents = [a["slug"] for a in client.get("/admin/agents", headers=AUTH).json()]
    assert "spotify-dj" in agents


def test_the_build_surface_requires_the_admin_key(client):
    assert client.get("/admin/builds").status_code == 401
    assert client.post("/admin/builder/build", json={"brief": "a spotify agent"}).status_code == 401
