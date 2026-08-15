"""Execution and the run trace.

The model is never called. `build_runner` is replaced with a fake whose ``run``
returns a canned `RunResult`-shaped object, which is enough to exercise everything
Bloom actually owns: the run row, the event log, the terminal event, teardown on
the error path, and the ceilings.

Fixtures are local and duplicated on purpose, and the client is configured through
the environment rather than by patching `get_store` into individual modules — see
`tests/test_admin_agents.py` for why that distinction matters here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import runtime_service
from app import trace as trace_module
from app.config import Settings, get_settings

ADMIN_TOKEN = "admin-secret"  # noqa: S105 — a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# --- a stand-in for agent_runtime's RunResult / Step -------------------------


@dataclass
class FakeStep:
    index: int = 0
    model: str = "anthropic/claude-haiku-4.5"
    text: str = ""
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 10
    tokens_out: int = 20
    cost_usd: float = 0.001
    started_at: str = "2026-01-01T00:00:00+00:00"
    finished_at: str = "2026-01-01T00:00:01+00:00"


@dataclass
class FakeResult:
    text: str = "the answer"
    total_cost: float = 0.001
    steps: list = field(default_factory=lambda: [FakeStep()])
    stopped_by: str | None = None


class FakeRunner:
    """Records what it was asked to do, and narrates one sentence on the way."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self.result = result or FakeResult()
        self.raises = raises
        self.calls: list[dict] = []

    async def run(self, prompt, *, conversation_id=None, depth=0, on_sentence=None, **kw):
        self.calls.append({"prompt": prompt, "conversation_id": conversation_id, "depth": depth})
        if on_sentence is not None:
            await on_sentence("Working on it.")
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOOM_DB_PATH", str(tmp_path / "bloom.db"))
    monkeypatch.setenv("BLOOM_ADMIN_KEYS", f"tester:{ADMIN_TOKEN}")
    monkeypatch.setenv("BLOOM_FEATURE_MCP", "false")
    monkeypatch.setenv("BLOOM_MCP_KEYS", "")
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


def _make_agent(client, **over) -> dict:
    body = {"slug": "tester", "system_prompt": "Be brief.", **over}
    response = client.post("/admin/agents", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _install_fake_runner(monkeypatch, runner: FakeRunner) -> dict:
    """Replace `build_runner`, recording whether teardown was awaited."""
    state = {"closed": False}

    async def aclose() -> None:
        state["closed"] = True

    monkeypatch.setattr(runtime_service, "build_runner", lambda *a, **kw: (runner, aclose))
    return state


def _wait_for_terminal(client, config_id: str, run_id: str, tries: int = 100) -> dict:
    """Poll the trace until the run is no longer `running`.

    The run is a background task, so a test that read once would race it. Polling
    the real endpoint is also the only way to assert the endpoint stays correct
    while a run is in flight.
    """
    import time

    for _ in range(tries):
        body = client.get(f"/admin/agents/{config_id}/runs/{run_id}/trace", headers=AUTH).json()
        if body["run"]["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never finished: {body['run']}")


# --- the happy path ---------------------------------------------------------


def test_a_test_run_records_the_run_its_text_and_its_steps(client, monkeypatch):
    runner = FakeRunner()
    _install_fake_runner(monkeypatch, runner)
    agent = _make_agent(client)

    started = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "hello"}
    )
    # 202 with the id up front, so a client can attach to the trace before the run
    # ends — the entire reason this is not a synchronous call.
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    assert started.json()["stream_url"].endswith(f"/admin/runs/{run_id}/events")

    body = _wait_for_terminal(client, agent["id"], run_id)
    assert body["run"]["status"] == "succeeded"
    assert body["run"]["result_text"] == "the answer"
    assert body["run"]["total_cost_usd"] == pytest.approx(0.001)
    assert body["run"]["origin"] == "test_run"
    assert body["run"]["caller"] == "tester"

    kinds = [e["kind"] for e in body["events"]]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "text" in kinds, kinds
    assert "step_finished" in kinds, kinds

    # Event ids are monotonic — they are the SSE cursor, so this is load-bearing.
    ids = [e["id"] for e in body["events"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)

    assert runner.calls[0]["prompt"] == "hello"


def test_the_run_appears_in_the_agents_history(client, monkeypatch):
    _install_fake_runner(monkeypatch, FakeRunner())
    agent = _make_agent(client)
    run_id = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "hi"}
    ).json()["run_id"]
    _wait_for_terminal(client, agent["id"], run_id)

    history = client.get(f"/admin/agents/{agent['id']}/runs", headers=AUTH).json()
    assert [r["id"] for r in history] == [run_id]


