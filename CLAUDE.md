# CLAUDE.md — The Amber Ecosystem

The section above the `---` is the ecosystem-wide reference, shared verbatim
across every repo. Keep it in sync rather than editing it locally. Everything
below the `---` describes **this** repo and is yours to rewrite.

## What this is

A personal, open-source ecosystem of independent apps (finance, school, project
tracking, FreeCallMe's dashboard, etc.), each usable completely standalone, that also
expose themselves to a personal AI layer via MCP. **Amber** is the orchestrating
voice/text agent that knows Johnny and can query or act across every connected app.
**Aperture** is the unifying Electron shell that ties the apps together visually and
manages device config/sync. Every app can be cloned and run alone by anyone — the
agent layer is always an opt-in extension, never a dependency.

## Core principle (do not violate)

**Every app must run standalone with zero knowledge the ecosystem exists.** If `git
clone`-ing a single app and running it requires anything from another repo, that's a
bug in the design. The agent/MCP layer is always optional, toggled by a feature flag,
off by default.

## The reframe that resolved most early confusion

Two different things were being conflated: **the mechanism** that lets an LLM call
tools and loop (build once, shared) vs. **the specific tools/data each app exposes**
(unique per app, but standardized via protocol). MCP is the shared protocol.
`agent-runtime` is the shared mechanism (a library). Every app just needs to expose
*its own* data/actions as MCP tools and resources — it does not need its own copy of
the loop logic.

## Naming reference

| Name | What it is |
|---|---|
| **Amber** | Personal orchestrating agent — voice pipeline, memory, backend-only, no frontend |
| **Aperture** | Electron shell app — unifying UI, device-local config store, import/export/sync |
| **Bloom** | Service wrapping `agent-runtime` — where reusable agent configurations are defined and run. Was called `agent-spawner`; **this repo**. |
| **notification-relay** | Push notification fan-out (Redis pub/sub → APNs → iOS) |
| **finance-agent, school-agent, outpost, freecallme, etc.** | Individual domain apps, each with their own frontend + MCP server |

Naming style for future apps: single clean nouns, consistent with
Outpost/ThinkTank/Aperture (Forge, Sentinel, Herald, Atlas, etc. — check for
collisions before assigning).

## Repo list

- `amber` — the agent itself
- `agent-mcp-py` — shared library: wraps the MCP Python SDK with auth, depth-guard,
  usage logging, sync-store registration. Every Python app's MCP server is built on this.
- `agent-runtime` — shared library: the actual agentic loop (call model → tool call →
  execute → repeat), built on OpenRouter's OpenAI-compatible endpoint. Imported
  directly (not called over network) by Amber and Bloom.
- `bloom` — service, imports `agent-runtime` in-process, exposes task delegation as
  an MCP tool (`run_task`) for apps that don't want to embed the runtime
  themselves. Formerly `agent-spawner`; **this repo**.
- `notification-relay` — service, Redis pub/sub, single `send_notification` endpoint
  (also exposed as an MCP tool).
- `amber-infra` — deployment backbone: Caddy config, install script, backup scripts,
  CI templates, the hosted config sync store.
- `amber-template` — scaffold repo, pre-wired with `agent-mcp-py`, Docker, CI,
  backups. **This repo is a clone of it.**
- `Aperture` — Electron shell.
- Individual app repos (`finance-agent`, `outpost`, etc.) — each standalone, each
  optionally MCP-enabled.
- `freecallme` — existing Next.js/Vercel/Supabase app, **not rewritten**; gets a small
  TypeScript MCP sidecar added.

## Tech stack decisions

- **New backend-heavy apps default to Python (FastAPI)** — this is what lets them
  share `agent-mcp-py` and `agent-runtime` with Amber and the spawner.
- **Frontends default to Next.js (React)** for anything with a dashboard.
- **Existing apps are not rewritten to match the pattern.** FreeCallMe stays Next.js;
  it gets an MCP server written in TypeScript (`@modelcontextprotocol/sdk`) as a
  sidecar, not a port to Python. MCP is the interop layer specifically so language
  doesn't have to match everywhere — only the protocol does.
- **Aperture** is Electron + React, frontend-only, not itself an MCP server.
- **Registry / service discovery** is not a static YAML file — it's a small hosted sync
  store that Aperture edits through a UI and every headless agent reads directly.
  Aperture's on-device storage is a cache; it is never the only copy, since headless
  services must work even when Aperture isn't open.

## Conventions every app must follow

- **Resource URIs mirror real dashboard views.** If a screen shows data, there's a
  matching MCP resource returning the same data (e.g. `finance://transactions/recent`).
  No separate "agent-only" version of the data.
- **Tools mirror real user actions.** If a human can click it, there's a tool that does
  the same thing, calling the same underlying function as the UI — not a parallel code
  path that can drift.
- **Query tools are marked `read_only=True`.** Action tools that are risky get
  `requires_confirmation=True`, gated by an `X-Confirmed` header set only after
  explicit approval.
- **Conversation depth is tracked via `X-Conversation-Id` and `X-Agent-Depth`
  headers**, capped at 5 hops, to prevent agent-to-agent call loops.
  `call_peer()` handles threading these automatically for any server-to-server
  composition.
- **External integrations (Stripe, PostHog, etc.) are composed, not re-implemented.** An
  app's own MCP server acts as a client to third-party MCP servers internally and
  re-exposes clean, domain-specific tools on top.
- **Usage and cost logging stay local to each app's own DB** — no shared central
  database, consistent with the independence principle. The spawner aggregates cost
  views by querying each app's own usage summary, not a shared table.
- **Model selection uses named tiers** (`cheap` / `balanced` / `strong`) resolved
  through `agent_runtime.model_router`, never hardcoded model strings scattered across
  apps.

## Deployment

**Target state:** two OVH servers. Server A (core/always-on): Amber, Bloom,
notification-relay, config sync store, Caddy. Server B (apps): individual app agents,
also behind Caddy. Every subdomain (`amber.johnny.dev`, `finance.johnny.dev`, etc.)
routes via one Caddy instance per server with per-app config snippets. Docker Compose
per app, pinned image tags (never `latest`), Watchtower for auto-updates initially,
migrating core services to GitHub Actions webhook deploys once stable.

---

# This repo: Bloom

> **Naming note.** The ecosystem docs called this `agent-spawner`. It is Bloom
> everywhere now — package, `BLOOM_` prefix, MCP `app_name`, `bloom.johnny.dev`.
> The shared half above has been corrected here; `amber_v2/CLAUDE.md` and
> `amber-template/app/agent_client.py` (which calls a peer named `spawner`) still
> need the same edit.

## What this app is

The place you **define an agent** instead of building an app for it.

An agent here is a row: a system prompt, a model tier, and the **connections** it
may act through. Amber delegates a task to one over MCP; Aperture creates and edits
them over REST. Adding "Bloom can control Spotify" is a config plus a TOML manifest,
not a repo, a container, a subdomain and a deploy.

It is **not** a general task queue, not a router that guesses which agent to use
(the caller names one — auto-routing is a v2 idea worth having only once there are
enough configs to make guessing better than asking), and not a UI.

## Two surfaces, two audiences

| | | |
|---|---|---|
| `/mcp` | `run_task`, `build_agent`, `edit_agent`, `list_agents`, `bloom://agents`, `bloom://builds` | Other agents. MCP, because a model must discover and choose. |
| `/admin/*` | config CRUD, test-run, run history + trace, connections, builds, provider manifests, keywords | Aperture. REST, because a GUI wants OpenAPI and a generated client. |

`BLOOM_MCP_KEYS` and `BLOOM_ADMIN_KEYS` are **separate key sets**: Aperture edits
configuration, a peer agent spends money, and one leaked token must not buy both.

## Architecture, and the decisions worth not re-litigating

### One config prefix, three consumers

`app/config.py` is the only `.env` surface. `agent_mcp` and `agent_runtime` would
each read their own `AGENT_MCP_*` / `AGENT_RUNTIME_*` environment; both are built
by injection with `_env_file=None` instead (`app/mcp.py`, `app/runtime_service.py`).
This is not tidiness — `agent_runtime.Settings` defaults `db_path` to
`agent_runtime.db` in the working directory, so a second surface means cost rows in
a file nobody reads and an empty join between spend and what caused it.

### One SQLite file, three writers

`bloom.db` holds Bloom's tables plus `agent_mcp_usage` and `agent_runtime_usage`,
written by the libraries through their own connections. They share
`conversation_id` / `app_name` / `depth` / `created_at`, which is the point. It is
also why WAL and a busy timeout are mandatory (WAL removes reader/writer blocking,
not writer/writer) and why the trace writer must stay off the model loop's thread.

Raw `sqlite3`, house pattern (`agent-mcp-py/CLAUDE.md:88`): module-level `_SCHEMA`
of `CREATE TABLE IF NOT EXISTS`, one connection under a `threading.Lock`, rows out
as dicts, ISO-8601 UTC-seconds TEXT, sync methods wrapped in `asyncio.to_thread` at
the call site. No ORM, no Postgres — the build spec said Postgres and SQLAlchemy;
every other service in the ecosystem does this, and `backup-sqlite.sh` already
exists.

**There is a migration runner** (`bloom_schema_version` + an ordered `_MIGRATIONS`
tuple, applied in `Store.__init__`), and the reason is worth keeping. 0.2.0 renamed
`oauth_connections` → `connections` and dropped `mcp_servers_json`, and shipped a
guard that *refused* any older file on the reasoning that "Bloom has never been
deployed". It had been — 0.1.0 was live on the box — and since `CREATE TABLE IF NOT
EXISTS` leaves an old file untouched, every existing install crash-looped at boot
with `unhealthy` as the only symptom. The version lives in a table rather than
`PRAGMA user_version` because that pragma is one file-wide slot and this file has
three tenants. Add migrations by appending; never renumber, and every one must be a
no-op against a fresh database, because a fresh file and a pre-versioning file both
report 0.

### Execution — `app/runtime_service.py`

`execute_run` is the **single** path; `run_task` and `test-run` differ only in
`origin` and `caller`. Three things there are load-bearing:

- **Bloom owns broker teardown.** `AgentRunner` closes a broker only when it built
  one itself; passing `broker=` makes its `finally` dead code, so an `MCPClient`'s
  session stack and `httpx2` client leak per run. Hence `(runner, aclose)`.
- **Stop conditions are always passed explicitly.** Any non-empty list *replaces*
  the runtime's default `StopOnSteps`, and hitting the hard cap instead forces an
  extra completion.
- **Ceilings clamp downwards only.** A delegated task is unattended; the config may
  lower `max_steps`/`max_cost_usd`, never raise them.

Broker order is priority order: credential tools, then peers. A peer must never be
able to shadow a tool carrying the user's own account access.

### The run trace — `app/trace.py`

Neither library has a step hook (`agent_mcp` has none at all; `agent_runtime` has
only `on_sentence`), so the trace is built from three seams. **Live:** assistant
text at sentence granularity, and tool start/finish via `TracingBroker`. **Not
live:** per-step tokens and cost, which `RunResult.steps` only yields at the end.
Token-level deltas are deliberately unavailable — they exist only via `stream()`,
which discards run state and records no usage rows.

`run_events` is append-only with an autoincrement id that *is* the SSE cursor, so a
client resumes on `Last-Event-ID` and a crashed process still leaves a readable
partial trace. Every event is queued and drained by one background task; a full
queue drops events rather than stalling the run it describes.

**The terminal `run_finished` event is the only thing that ends a stream**, so it is
emitted on every path out including cancellation, and the startup sweep writes one
for every run a dead process left `running`.

### Connections — one vocabulary for what an agent can reach

There used to be two, and both were half-built. An agent's reach was an
``mcp_servers`` list of bare peer names on the config row *plus* a set of OAuth
connections owned by that config — so a peer could not carry a credential, an API
key was not expressible at all, and a connection could not be shared even though the
schema had a binding table and a ``shared`` flag: the exchange stamped an owner
every time, so nothing could ever set it.

A **connection** is now a first-class row in a global library, with a `kind` of
`oauth`, `api_key` or `mcp`. Creating one from an agent's page attaches it; the row
belongs to nobody, any other agent can attach the same one, and **deleting an agent
deletes none of them** — the exact inversion of what `delete_config` used to do, and
the property `tests/test_connections.py` guards first.

Three things this bought, none of which needed new machinery:

* **`api_key` reuses the provider manifests.** `register_operations` already built
  tools that ask a resolver for a credential at call time and never close over one;
  only where the secret comes from differs. A manifest says which it accepts with
  `auth = ["oauth", "api_key"]`, and `[api_key]` says where the key goes.
* **A peer is just a connection with a URL.** `MCPClient` takes a plain
  `{name: {base_url, token}}` resolver mapping, so `_peer_resolver` bypasses
  `agent_mcp.registry` entirely. `_known_peers` is gone; discovery survives only as
  prefill in `/admin/connections/kinds`. A connection's `name` is the tool namespace
  (`<name>__<tool>`), which is why it is validated like a manifest's provider name.
* **Provider client credentials moved onto the connection.** They were process
  environment, which made connecting Spotify a deployment operation — write
  `secrets.yaml` over SSH, reconcile, restart the container — and required a whole
  panel in Aperture's *Servers* tab to do it from a GUI. `client_id` lives in the
  connection's config and `client_secret` in its own encrypted column;
  `registry.client_for` resolves connection-first, environment-second, so an
  existing deployment keeps working and a shared registration is still possible.
  That panel, its `dynamic_keys` manifest stanza and the `fillCredentials` SSH
  action are all deleted. `POST /admin/connections/{id}/secret` takes `client_id`
  alongside `client_secret`, so a connection **Bloom** created — every one the
  builder makes — can be given an app registration afterwards. Create-time-only was
  the half of this that was still a deployment operation, and it had no symptom
  beyond `/oauth/start` naming an environment variable.

The tradeoff worth knowing: an app client secret now sits in `bloom.db`, under the
same Fernet key that already protects the user's access *and refresh* tokens for
that account. The refresh token is the more dangerous of the two, so this is not a
new class of secret in the file.

Note the asymmetry in `build_runner`: the credential broker is gated on
`connections_enabled`, `_peer_resolver` is not. A tokenless peer on a trusted
network needs no encryption key — there is no secret to store badly.

**Broker order is still priority order**, and still load-bearing: credential tools
first, peers last, so a peer can never shadow a tool carrying the user's own account
access. `tests/test_peers.py` asserts it explicitly.

### Credentials — `app/providers/`, `app/credentials.py`, `app/crypto.py`

A provider is a TOML manifest declaring operations; each becomes a tool named
`<provider>_<operation>`. Rejected at load: a name over 40 chars or containing `__`
(collides with MCP's `server__tool`), an operation with no description (it is the
only thing the model sees), a header parameter, or a parameter named
`authorization`/`token`/`access_token`/`api_key`/`cookie`.

That denylist is the load-bearing one. A tool argument lands in `Step.tool_calls`,
is replayed into the next request, is logged at INFO by the runtime, and is
persisted in Bloom's trace — a credential must never be settable as one.

Tokens are **not** captured in closures either: each tool holds a *connection id*
and asks `CredentialResolver.credential()` at call time — which returns a
`Credential` with the secret already placed in the header or query parameter that
provider wants, so nothing downstream branches on where it came from. A token
expiring mid-run is refreshed rather than fatal. Refresh is serialised per connection with a re-read inside the
lock, because for a provider with rotating refresh tokens losing that race
permanently breaks the grant. That is correct single-process only; multi-worker
would need a DB row claim.

`MultiFernet` from day one — head encrypts, all decrypt — so rotation is an endpoint
rather than a migration. A missing key is fatal at startup *only if* connections are
already stored.

### OAuth

The flow belongs to a **connection**, not to whichever agent started it — that is
what makes one approval attachable to a second agent. `oauth_states.connection_id`
carries it, and `status='pending'` finally means something (it was in the enum from
day one and nothing ever wrote it).

Bloom hosts the callback because Aperture, a desktop app, has no public URL. The
callback is the **one unauthenticated route** in the service and cannot be
otherwise: a provider redirects a browser, which carries no bearer. Its security is
a single-use `state` row plus PKCE. It lives on its own router
(`app/admin/oauth_callback.py: public_router`) — do not add a second route there.

The completion page fires `aperture://oauth-complete?provider=…&status=…` *and*
renders readable text, because Aperture does not register that scheme yet. Both, so
the flow works today and improves with no server change.

### The builder — `app/builder/`

The place a *description* becomes a configuration. "Create a Spotify agent" arrives
from Aperture (`POST /admin/builder/build`) or from Amber (the `build_agent` MCP
tool), and comes back as a real agent, its connections, and the list of things a
human still has to do.

**It edits as well as creates, and that is not a convenience.** "Let the Spotify
agent skip tracks" used to be unanswerable: the builder could only create, so the
only offer available was a second agent — which cannot work, because a new agent
attached to the same connection inherits the same OAuth grant. A permission is a
property of the *connection*, not of the agent. `POST /admin/builder/edit` and the
`edit_agent` MCP tool take a slug and a plain-language change; `bloom_get_agent`,
`bloom_update_agent`, `bloom_detach_connection`, `bloom_set_connection_scopes` and
`bloom_authorize_connection` are what the builder does it with. Both the tool
descriptions and the prompt name the rebuild as *the wrong move*, rather than merely
offering the right one — it is what a model reaches for unprompted.

An edit is a `builds` row with `mode='edit'` (schema v2), so it reuses the trace,
the SSE stream and the checklist unchanged. Two things differ, both in
`app/builder/service.py`:

* **it is settled from `changes`, not from `agent_config_id`.** A build is judged by
  whether an agent exists afterwards; an edit cannot be, because it existed
  beforehand. Each write tool appends a line, and an edit that appended none is
  `failed` however cheerfully the model summarised it;
* **its checklist counts toward the status.** A build's connections are freshly
  created and therefore `pending`, so `_all_active` settles it correctly. An edit's
  are usually already `active` and *stay* active after a scope change — a token
  keeps the grant it was issued with — so an outstanding non-`manual` step is what
  makes it `needs_setup`. Downgrading a re-scoped connection to `pending` instead
  would strip every one of that agent's tools until someone came back: breaking a
  working agent to signal that it is about to become more capable.

**The builder cannot edit the builder.** `_reject_builder` refuses `BUILDER_SLUG` on
every write path in `app/builder/tools.py`, and `resolve_edit_target` refuses it
before a run starts. That was implicit while the builder could only create — the
`UNIQUE` index refuses a second row with that slug — and stopped being implicit the
moment it could write to an existing one. It is now the fourth lock on a model
rewriting its own instructions, and the one that would otherwise have gone missing.
There is also no slug-rename parameter: a slug is how `run_task` names an agent and
how every past build refers to it, so a model rebinding one breaks callers nowhere
near the run. Aperture's `PATCH /admin/agents/{id}` still allows it.

**It is a seeded row, and privilege is its slug.** `is_builder(config)` — one line
in `app/builder/agent.py` — is the only gate on the tools that write configurations.
`build_runner` registers `builder_broker` *first* when that predicate holds and never
otherwise, so nothing an operator can attach produces them: they are not derived from
`connections` at all. Three things must fail for a normal agent to reach them: the
`UNIQUE` index on `agent_configs.slug`, the reserved-slug validators in
`app/admin/agents.py` (create *and* patch), and `ensure_builder_config` running in
the lifespan before anything serves. `tests/test_builder_run.py` asserts the
*absence* — an intersection of a normal agent's broker with `TOOL_NAMES` — because
that is the direction that stays true only if somebody keeps checking.

A row rather than a sentinel because `runs.agent_config_id` is real: a synthetic id
would give every build a `null` slug in the activity feed and 404 the trace endpoint,
which is the endpoint the whole feature reuses for free.

**MCP-first is a procedure, not a preference.** `app/builder/mcp_registry.py` queries
the official registry and computes a `usable` verdict, because Bloom reaches a peer
through `MCPClient` — streamable-HTTP, `Authorization: Bearer`, nothing else. A
package-only entry (npx/stdio), an `sse` remote, or one wanting `X-API-Key` is
*unreachable*, and a model told merely to "prefer official servers" attaches one and
reports success. The reason string names which, so the model stops rather than
retries. Trust is deliberately not computed: anyone can publish there — searching
"spotify" returns third-party trend-data servers — so the prompt tells it to judge,
and the real protection is that nothing works until a human attaches a credential.

**Everything it creates is inert, structurally.** No authoring tool has a parameter a
secret could go in, so no credential can reach `Step.tool_calls`, the runtime's INFO
log, or the trace — `tests/test_builder_tools.py` asserts that against the registered
JSON schema, because the thing that would break it is somebody later adding a helpful
`secret=`. Connections are created `pending`, including peers, overriding the
"tokenless means active" default that is right for a URL a human typed and wrong for
one found in a public registry.

**Two ceilings.** A build is 12–25 steps against a service default of 8, so
`build_runner` selects `builder_max_steps`/`builder_max_cost_usd` by `is_builder`.
The invariant is unchanged — a config may lower a ceiling, never raise it — there are
simply two, and which applies is a property of the agent. `execute_run` gained
`timeout_s` for the same reason.

**When it cannot wire something, it fails and creates nothing.** Now only when the
API is genuinely undocumented — a manifest written from guesses is worse than none,
because it looks finished and fails later with a credential attached.

### Provider manifests are written at runtime — `app/manifests.py`

**Shipping a manifest per OAuth service does not scale, so the builder writes them.**
That was the last thing in Bloom that made adding a capability a pull request and a
redeploy, which contradicts the whole premise — a capability is a row in a table. The
builder researches the API with `read_url`, calls `bloom_list_manifest_format`, and
writes with `bloom_write_provider_manifest`; the manifest is live in the same run, so
it can create the connection immediately rather than reporting success and waiting
for a restart. **Nothing ships.** `app/providers/` holds the loader and no manifests
at all; `providers()` is stored rows and only stored rows.

That last part changed after a failure worth keeping. `spotify.toml` and `github.toml`
used to stay as reviewed reference implementations that beat any row of the same name,
which sounded conservative and was the opposite: the two services most likely to be
connected already were the two whose gaps could not be repaired. Spotify shipped
without a `next` operation, so "skip this song" was unanswerable — the agent ran,
called nothing, and reported success, while the user's own OAuth grant already carried
`user-modify-playback-state`. The capability was there; only the definition was
missing, and the definition was the one part nobody could reach without a pull request.
The reference the builder needs is `app/builder/manifest_format.py`, which teaches the
*shape* without deciding what exists. The two files live on as test fixtures in
`tests/fixtures/`.

**Extending a manifest is the normal fix for a missing capability**, and the builder
is pushed hard toward it: `bloom_get_provider_manifest` returns the current TOML,
`bloom_write_provider_manifest` takes the whole document back under the same name, and
both the tool descriptions and the prompt say that a write *replaces* rather than
merges. A rebuilt agent never adds a tool — the tools come from the provider, resolved
per run in `runtime_service.py` — so an agent picks up new operations on its next run
with no rebuild and no reconnect.

`BLOOM_MANIFEST_SEED_DIR` (unset by default) imports `*.toml` from a directory as
ordinary rows: never overwriting a name that already exists, and producing something
editable exactly like one the builder wrote. It is how the tests get their two worked
examples, and how an operator restores an export — deliberately not a tier.

**A manifest is not inert data**, which is the whole difficulty:
`register_operations` turns each entry into a callable tool and `CredentialResolver`
attaches a live token to every request it makes. Four things pay for the trade, and
[docs/provider-manifests-future.md](docs/provider-manifests-future.md) prices each:

1. `load_manifest_text(trusted=False)` — endpoints must be https and pass the SSRF
   check `read_url` uses, no `DELETE`, ≤20 operations, ≤16 KB. The metadata-service
   case is the one that matters: without it, a manifest naming `169.254.169.254`
   turns the credential resolver into an authenticated client of the instance's own
   identity service;
2. **an existing local row always wins** over anything pulled from the sync store,
   which is what stops somebody else's model repointing a provider you already have
   a credential attached to — the attack needing no new credential at all. This
   replaced a rule that let two shipped files outrank every row; that version also
   stopped *you* fixing your own manifest, which is how a Spotify agent ended up
   unable to skip a track with the right scope already granted;
3. **the credential is the real gate**, and it always was — a manifest does nothing
   until a human attaches an account. What that human lacked was the one checkable
   fact, which `ConnectionOut.credential_hosts` now carries: "your key will be sent to
   api.example.com" is something they can compare against the service they meant;
4. **verification** — validation proves a manifest parses, never that its `api_base`
   is real. A successful `POST /connections/{id}/test` is the only evidence there is,
   and it marks the manifest verified. Anything unverified says so.

**`PUT /admin/manifests/{name}` is the acceptance criterion, not a convenience.** A
model-authored operation will sometimes be wrong, and if fixing it meant editing TOML
in the repo, this would have traded "open the editor to add a provider" for "open the
editor to fix one" — the same editor. Correcting one is a form. `DELETE` leaves
connections and their credentials alone, so "delete it and let the builder retry" is
recovery rather than destruction.

**`allow_request` is the honest way out of a hard API.** A manifest may declare a
bounded `<provider>_request` tool — `safe_path` locks it to `api_base`, no `DELETE` —
for services too irregular to model as operations. `app/providers/registry.py` rejects
a generic request tool in its own docstring, and that reasoning assumed the
alternative was a *human*-written manifest; when both are model-written the choice
becomes "guessing at authoring time, frozen" versus "guessing at call time,
correctable". Declared operations are still better and still preferred. This exists so
an irregular API produces a working agent instead of four operations that all 400.

Manifests travel through the sync store's `/manifests` (`app/manifest_sync.py`), so
research done on one install is not repeated on the next. The store treats the TOML as
opaque and does not parse it, so a pulled manifest is untrusted input and goes through
`trusted=False` like any other. **Local always wins** — a pull never overwrites what
this install has, which is what keeps the `PUT` above trustworthy.

### Model keywords — `app/models.py`

`agent_runtime.model_router` offers three names, and a ladder can only say "better";
it cannot say "for code". Bloom ships the ecosystem's ten keywords (Amber's
`app/models.py` is the other half) and overlays the shared table from
`GET {sync_store_url}/models`. **Pull-only** — Amber owns editing, via Aperture.
`build_runner` resolves the keyword to a concrete id *before* `AgentRunner` sees it,
because the router would raise `UnknownTier` on `coding`. Resolution never raises: an
unreachable store keeps the table it had rather than silently re-pointing everything.

