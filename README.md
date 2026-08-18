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

**Or you can just describe one.** Tell Bloom "create a Spotify agent that can play
and search music" — from Aperture, or from Amber over MCP — and its own builder
researches the service, prefers an existing MCP server over anything it would have to
carry itself, picks a model keyword from the work, writes the agent and its
connections, and hands back the setup steps you still have to do. It never holds a
credential, so everything it makes is inert until you attach one.

It is the service the ecosystem docs have been calling `agent-spawner`.

## Two surfaces, two audiences

| | | |
|---|---|---|
| `/mcp` | **Execution.** `run_task` to delegate, `build_agent` to create, `edit_agent` to change one. | How *other agents* reach it. MCP, because a model has to discover and choose. |
| `/admin/*` | **Management.** CRUD over configs, connections, test runs, run history, builds. | How *Aperture* edits them. Plain REST, because a GUI wants an OpenAPI schema and a generated client, not tool selection. |

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

All of it, with 342 tests and no network needed to run them.

- [x] Agent config CRUD (`/admin/agents`), the store, the error envelope, auth
- [x] `/mcp` — `run_task`, `build_agent`, `edit_agent`, `list_agents`,
      `bloom://agents`, `bloom://builds`
- [x] Execution: `runtime_service.build_runner`, ceilings, broker teardown
- [x] Run trace: `run_events`, live SSE with `Last-Event-ID` resumption, test-run
- [x] Connections: one library, three kinds (`oauth` / `api_key` / `mcp`), shared
      across agents, with a `/test` probe that actually contacts the far end
- [x] OAuth: provider manifests, Fernet-at-rest, PKCE, the `aperture://` handoff
- [x] Proactive token refresh, plus a call-time check the sweep cannot replace
- [x] The builder (`app/builder/`) — MCP-registry-first research, Tavily search and
      a URL reader, agent and connection authoring, and a typed setup checklist.
      Privileged tools reach exactly one reserved slug and nothing else
- [x] **Provider manifests written at runtime.** Shipping a TOML file per OAuth
      service does not scale, so the builder writes them from the service's own
      docs and they live in the database, not the code tree. Shared through the
      sync store so one install's research is not repeated. Bloom ships none at
      all, so every provider is editable — endpoints must be public https, no
      DELETE, and a wrong or incomplete one is fixed at `PUT /admin/manifests/{name}`
      or by asking, rather than in an editor and a deploy
- [x] Editing what it built — `POST /admin/builder/edit` and the `edit_agent` tool,
      covering prompts, keywords, ceilings, attachments and OAuth scopes. A scope
      belongs to the connection, so "let it skip tracks" is an edit, not a rebuild —
      a rebuilt agent would inherit the same grant. The builder still cannot edit
      itself
- [x] Model keywords (`app/models.py`) — the ecosystem's ten, pulled from the shared
      sync store, so `coding` means the same thing here as it does to Amber
- [x] [docs/aperture-integration.md](docs/aperture-integration.md) + `docs/openapi.json`

Not done, and deliberately: **deploying it.** That is a change to `amber-infra` —
a `bloom/docker-compose.prod.yml`, an `apps.bloom` stanza in `secrets.yaml`, and
`install.sh --app bloom --domain bloom.johnny.dev --upstream 127.0.0.1:8010`.

## Adding a provider

**Usually you don't — you ask.** Tell the builder to make an agent for a service and
it writes the provider manifest itself, from that service's own documentation, and
stores it in the database. Shipping a TOML file per OAuth service was the last thing
here that made adding a capability a pull request and a redeploy, which is exactly
what Bloom exists to stop. If it gets an operation wrong, fix it at
`PUT /admin/manifests/{name}` — a form in Aperture, not an editor and a deploy.

**An incomplete manifest is the same conversation.** "Add skip to Spotify" is an
edit: the builder reads the current TOML, adds the operations, and writes the whole
document back under the same name. Every agent using that provider has the new tools
on its next run — a rebuilt agent would not, because tools come from the provider,
not from the agent.

The rules a written manifest must pass, on top of everything below: endpoints must be
public HTTPS (a manifest naming `169.254.169.254` would aim your live credential at
the cloud metadata service), no `DELETE` operations, at most 20 operations and 16 KB.
**Bloom ships no manifests**, so there is no name you cannot redefine and no provider
whose gaps need a release — see
[docs/provider-manifests-future.md](docs/provider-manifests-future.md) for why the
two that used to ship were removed. Manifests are shared through the sync store, so
one install's research is not repeated on the next.

**Writing one by hand** is a TOML document, not code. Copy either worked example in
[tests/fixtures/](tests/fixtures/) — `spotify.toml` (OAuth) or `github.toml` (which
also accepts a pasted key) — say which kinds of credential it takes with `auth`, and
declare one `[[operations]]` block per thing an agent should be able to do. Each
becomes a tool named `<provider>_<operation>` whose `description` is the only thing
the model sees — so write it for the model, naming the fields it should read out of
the response. Load it with `PUT /admin/manifests/{name}`, or point
`BLOOM_MANIFEST_SEED_DIR` at the directory holding it.

The loader refuses a manifest that would break something far away: a tool name over
40 characters or containing `__` (which would collide with MCP's `server__tool`
namespacing), an operation with no description, a parameter in a header, or a
parameter named `authorization`/`token`/`access_token`/`api_key`/`cookie`. That last
one matters most: a tool argument ends up in the model's context, in the runtime's
log, and in Bloom's own trace, so a credential must never be settable as one.

Tokens are never held by the tools either. Each closes over a *connection id* and
asks for a live token when it fires, which is what lets a token expire mid-run and
be refreshed without the task failing.

**One TOML trap worth naming, because it bit this repo.** Every bare key must appear
*above* the first `[table]` header. TOML assigns a key to the most recently opened
table, so a `scopes_default` written below `[probe]` silently becomes
`probe.scopes_default` — the manifest still loads, and the provider quietly gets no
scopes. `spotify.toml` shipped that way, and `operations_for` was hiding `play`,
`pause` and `now_playing` as unauthorised, leaving Spotify with only `search`. Keep
`[probe]` and `[[operations]]` last.

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