def test_a_trace_is_not_served_under_the_wrong_agent(client, monkeypatch):
    """A run id is unguessable, but serving it under another config's URL would
    make the history view quietly wrong."""
    _install_fake_runner(monkeypatch, FakeRunner())
    owner = _make_agent(client)
    other = _make_agent(client, slug="other")
    run_id = client.post(
        f"/admin/agents/{owner['id']}/test-run", headers=AUTH, json={"input": "hi"}
    ).json()["run_id"]
    _wait_for_terminal(client, owner["id"], run_id)

    wrong = client.get(f"/admin/agents/{other['id']}/runs/{run_id}/trace", headers=AUTH)
    assert wrong.status_code == 404


# --- the failure paths ------------------------------------------------------


def test_a_failed_run_is_still_closed_out_and_still_tears_the_broker_down(client, monkeypatch):
    """The leak this guards against is real: agent_runtime only closes a broker it
    built itself, so with an injected one Bloom's `finally` is the only teardown.
    """
    runner = FakeRunner(raises=RuntimeError("upstream exploded"))
    state = _install_fake_runner(monkeypatch, runner)
    agent = _make_agent(client)

    run_id = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "boom"}
    ).json()["run_id"]
    body = _wait_for_terminal(client, agent["id"], run_id)

    assert body["run"]["status"] == "failed"
    assert "upstream exploded" in body["run"]["error"]
    # The terminal event must exist even here: it is the only thing that ends a
    # stream, so without it a live viewer would hang on a run that already failed.
    assert body["events"][-1]["kind"] == "run_finished"
    assert body["events"][-1]["ok"] is False
    assert state["closed"] is True


def test_a_test_run_is_refused_when_no_model_key_is_configured(client, monkeypatch):
    monkeypatch.setenv("BLOOM_OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    agent = _make_agent(client)
    refused = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "hi"}
    )
    assert refused.status_code == 503
    assert refused.json()["error"] == "unavailable"


def test_a_test_run_against_an_unknown_agent_is_a_404(client):
    assert (
        client.post("/admin/agents/nope/test-run", headers=AUTH, json={"input": "hi"}).status_code
        == 404
    )


# --- streaming --------------------------------------------------------------


def test_the_stream_replays_a_finished_run_and_then_ends(client, monkeypatch):
    """Replay-then-tail means a viewer that connects late loses nothing."""
    _install_fake_runner(monkeypatch, FakeRunner())
    agent = _make_agent(client)
    run_id = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "hi"}
    ).json()["run_id"]
    _wait_for_terminal(client, agent["id"], run_id)

    with client.stream("GET", f"/admin/runs/{run_id}/events", headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: run_started" in body
    assert "event: run_finished" in body
    # Every event carries its id, which is what makes Last-Event-ID resumption work.
    assert "id: " in body


def test_the_stream_resumes_from_last_event_id(client, monkeypatch):
    _install_fake_runner(monkeypatch, FakeRunner())
    agent = _make_agent(client)
    run_id = client.post(
        f"/admin/agents/{agent['id']}/test-run", headers=AUTH, json={"input": "hi"}
    ).json()["run_id"]
    trace = _wait_for_terminal(client, agent["id"], run_id)
    first_id = trace["events"][0]["id"]

    with client.stream(
        "GET",
        f"/admin/runs/{run_id}/events",
        headers={**AUTH, "Last-Event-ID": str(first_id)},
    ) as response:
        body = "".join(response.iter_text())

    # The replayed-from event itself is excluded; the cursor is exclusive.
    assert f"id: {first_id}\n" not in body
    assert "event: run_finished" in body


def test_streaming_an_unknown_run_is_a_404(client):
    assert client.get("/admin/runs/nope/events", headers=AUTH).status_code == 404


# --- the store's own guarantees ---------------------------------------------


def test_the_startup_sweep_closes_a_run_a_dead_process_left_behind(tmp_path):
    """Both halves matter. Without the terminal event a client tailing across a
    restart hangs forever, which is worse than a run marked lost."""
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="ghost")
    store.create_run(run_id="r1", agent_config_id=config["id"], prompt="p", origin="mcp")

    assert store.sweep_abandoned_runs() == ["r1"]

    run = store.get_run("r1")
    assert run["status"] == "abandoned"
    assert run["finished_at"] is not None
    assert [e["kind"] for e in store.events_after("r1")] == ["run_finished"]

    # Idempotent: a second sweep finds nothing, so a restart loop cannot pile up
    # terminal events on the same run.
    assert store.sweep_abandoned_runs() == []
    store.close()