## The promise this repo does not make

Every other app asserts that with the MCP flag off it never *imports* the agent
stack. Bloom cannot: the admin API imports `agent_mcp.auth` and
`agent_runtime.model_router`. `tests/test_mcp.py` pins the true, weaker guarantee —
no `/mcp` routes, `app.mcp` never imported, admin API serving normally.

## Commands

```bash
python -m venv .venv && .venv/Scripts/activate    # .venv/bin/activate on POSIX
pip install -e ".[dev]"
cp .env.example .env                              # BLOOM_ADMIN_KEYS at minimum

pytest -q                                         # 342 tests, no network, nothing to start
ruff check . && ruff format --check .
make openapi                                      # docs/openapi.json; CI fails if stale
uvicorn app.main:app --reload --port 8010
```

The builder additionally needs `BLOOM_OPENROUTER_API_KEY` and a Tavily
`BLOOM_SEARCH_API_KEY`; without either, `POST /admin/builder/build` and
`/admin/builder/edit` answer 503 naming which is missing rather than letting a model
write an integration from memory. `bloom_authorize_connection` further needs
`BLOOM_FEATURE_OAUTH` and `BLOOM_FERNET_KEYS`, and says so as a checklist step
instead of minting a link it could not store the resulting token for.

Key modules: [app/config.py](app/config.py) (every knob),
[app/db.py](app/db.py) (schema + store), [app/runtime_service.py](app/runtime_service.py)
(config → runner → run), [app/trace.py](app/trace.py) (the trace seams),
[app/mcp.py](app/mcp.py) (`run_task`, `build_agent`, `edit_agent`),
[app/providers/registry.py](app/providers/registry.py) (manifests → tools),
[app/manifests.py](app/manifests.py) (manifests written at runtime) and
[app/manifest_sync.py](app/manifest_sync.py) (sharing them),
[app/admin/connections.py](app/admin/connections.py) (the library),
[app/credentials.py](app/credentials.py) (live credentials),
[app/builder/](app/builder/) (the agent that writes agents — `prompt.py` is its
instructions, `tools.py` what it may write), [app/models.py](app/models.py) (model
keywords), [docs/aperture-integration.md](docs/aperture-integration.md) (the GUI
contract).

