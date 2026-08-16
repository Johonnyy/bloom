"""Model keywords: the vocabulary, where it comes from, and how it fails.

Bloom ships the ecosystem's ten keywords and overlays whatever the shared sync store
says they currently point at. Two properties matter more than the mapping itself:

* **Resolution never raises.** It runs immediately before a model call, where the
  cost of guessing wrong is one reply from the wrong model and the cost of raising is
  no reply at all.
* **An unreachable store leaves the previous table in place.** Degrading to built-in
  defaults mid-flight would silently re-point every keyword the moment the store
  hiccuped, which is a far worse failure than a stale table.

Offline throughout: the sync client is driven through an injected factory.
"""

from __future__ import annotations

import asyncio

import pytest

from app import models
from app.config import Settings


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:"}
    return Settings(**{**base, **over})


@pytest.fixture(autouse=True)
def clean_catalogue():
    """The override cache is module state; a leak between tests is a false pass."""
    models.invalidate_cache()
    yield
    models.invalidate_cache()


class FakeResponse:
    def __init__(self, status_code=200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def factory(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


SHARED = {
    "keywords": {
        "coding": {"model": "anthropic/claude-opus-4.5", "description": "Ours, re-pointed."},
        "sql": {"model": "vendor/sql-whisperer", "description": "A keyword we invented."},
        "broken": {"model": "not-a-model-id", "description": "No slash; must be dropped."},
    },
    "count": 3,
}


# --- the vocabulary -----------------------------------------------------------


def test_the_builtin_vocabulary_is_the_ecosystems_ten_keywords():
    """If this drifts from Amber's, one keyword means two things in one ecosystem."""
    assert [k.name for k in models.BUILTIN] == [
        "fast",
        "cheap",
        "balanced",
        "strong",
        "coding",
        "reasoning",
        "writing",
        "research",
        "vision",
        "long",
    ]


def test_every_builtin_points_at_a_real_looking_model_id():
    for keyword in models.BUILTIN:
        assert models.valid_model(keyword.model), keyword.name


# --- resolution ---------------------------------------------------------------


def test_a_literal_model_id_passes_straight_through():
    """The escape hatch: a model released this morning must be usable this morning."""
    assert models.resolve("vendor/brand-new", _settings()) == "vendor/brand-new"


def test_an_empty_keyword_means_the_install_default():
    assert models.resolve("", _settings(default_tier="cheap")) == models.BUILTIN_MODELS["cheap"]
    assert models.resolve(None, _settings(default_tier="cheap")) == models.BUILTIN_MODELS["cheap"]


def test_an_unknown_keyword_falls_back_rather_than_raising():
    """One reply from the wrong model beats no reply at all."""
    resolved = models.resolve("galaxy-brain", _settings(default_tier="balanced"))
    assert resolved == models.BUILTIN_MODELS["balanced"]


def test_the_three_agent_runtime_tiers_still_resolve():
    """A config written before this module existed must keep working."""
    for tier in ("cheap", "balanced", "strong"):
        assert "/" in models.resolve(tier, _settings())


def test_a_shared_override_wins_over_the_builtin_default():
    client = FakeClient(FakeResponse(payload=SHARED))
    settings = _settings(mcp_sync_store_url="https://sync.example")
    kept = asyncio.run(models.refresh(settings, client_factory=client.factory))

    # `broken` has no slash, so it is dropped rather than stored as a model id.
    assert kept == 2
    assert models.resolve("coding", settings) == "anthropic/claude-opus-4.5"
    # A keyword nobody re-pointed still means what Bloom shipped with.
    assert models.resolve("writing", settings) == models.BUILTIN_MODELS["writing"]
    # And a keyword invented elsewhere in the ecosystem becomes usable here.
    assert models.resolve("sql", settings) == "vendor/sql-whisperer"


def test_the_store_is_asked_for_the_documented_path():
    client = FakeClient(FakeResponse(payload=SHARED))
    settings = _settings(mcp_sync_store_url="https://sync.example/", mcp_sync_store_token="t")
    asyncio.run(models.refresh(settings, client_factory=client.factory))
    assert client.calls[0]["url"] == "https://sync.example/models"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer t"


# --- failure ------------------------------------------------------------------


def test_no_sync_store_means_the_builtins_are_the_whole_truth():
    client = FakeClient(FakeResponse(payload=SHARED))
    assert asyncio.run(models.refresh(_settings(), client_factory=client.factory)) == 0
    assert client.calls == []
    assert models.sync_enabled(_settings()) is False


def test_an_unreachable_store_keeps_the_table_it_already_had():
    """Degrading to defaults mid-flight would silently re-point every keyword."""
    settings = _settings(mcp_sync_store_url="https://sync.example")
    ok = FakeClient(FakeResponse(payload=SHARED))
    asyncio.run(models.refresh(settings, client_factory=ok.factory))
    assert models.resolve("coding", settings) == "anthropic/claude-opus-4.5"

    down = FakeClient(OSError("connection refused"))
    asyncio.run(models.refresh(settings, client_factory=down.factory))
    assert models.resolve("coding", settings) == "anthropic/claude-opus-4.5"


def test_an_error_status_and_an_unexpected_shape_are_both_survivable():
    settings = _settings(mcp_sync_store_url="https://sync.example")
    for response in (
        FakeResponse(status_code=503),
        FakeResponse(payload={"keywords": ["not", "a", "map"]}),
        FakeResponse(payload={}),
    ):
        assert (
            asyncio.run(models.refresh(settings, client_factory=FakeClient(response).factory)) == 0
        )
        assert models.resolve("coding", settings) == models.BUILTIN_MODELS["coding"]


# --- what the admin API validates against ------------------------------------


def test_known_accepts_keywords_and_literal_ids_and_rejects_typos():
    assert models.known("coding") is True
    assert models.known("balanced") is True
    assert models.known("vendor/anything") is True
    assert models.known("galaxy-brain") is False
    assert models.known("") is False
    assert models.known(None) is False


def test_the_reported_table_says_which_entries_were_re_pointed():
    """ "This is what Bloom shipped with" and "somebody changed it" are different."""
    settings = _settings(mcp_sync_store_url="https://sync.example")
    client = FakeClient(FakeResponse(payload=SHARED))
    asyncio.run(models.refresh(settings, client_factory=client.factory))

    by_name = {k["name"]: k for k in models.keywords(settings)}
    assert by_name["coding"]["overridden"] is True
    assert by_name["coding"]["builtin"] is True
    assert by_name["writing"]["overridden"] is False
    # A keyword invented elsewhere is present but not a built-in.
    assert by_name["sql"]["builtin"] is False
    assert by_name["sql"]["overridden"] is True