def test_event_ids_are_the_cursor_and_events_after_is_exclusive(tmp_path):
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="cursors")
    store.create_run(run_id="r1", agent_config_id=config["id"], prompt="p", origin="mcp")

    first = store.append_event("r1", seq=0, kind="run_started")
    second = store.append_event("r1", seq=1, kind="text", payload={"text": "hi"})
    assert second > first

    assert [e["id"] for e in store.events_after("r1", first)] == [second]
    assert store.events_after("r1", second) == []
    # The payload round-trips as a dict, not a JSON string.
    assert store.events_after("r1", first)[0]["payload"] == {"text": "hi"}
    store.close()


def test_appending_an_event_touches_the_runs_heartbeat(tmp_path):
    """A run emitting events is alive by definition, so the reaper needs no
    separate signal — which only works if the heartbeat actually moves."""
    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="beats")
    store.create_run(run_id="r1", agent_config_id=config["id"], prompt="p", origin="mcp")
    store._conn.execute("UPDATE runs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
    store._conn.commit()

    store.append_event("r1", seq=0, kind="text")
    assert store.get_run("r1")["heartbeat_at"] > "2001-01-01"
    store.close()


# --- the runner Bloom assembles ---------------------------------------------


def _runtime_settings(**over) -> Settings:
    return Settings(**{"_env_file": None, "db_path": ":memory:", **over})


def test_ceilings_clamp_downwards_only(tmp_path):
    """A config may lower a ceiling. It must not be able to raise one — a
    delegated task is unattended, so this is the only bound on its cost."""
    from app.trace import RunRecorder, TraceWriter

    settings = _runtime_settings(max_steps=8, max_cost_usd=0.50)
    store = db_module.Store(str(tmp_path / "bloom.db"))
    recorder = RunRecorder(TraceWriter(store), "r1")

    greedy = {
        "id": "1",
        "slug": "s",
        "model_tier": "balanced",
        "max_steps": 99,
        "max_cost_usd": 99.0,
    }
    runner, _ = runtime_service.build_runner(
        greedy, recorder=recorder, settings=settings, store=store
    )
    limits = {type(c).__name__: c for c in runner.stop_conditions}
    assert limits["StopOnSteps"].max_steps == 8
    assert limits["StopOnCost"].max_cost_usd == pytest.approx(0.50)

    modest = {
        "id": "1",
        "slug": "s",
        "model_tier": "balanced",
        "max_steps": 2,
        "max_cost_usd": 0.01,
    }
    runner, _ = runtime_service.build_runner(
        modest, recorder=recorder, settings=settings, store=store
    )
    limits = {type(c).__name__: c for c in runner.stop_conditions}
    assert limits["StopOnSteps"].max_steps == 2
    assert limits["StopOnCost"].max_cost_usd == pytest.approx(0.01)
    store.close()


def test_both_stop_conditions_are_always_passed_explicitly(tmp_path):
    """Any non-empty list *replaces* the runtime's default StopOnSteps, so a list
    with only a cost guard would leave the step count bounded solely by the hard
    cap — and hitting that cap forces an extra, unlogged completion."""
    from app.trace import RunRecorder, TraceWriter

    store = db_module.Store(str(tmp_path / "bloom.db"))
    recorder = RunRecorder(TraceWriter(store), "r1")
    runner, _ = runtime_service.build_runner(
        {"id": "1", "slug": "s", "model_tier": "balanced"},
        recorder=recorder,
        settings=_runtime_settings(),
        store=store,
    )
    assert {type(c).__name__ for c in runner.stop_conditions} == {"StopOnSteps", "StopOnCost"}
    store.close()


def test_runtime_settings_do_not_come_from_the_environment(monkeypatch):
    """Two config surfaces would eventually disagree about which database to
    write, and the cost ledger would silently land in a file nobody reads."""
    monkeypatch.setenv("AGENT_RUNTIME_DB_PATH", "/tmp/wrong.db")
    monkeypatch.setenv("AGENT_RUNTIME_OPENROUTER_API_KEY", "sneaky")
    built = runtime_service.runtime_settings(
        _runtime_settings(db_path="right.db", openrouter_api_key="real")
    )
    assert built.db_path == "right.db"
    assert built.openrouter_api_key == "real"
    assert built.app_name == "bloom"


# --- the tracing broker -----------------------------------------------------


class _RecordingBroker:
    def __init__(self, result: str = "done") -> None:
        self.result = result
        self.bound: dict | None = None
        self.closed = False

    async def list_tools(self):
        return [{"type": "function", "function": {"name": "t"}}]

    async def call_tool(self, name, args):
        return self.result

    def bind(self, **kwargs):
        self.bound = kwargs

    async def aclose(self):
        self.closed = True


def test_the_tracing_broker_narrates_a_call_and_forwards_lifecycle(tmp_path):
    from app.trace import RunRecorder, TraceWriter, TracingBroker

    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="tools")
    store.create_run(run_id="r1", agent_config_id=config["id"], prompt="p", origin="mcp")

    writer = TraceWriter(store)
    inner = _RecordingBroker()
    broker = TracingBroker(inner, RunRecorder(writer, "r1"))

    async def exercise():
        # bind must accept and forward **kwargs: CompositeBroker fans arbitrary
        # keywords out to its children, so a pinned signature swallows anything
        # the runtime adds later.
        broker.bind(conversation_id="c1", depth=2, confirmed=False)
        assert await broker.call_tool("search", {"q": "hi"}) == "done"
        await broker.aclose()
        # Drain the queue by hand: the writer task is not running in this test.
        while not writer._queue.empty():
            event = writer._queue.get_nowait()
            store.append_event("r1", seq=event.seq, kind=event.kind, **event.fields)

    asyncio.run(exercise())

    assert inner.bound == {"conversation_id": "c1", "depth": 2, "confirmed": False}
    assert inner.closed is True

    kinds = [e["kind"] for e in store.events_after("r1")]
    assert kinds == ["tool_started", "tool_finished"]
    started, finished = store.events_after("r1")
    assert started["tool_name"] == "search"
    assert json.loads(started["payload"]["args"]) == {"q": "hi"}
    assert finished["ok"] is True
    assert finished["latency_ms"] >= 0
    store.close()