## Not done

**Deployment.** It belongs in `amber-infra`: a `bloom/docker-compose.prod.yml`
modelled on `sync-store/`'s, an `apps.bloom` stanza in `secrets.yaml` (Server A),
and `install.sh --app bloom --domain bloom.johnny.dev --upstream 127.0.0.1:8010`.
The generic `_template.caddy` already imports `streaming`, which both `/mcp` and the
SSE trace need.

**Auto-routing** (`run_task` picking the agent itself) — not until there are enough
configs that guessing beats asking. The builder makes this more likely to be worth
doing, since configs are now cheap to create.

**Reaching a service with no MCP server and no manifest.** The builder stops and says
so. [docs/provider-manifests-future.md](docs/provider-manifests-future.md) is the
written-down version of the three ways out and what each costs; the short version is
that a model authoring the file which defines HTTP calls made with your credentials
wants a review gate, and the review gate wants a UI that does not exist yet.

**A diff view for an edit.** Editing itself is done (see above); what an edit does
not have is a *preview*. It writes, then reports. `builds.changes` is the record
after the fact, which is enough to see what happened and not enough to approve it
first — and approval needs the same missing `X-Confirmed` channel as everything
else below.

**Multi-turn clarification.** The builder gets one brief and must commit, whether
building or editing, because resuming would need conversation continuity
`execute_run` does not offer. An ambiguous edit brief is answered by guessing, which
is why `changes` exists and why the prompt tells it not to touch what was not asked
about.

**`requires_confirmation` on `run_task`.** It is the strongest candidate in the
ecosystem for a confirmation gate and is still unmarked: the gate is satisfied only
by an inbound `X-Confirmed: true`, and no caller can produce one — Amber's wire
protocol has no tool-approval frame and Aperture has no HTTP client. Marking it
today would make Bloom permanently uncallable rather than safely gated. The per-config
step and cost ceilings are the control that binds now. Revisit when that frame lands.
