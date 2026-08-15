# bloom

The place you **define an agent** instead of building an app for it.

Amber should be able to hand "put on something mellow" to a music agent without
carrying a Spotify integration herself. Bloom is where that agent is defined — a
system prompt, a model tier, and the **connections** it may act through — and where
it runs when someone delegates to it. Adding a capability to the ecosystem becomes a
row in a table rather than a repo, a container, a subdomain and a deploy.

A connection is an OAuth account, an API key, or an MCP server, and it lives in a
global library: approve Spotify once and any agent can attach it. Creating an agent
asks for a slug and nothing else.

It is the service the ecosystem docs have been calling `agent-spawner`.

## Two surfaces, two audiences

| | | |
|---|---|---|
| `/mcp` | **Execution.** One tool, `run_task`. | How *other agents* delegate. MCP, because a model has to discover and choose it. |
| `/admin/*` | **Management.** CRUD over configs, test runs, run history, OAuth. | How *Aperture* edits them. Plain REST, because a GUI wants an OpenAPI schema and a generated client, not tool selection. |

Both are bearer-authenticated, with **deliberately separate key sets**
(`BLOOM_MCP_KEYS` and `BLOOM_ADMIN_KEYS`): a desktop GUI that can edit
configuration should not hold a token that also spends money.

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  .venv/bin/activate on Linux/macOS
pip install -e ".[dev]"
cp .env.example .env              # then set BLOOM_ADMIN_KEYS at minimum

pytest -q                         # needs nothing running
uvicorn app.main:app --reload --port 8010
```

`make help` lists the shortcuts. There is no database to start: Bloom's store is
one SQLite file, the ecosystem house pattern, and `sqlite3` is in the stdlib.

Define an agent and ask for it back:

```bash
curl -X POST -H "Authorization: Bearer $KEY" localhost:8010/admin/agents \
     -H 'content-type: application/json' \
     -d '{"slug":"spotify-dj","system_prompt":"You pick music."}'
curl -H "Authorization: Bearer $KEY" localhost:8010/admin/agents
```

## How it fits together

* **`agent-runtime`** owns the loop — stream, tool call, execute, feed back,
  repeat. Bloom builds an `AgentRunner` per run from a config row and never
  reimplements that.
* **`agent-mcp-py`** owns the conventions — bearer auth, the 5-hop depth guard,
  per-call usage logging, sync-store registration. They arrive with the
  decorators.
* **One config prefix.** Both libraries can read their own `AGENT_RUNTIME_*` /
  `AGENT_MCP_*` environment. Neither is allowed to: `app/config.py` is the only
  surface, and the settings objects are built from it with `_env_file=None`. Two
  surfaces would eventually disagree about which database file to write.
* **One SQLite file, three writers.** `bloom.db` holds Bloom's tables plus
  `agent_mcp_usage` and `agent_runtime_usage`. They share `conversation_id` /
  `app_name` / `depth` / `created_at`, so what a run cost joins to the calls that
  caused it. That co-tenancy is why WAL and a busy timeout are mandatory here and
  why the trace writer runs off the model loop's thread.

## What is built

All of it, with 75 tests and no network needed to run them.

- [x] Agent config CRUD (`/admin/agents`), the store, the error envelope, auth
- [x] `/mcp` — `run_task`, `list_agents`, the `bloom://agents` resource
- [x] Execution: `runtime_service.build_runner`, ceilings, broker teardown
- [x] Run trace: `run_events`, live SSE with `Last-Event-ID` resumption, test-run
- [x] Connections: one library, three kinds (`oauth` / `api_key` / `mcp`), shared
      across agents, with a `/test` probe that actually contacts the far end
- [x] OAuth: provider manifests, Fernet-at-rest, PKCE, the `aperture://` handoff
- [x] Proactive token refresh, plus a call-time check the sweep cannot replace
- [x] [docs/aperture-integration.md](docs/aperture-integration.md) + `docs/openapi.json`

Not done, and deliberately: **deploying it.** That is a change to `amber-infra` —
a `bloom/docker-compose.prod.yml`, an `apps.bloom` stanza in `secrets.yaml`, and
`install.sh --app bloom --domain bloom.johnny.dev --upstream 127.0.0.1:8010`.

## Adding a provider

A file, not code. Copy [app/providers/spotify.toml](app/providers/spotify.toml) (an
OAuth provider) or [app/providers/github.toml](app/providers/github.toml) (which
also accepts a pasted key), say which kinds of credential it takes with `auth`, and
declare one `[[operations]]` block per thing an agent should be able to do. Each becomes a tool
named `<provider>_<operation>` whose `description` is the only thing the model sees
— so write it for the model, naming the fields it should read out of the response.

The loader refuses a manifest that would break something far away: a tool name over
40 characters or containing `__` (which would collide with MCP's `server__tool`
namespacing), an operation with no description, a parameter in a header, or a
parameter named `authorization`/`token`/`access_token`/`api_key`/`cookie`. That last
one matters most: a tool argument ends up in the model's context, in the runtime's
log, and in Bloom's own trace, so a credential must never be settable as one.

Tokens are never held by the tools either. Each closes over a *connection id* and
asks for a live token when it fires, which is what lets a token expire mid-run and
be refreshed without the task failing.

## One promise this repo does *not* make

Every other app in the ecosystem asserts in its tests that with the MCP flag off
it never *imports* the agent stack — the core principle that an app must run
standalone with zero knowledge the ecosystem exists.

**Bloom cannot claim that.** It is the one service whose purpose *is* the agent
layer: the admin API imports `agent_mcp.auth` to check a token and
`agent_runtime.model_router` to validate a tier. What is true, and what
`tests/test_mcp.py` pins instead, is the weaker guarantee: with MCP off no `/mcp`
routes appear, `app.mcp` is never imported, and the management API serves
normally — so editing configurations before you have an OpenRouter key is a
supported state rather than an accident.