def test_a_tool_that_returns_an_error_string_is_traced_as_failed(tmp_path):
    """The brokers underneath convert every tool failure into a result *string*,
    so `ok` has to be read off the text rather than off an exception."""
    from app.trace import RunRecorder, TraceWriter, TracingBroker

    store = db_module.Store(str(tmp_path / "bloom.db"))
    config = store.create_config(slug="tools")
    store.create_run(run_id="r1", agent_config_id=config["id"], prompt="p", origin="mcp")

    writer = TraceWriter(store)
    broker = TracingBroker(
        _RecordingBroker("Error running search: nope"), RunRecorder(writer, "r1")
    )

    async def exercise():
        await broker.call_tool("search", {})
        while not writer._queue.empty():
            event = writer._queue.get_nowait()
            store.append_event("r1", seq=event.seq, kind=event.kind, **event.fields)

    asyncio.run(exercise())
    assert store.events_after("r1")[1]["ok"] is False
    store.close()


def test_a_full_trace_queue_drops_events_rather_than_blocking_the_run(tmp_path):
    """Losing a line of narration is strictly better than stalling the run it
    describes — `emit` is called from inside the generation loop."""
    from app.trace import RunRecorder, TraceWriter

    store = db_module.Store(str(tmp_path / "bloom.db"))
    writer = TraceWriter(store, maxsize=2)
    recorder = RunRecorder(writer, "r1")
    for _ in range(50):
        recorder.text("chatter")  # must not raise
    assert writer._queue.qsize() == 2
    assert writer._dropped == 48
    store.close()
