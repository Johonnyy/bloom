"""Every knob Bloom has, in one place.

**One prefix, three consumers.** Bloom embeds two shared libraries — `agent_mcp`
(its execution surface) and `agent_runtime` (the loop that actually runs a
configured agent) — and each can read its own ``AGENT_MCP_*`` / ``AGENT_RUNTIME_*``
environment. Neither is allowed to: `app.mcp` and `app.runtime_service` build their
settings objects from the values below with ``_env_file=None``.

That is not tidiness. `agent_runtime.Settings` defaults ``db_path`` to
``agent_runtime.db`` in the working directory; if it were left to read its own
environment, Bloom's cost rows would land in a file nobody looks at while its
tables lived somewhere else, and the join between what was spent and what caused
it would silently be empty. One prefix, one source of truth. Add a knob here and
inject it; never introduce a second prefix into ``.env``.

**Where provider client credentials live.** They are deliberately *not* fields
here. A provider manifest (``app/providers/*.toml``) names the environment
variables holding its client id and secret, and `app.providers.registry` reads
them by that name — the set of providers is not known when this class is defined,
and putting them here would mean editing Python to add a provider, which is the
exact thing the manifest format exists to avoid.

That indirection is also why `load_dotenv` is called below; see its comment.
"""

from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The one deliberate export of `.env` into the process environment.
#
# pydantic-settings reads `.env` into the *model* without exporting it, so a
# variable named by a provider manifest rather than declared below — every
# ``BLOOM_OAUTH_<PROVIDER>_CLIENT_ID`` — resolves under Docker (whose ``env_file``
# genuinely exports) and silently does not under a bare ``uvicorn``. The symptom is
# a provider reporting itself unconfigured while its credentials sit in `.env`.
# This is the same trap `app/mcp.py` documents for ``auth_keys_env``; there the fix
# was to drop the indirection, and here the indirection is the feature.
#
# ``override=False`` is load-bearing: a real environment variable still wins, so
# compose, systemd and a test's ``monkeypatch.setenv`` all behave exactly as before.
#
# The path is given explicitly rather than left to ``find_dotenv``, which walks up
# from *this file* while ``env_file`` below resolves against the working directory.
# Same string, same semantics: either both find the file or neither does, and there
# is never one half of the config reading a `.env` the other half cannot see.
load_dotenv(".env", override=False)


class Settings(BaseSettings):
    """Configuration, read from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="BLOOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Load-bearing: this process also carries the AGENT_MCP_* and
        # AGENT_RUNTIME_* variables the embedded libraries would read if we let
        # them. Without this, one of them is a hard validation error at boot.
        extra="ignore",
    )

    # --- App ---
    # Doubles as the MCP namespace and the resource URI scheme, so it must be
    # snake_case and <= 22 chars; agent-mcp-py validates it at construction.
    app_name: str = "bloom"
    version: str = "0.2.1"
    port: int = 8010
    log_level: str = "info"

    # This service's externally reachable base URL, used to build the OAuth
    # redirect_uri. A provider redirects a browser here, so it must be the public
    # name and not a loopback address.
    public_url: str = ""

    # --- Storage ---
    # One SQLite file, three tenants: Bloom's own tables, agent_mcp's tool log
    # (agent_mcp_usage) and agent_runtime's cost rows (agent_runtime_usage). The
    # shared conversation_id / app_name / depth / created_at columns are what let
    # spend be joined to the calls that caused it. WAL and a busy timeout make the
    # three-way tenancy safe; see app/db.py.
    db_path: str = "data/bloom.db"

    # --- The loop (agent_runtime) ---
    openrouter_api_key: str = Field(
        default="",
        description="OpenRouter API key. Every configured agent runs through it.",
    )
    # The tier a config falls back to when it names none. A *named tier*, not a
    # model id — agent_runtime.model_router resolves it, so upgrading every agent
    # in the ecosystem is one edit there rather than one per config. A literal
    # "vendor/model" still passes through as an escape hatch.
    default_tier: str = "balanced"
    max_tokens: int = 2048
    # Ceilings a config may lower but not raise. A delegated task is unattended by
    # definition: nobody is watching the tool loop, so the ceiling is the only
    # thing between a bad prompt and an unbounded bill.
    max_steps: int = 8
    max_cost_usd: float = 0.50
    # Wall-clock cap on one run. Without it a hung provider call leaves a run
    # marked `running` forever and an SSE client tailing it hangs with no
    # terminal event.
    run_timeout_s: float = 300.0

    # --- Bloom's MCP server: the execution surface ---
    # On by default, unlike the template's opt-out flag. Every other app in the
    # ecosystem is a standalone app that *may* expose itself over MCP; being
    # reachable over MCP is the entire reason Bloom exists. It still fails closed
    # without keys — see mcp_enabled.
    feature_mcp: bool = True
    mcp_keys: str = Field(
        default="",
        description="Bearer keys other agents present to call run_task, as "
        "'name:token' pairs. Empty is safe — the server is simply not mounted.",
    )
    mcp_public_url: str = ""
    mcp_sync_store_url: str = ""
    mcp_sync_store_token: str = ""
    # Peers a configured agent may call, as "name=https://host" pairs. Parsed by
    # agent_mcp.registry.load_static_peers — Bloom does not invent a second format
    # for the same thing. The static layer sits in front of whatever the sync
    # store discovers, so this is an override, not a fallback.
    mcp_peers: str = ""
    mcp_peer_token: str = ""

    # --- The management surface (Aperture) ---
    # Deliberately a *separate* key set from mcp_keys. Aperture is a different
    # principal from a peer agent: it edits configs and reads run history, but a
    # GUI holding a key that also lets it call run_task means one leaked token
    # buys both. Same format as mcp_keys.
    admin_keys: str = Field(default="", description="Bearer keys for /admin.")

    # --- Model keywords ---
    # Amber's vocabulary, not agent_runtime's three-rung ladder. A ladder can only
    # say "better"; it cannot say "for code" or "for a long document", which is how
    # a model is actually chosen. `app/models.py` ships the same ten keywords Amber
    # does and overlays whatever the sync store says they currently point at, so
    # re-pointing `coding` once moves the whole fleet instead of needing a release
    # per app. With no sync store configured Bloom simply uses its built-ins.
    model_sync_interval_s: float = 300.0

    # --- The builder: the agent that writes other agents ---
    feature_builder: bool = True
    # The builder reasons about an unfamiliar API from search results and has to
    # write another agent's instructions, which is the kind of open-ended work the
    # cheap rungs are bad at. This is the one place paying for a strong model is
    # obviously worth it: you pay once to author something you then run cheaply.
    builder_keyword: str = "strong"
    # The builder's own ceilings, deliberately separate from the service-wide ones.
    # A real build is 12-25 steps (inspect, search the registry, read a page or two,
    # create, verify, write the checklist) against a service default of 8. Raising
    # the global ceiling instead would raise it for every unattended delegated task,
    # which is precisely what that ceiling exists to prevent.
    builder_max_steps: int = 30
    builder_max_cost_usd: float = 2.00
    builder_run_timeout_s: float = 900.0

    # --- Research (the builder's eyes) ---
    # Tavily. Without a key the builder is reported unavailable rather than quietly
    # downgraded: it cannot do its job blind, and a build that guesses an API from
    # memory is worse than one that refuses.
    search_api_key: str = ""
    search_max_results: int = 5
    search_timeout_s: float = 15.0
    # `read_url` fetches pages nobody vetted, so both limits bound a hostile answer:
    # bytes off the wire, and characters handed to the model.
    read_url_max_bytes: int = 400_000
    read_url_max_chars: int = 20_000
    read_url_timeout_s: float = 20.0
    mcp_registry_url: str = "https://registry.modelcontextprotocol.io"

    # --- OAuth (Phase 4) ---
    feature_oauth: bool = False
    # Fernet keys, comma-separated, newest first: the head encrypts, all decrypt.
    # MultiFernet from day one — it costs one line now and is the difference
    # between rotation being an admin endpoint and rotation being a migration
    # with downtime. Sourced from the amber-infra secrets file, never from the DB
    # and never derived from a passphrase at runtime.
    fernet_keys: str = ""
    oauth_refresh_interval_s: float = 300.0
    # How far ahead of expiry a token is refreshed. Generous on purpose: the cost
    # of refreshing early is one HTTP call, the cost of refreshing late is a task
    # failing mid-run.
    oauth_refresh_skew_s: float = 600.0

    @property
    def mcp_enabled(self) -> bool:
        """True only when the MCP server is both wanted and configured.

        agent-mcp-py fails closed: ``routes()`` raises rather than serve an
        unauthenticated app. Without this second condition, a deploy that set the
        flag but not the keys would crash at startup instead of quietly not
        exposing a server nobody had finished configuring.
        """
        return self.feature_mcp and bool(self.mcp_keys.strip())

    @property
    def oauth_enabled(self) -> bool:
        """True only when tokens can actually be encrypted.

        Storing an OAuth token without a key is not a degraded mode, it is a
        breach, so the OAuth routes report 503 rather than fall back to plaintext.
        A *missing* key with connections already stored is a different and worse
        problem — that one raises at startup, in `app.crypto`.
        """
        return self.feature_oauth and bool(self.fernet_keys.strip())

    @property
    def builder_enabled(self) -> bool:
        """True only when the builder can actually do its job.

        Three things, not one. The flag is intent; the OpenRouter key is what runs
        any agent at all; the search key is what makes *this* agent different from
        a model guessing an API from memory. Missing any of them is reported as a
        503 naming which, rather than a build that starts, spends money and ends
        with a confidently wrong manifest.
        """
        return (
            self.feature_builder
            and bool(self.openrouter_api_key.strip())
            and bool(self.search_api_key.strip())
        )

    def builder_unavailable_reason(self) -> str:
        """Why the builder cannot run, as a sentence for a 503. Empty when it can."""
        if not self.feature_builder:
            return "The builder is switched off (BLOOM_FEATURE_BUILDER=false)."
        if not self.openrouter_api_key.strip():
            return "No BLOOM_OPENROUTER_API_KEY is configured, so no agent can run."
        if not self.search_api_key.strip():
            return (
                "No BLOOM_SEARCH_API_KEY is configured. The builder researches a "
                "service before wiring it, and refuses to work from memory."
            )
        return ""


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so reading config is free anywhere. Tests that need a different
    environment clear it with ``get_settings.cache_clear()``, or — better —
    construct ``Settings(_env_file=None, **overrides)`` directly and pass it in,
    which touches no global state at all.
    """
    return Settings()


settings = get_settings()
