# Bloom ↔ Aperture integration contract

What Bloom's management API offers, and what Aperture has to build to use it.

## Read this first: the Aperture side is built

This document used to describe work to be done. It no longer does — Aperture has the
whole surface, and this is now a description of a contract both ends implement. What
exists there:

- `src/main/bloom/` — `client.ts` (a `fetch` client over the global, no new
  dependency), `wire.ts` (the SSE parser and the snake_case row mappers),
  `run-stream.ts` (live tailing with `Last-Event-ID` resumption), `link.ts`,
  `discover.ts` (finds Bloom over SSH and reads its admin key), `token-store.ts`
  (Electron `safeStorage`), `deep-link.ts` (the `aperture://` handler).
- `src/renderer/bloom/` — the Bloom tab: `AgentList`, `AgentEditor`, `Connections`,
  `BuildAgent`, `SetupChecklist`, `TestRun`, `TraceView`, `RunHistory`, `Usage`.
- `src/shared/bloom.ts` — the typed vocabulary, mirroring `docs/openapi.json`. If the
  two disagree, that file is wrong.

The one thing still worth knowing: the OAuth completion page fires the deep link
*and* renders "you can close this tab", so the flow degrades gracefully if the
protocol registration ever fails.

## The builder

```
POST   /admin/builder/build            {brief}  202 → {build_id, run_id, mode, stream_url, trace_url}
POST   /admin/builder/edit  {agent_slug, change} 202 → same shape, mode='edit'
GET    /admin/builds                   ?limit&offset&status&mode → Build[]
GET    /admin/builds/{build_id}                  200 → Build
POST   /admin/builds/{build_id}/steps/{index}/done  200 → Build
DELETE /admin/builds/{build_id}                  204
GET    /admin/models/keywords                    200 → {keywords[], shared, default}
```

**A build is a run**, and that is the whole integration story: `run_id` points at the
existing `/admin/runs/{id}/events` stream and the existing trace renderer, so the
builder needed no new transport in Aperture at all. `POST` answers 202 immediately;
read the outcome from `/admin/builds/{id}` once the run's terminal event arrives.

**An edit is a build with `mode='edit'`** — same row, same stream, same trace, same
checklist, so it reuses the build panel rather than needing a second one. Put it on
the agent detail page as "change this agent" taking a sentence; `?mode=edit` filtered
to that slug is the agent's change history. Two differences worth rendering:

* **`changes[]` is what actually happened**, one line per write. It is empty for a
  build, whose evidence is the agent it left behind, and an edit with an empty
  `changes` is always `failed` — show that list, not the summary, when answering
  "did it work".
* **`needs_setup` after an edit usually means re-consent.** Widening an OAuth scope
  records the scope but cannot grant it; the stored token keeps the grant it was
  issued with until someone approves the new one. The connection stays `active`
  throughout — deliberately, so the agent keeps the tools it already had — so the
  outstanding `connect_oauth` step is the only signal, and a UI that reports
  "updated" without surfacing it tells the user a permission is live when it is not.

`Build.status` is `running | needs_setup | ready | failed`. **`failed` is sometimes
the correct answer** — no usable MCP server and no shipped manifest means the builder
reports what it found and creates nothing, and `summary` is where the reason lives.

`checklist[]` steps are typed so a client renders each as the control that completes
it: `connect_oauth` → the Connect button, `paste_api_key` → the secret box,
`register_oauth_app`/`set_env` → text plus a link, `manual` → text. **An unknown
`kind` must be coerced to `manual`, never dropped** — Bloom's `normalise_step` and
Aperture's `toSetupStep` do exactly this, so the two ends cannot disagree about what
a stored checklist means.

`503 unavailable` from `/builder/build` or `/builder/edit` means the builder is not
configured, and the message names which of `BLOOM_FEATURE_BUILDER`,
`BLOOM_OPENROUTER_API_KEY` or `BLOOM_SEARCH_API_KEY` is missing. Show it as a setup
prompt, not an error. `404 not_found` from `/builder/edit` is an unknown slug,
resolved before anything is spent.

`AgentConfig.builtin` marks Bloom's own builder row. Its slug and prompt are defined
in code and re-seeded at every boot, so Bloom answers `409` to an edit of either —
show them read-only, and do not offer "change this agent" on it at all: the builder
refuses to edit itself, at the route and again at each tool. The model keyword and
the ceilings are editable.

## Provider manifests

```
GET    /admin/manifests                  200 → Manifest[]
GET    /admin/manifests/{name}           200 → Manifest
PUT    /admin/manifests/{name}   {toml}  200 → Manifest   (422 with the parser's message)
DELETE /admin/manifests/{name}           204
```

**A provider is no longer necessarily code.** The builder writes manifests at runtime
from a service's own documentation, because shipping a TOML file per OAuth service
does not scale. `source` says which kind each one is: `file` (shipped, reviewed,
read-only), `stored` (written on this install), `shared` (pulled from the sync store).
`editable` is the flag to render from — it is false exactly for shipped files, which
should read as read-only rather than 404.

**`PUT` is the feature, not a CRUD nicety.** A model-authored operation will sometimes
have the wrong path or a scope string that does not exist, and the whole point of
writing manifests at runtime is lost if fixing one means editing a repository. Give it
a real editor: `toml` is the source of truth, `operations[]` is a flattened projection
for display. A `422` carries the parser's own message, which names what to change.
`DELETE` leaves connections and their credentials alone, so "delete it and let the
builder try again" is a safe recovery to offer.

**Two fields carry the trust story, and they belong next to the credential form** —
they are on `Connection`, not buried in a settings page:

| Field | Render as |
|---|---|
| `provider_reviewed` | false → "this definition was written by Bloom, not reviewed by a person" |
| `credential_hosts` | "your key will be sent to **api.example.com**" — the first entry is the API base, where it goes repeatedly and unattended |
| `verified_at` (on `Manifest`) | null → "unproven". Set by a successful `POST /connections/{id}/test`, which is the only evidence a manifest describes a real API |

A person cannot audit a 90-line TOML document before pasting a key. They *can* check
a hostname against the service they meant to connect, which is why that is the fact
surfaced. Show it at the moment the credential is entered — a warning anywhere else
is a warning nobody sees.

The `review_manifest` checklist step points here: `connection_name` carries the
provider name. It is not a gate and nothing blocks on it.

## Connection basics

| | |
|---|---|
| Base URL | user-entered, like `Settings.amberUrl` already is. There is no discovery for this. |
| Auth | `Authorization: Bearer <BLOOM_ADMIN_KEYS token>` on every `/admin` route except the OAuth callback |
| Errors | always `{"error": "<code>", "message": "<prose>"}` — never FastAPI's `{"detail"}` |
| Error codes | `bad_request` `unauthorized` `forbidden` `not_found` `conflict` `unprocessable` `unavailable` |
| Schema | `docs/openapi.json`, checked in and CI-verified current. Generate a client from it. |

**The admin key is not the MCP key.** `BLOOM_ADMIN_KEYS` and `BLOOM_MCP_KEYS` are
separate on purpose: Aperture edits configuration, a peer agent spends money, and
one leaked token should not buy both. Aperture must never be given an MCP key.

## Agent builder — `/admin/agents`

```
POST   /admin/agents            create           201 → AgentConfig
GET    /admin/agents            list             200 → AgentConfig[]  (newest first)
GET    /admin/agents/{id}                        200 → AgentConfig
PATCH  /admin/agents/{id}       partial update   200 → AgentConfig
DELETE /admin/agents/{id}                        204
```

```jsonc
// AgentConfig
{
  "id": "842a050b9d35…",           // uuid4 hex, URL-safe unquoted
  "slug": "spotify-dj",            // ^[a-z][a-z0-9-]{0,63}$ — how a caller names it
  "name": "Spotify DJ",
  "system_prompt": "You pick music.",
  "model_tier": "balanced",        // cheap | balanced | strong, or a literal vendor/model
  "connections": ["…"],            // attached connection ids, in broker order
  "max_steps": null,               // null = use the service ceiling
  "max_cost_usd": null,
  "created_at": "…", "updated_at": "…"
}
```

Two validation behaviours worth building UI around:

- **`model_tier` is rejected at edit time** (422) if it is neither a known tier nor
  a literal `vendor/model`. Show the message; it names what was wrong.
- **Creation asks nothing about what the agent can reach.** `mcp_servers` is gone;
  capability is attached afterwards as connections. An unknown field is **refused**
  (`422` naming it) rather than ignored, so a stale client sending `mcp_servers`
  hears about it instead of silently creating an agent that reaches nothing.
- A duplicate `slug` is `409 conflict`.
- **Ceilings clamp, they never raise.** A config may lower `max_steps` /
  `max_cost_usd` below the service values; a higher number is silently clamped
  down at run time rather than rejected. Present them as "at most".

## Test-run panel

```
POST /admin/agents/{id}/test-run     {"input": "…"}   202 → {run_id, status, stream_url, trace_url}
GET  /admin/runs/{run_id}/events                      200 text/event-stream
POST /admin/runs/{run_id}/cancel                      202 → {run_id, status: "cancelling"}
GET  /admin/agents/{id}/runs/{run_id}/trace           200 → {run, events[]}
GET  /admin/agents/{id}/runs?limit=&offset=           200 → run[]
GET  /admin/runs?limit=&offset=&status=&origin=       200 → run[]   (every agent)
```

`stream_url` and `trace_url` come back as **relative paths** — join them to the base
URL rather than using them directly.

**Stop** is `POST /admin/runs/{id}/cancel`. It answers `202`, not `200`, because
cancellation is a request rather than an act: the outcome arrives on the trace as
`run_finished{status: "cancelled"}`, the same terminal event every other ending
produces, so a client that waits for the event needs no special case for stopping.
Two failure modes worth distinguishing in the UI — `409 conflict` means either the
run already finished (the message says which status) or it is not executing in this
process, and `404` means no such run at all.

`POST` answers **immediately with the id** and runs in the background. That is the
whole reason it is 202: a synchronous call could not hand you an id before the run
ended, so there would be nothing to attach a live panel to. The intended sequence
is POST → read `stream_url` → open the stream. There is no race: the stream replays
from the beginning of the log before tailing, so connecting late loses nothing.

`503 unavailable` means no `BLOOM_OPENROUTER_API_KEY` is configured — worth showing
as a setup prompt rather than an error.

### The event stream

Server-sent events. Each carries `id:` (a monotonic integer) and `event:` (the
kind). On reconnect the browser's `EventSource` sends `Last-Event-ID` automatically
and the stream resumes exactly where it stopped; a hand-rolled client should do the
same, or pass `?after=<id>`. A `: ping` comment arrives every 15s.

| `event:` | fields | meaning |
|---|---|---|
| `run_started` | `payload.agent_slug`, `payload.origin`, `payload.model_tier` | the run began |
| `text` | `payload.text` | one completed **sentence** of the answer |
| `tool_started` | `tool_name`, `payload.args` | a tool is about to run |
| `tool_finished` | `tool_name`, `ok`, `latency_ms`, `payload.result` | and its outcome |
| `step_finished` | `step_index`, `tokens_in`, `tokens_out`, `cost_usd`, `payload.model` | per-step accounting |
| `run_finished` | `ok`, `cost_usd`, `payload.status`, `payload.stopped_by`, `payload.error` | **terminal — the stream closes** |

**Be honest in the UI about what is live.** Text arrives at *sentence* granularity,
not token by token — the underlying runtime exposes no token callback that also
records cost, and Bloom will not trade the cost ledger for a smoother cursor. Tool
events are genuinely live. `step_finished` rows all arrive at the end, together,
because that is when the runtime makes them available. Rendering them as if they
streamed would be a lie the user notices.

`payload.status` is one of `succeeded | failed | cancelled | abandoned`.
`abandoned` means the process running it restarted. When `payload.stopped_by`
names a stop condition, **the answer is truncated** — the run hit its step or cost
ceiling — and should be labelled as such, not shown as a finished reply.

`/trace` returns the same records as a list, so one renderer serves both the live
panel and the history view. It 404s if the run does not belong to that agent.

`GET /admin/runs` is the global activity feed and each row carries `agent_slug`
joined in. It exists rather than fanning out over `/agents/{id}/runs` because pages
fetched per agent cannot be ordered against each other without fetching all of them.
A run whose config has since been deleted still appears, with `agent_slug: null` —
history outlives configuration, and dropping those rows would make spend vanish
along with the agent.

## Cost

```
GET /admin/usage?since=<iso8601>     200 → {since, runs, models, tools, caveat}
```

Three sources in one document, because no one of them answers the question alone:
`runs` is Bloom's own rollup (counts by outcome, spend, `by_agent`), `models` is
model spend per model from the runtime's tracker, and `tools` is tool-call counts
per tool and per caller from the MCP layer.

`tools` is **`null`, not zero**, when Bloom's MCP server is not mounted — zero would
read as "nothing has called me" rather than "nobody could".

Note this is deliberately *not* `/agent/usage`, which reports the same numbers behind
`BLOOM_MCP_KEYS`. Aperture should never hold an MCP key: it would let one leaked
token both read the ledger and spend against it.

The response carries a `caveat` string, and it should be shown rather than dropped —
see the accounting note below. Label totals "at least".

### One accounting caveat to surface

When a run stops on a ceiling, the underlying runtime makes one further completion
that it does not report. So a stopped run's `cost_usd` **under-reports by exactly
one model call**. This is upstream behaviour in `agent-runtime`, not a Bloom bug;
if the number is presented as authoritative, say "at least".

## Connections

```
GET    /admin/connections/kinds                        200 → what this build offers
POST   /admin/connections                              201 → Connection
GET    /admin/connections?kind=&provider=&status=      200 → Connection[]
GET    /admin/connections/{id}                         200 → Connection
PATCH  /admin/connections/{id}                         200 → Connection
DELETE /admin/connections/{id}?force=                  204 · 409 when attached
GET    /admin/connections/{id}/agents                  200 → {id, slug, name}[]
POST   /admin/connections/{id}/secret                  200 → Connection
POST   /admin/connections/{id}/revoke                  200 → Connection
POST   /admin/connections/{id}/test                    200 → {ok, checked, status, detail, tools}
POST   /admin/connections/{id}/oauth/start             200 → {authorize_url, state, …}
GET    /admin/agents/{id}/connections                  200 → Connection[]  (broker order)
POST   /admin/agents/{id}/connections                  201 → Connection[]
DELETE /admin/agents/{id}/connections/{cid}            204
GET    /admin/oauth/{provider}/callback     *** no auth — the browser lands here ***
```

**A connection is a library entry, not an agent's possession.** This is the change
to build around. Previously an OAuth connection could only be created *through* an
agent and was owned by it, so deleting the agent deleted the credential; a `shared`
flag existed and nothing could ever set it. Now:

- creating from an agent's page attaches it, via `attach_to` in the same
  transaction — one call, because "add a connection to this agent" is one intent
  and two would invent a half-done state to recover from;
- any other agent attaches the same row with `POST /admin/agents/{id}/connections`;
- deleting an agent deletes **no** connections;
- deleting a *connection* that is still attached is `409` naming the agents that
  would lose it. Render that as a confirmation and retry with `?force=true` — it is
  the one sharp edge of a shared library, so it asks rather than surprises.

### Three kinds

| `kind` | what it takes | what the agent gets |
|---|---|---|
| `oauth` | provider + client id/secret, then the browser | `<provider>_<operation>` tools, refreshed automatically |
| `api_key` | provider + a pasted key | the same tools, static key |
| `mcp` | `name` + `config.url` + optional bearer | every tool that server exposes, as `<name>__<tool>` |

`name` is validated as `^[a-z][a-z0-9_-]{0,23}$` with no `__`. For `mcp` it is the
tool namespace, so a `__` in it would make `<server>__<tool>` split in the wrong
place.

**No response ever carries a secret.** `has_secret` and `has_client_secret` are
booleans; `client_id` comes back inside `config` because it is not a secret and the
user must be able to see which app a connection is bound to. A `PATCH` carrying a
secret is `422` — rotation goes through `POST /{id}/secret`, so a key cannot ride
along on an ordinary edit and land in a request log.

`tools[]` on every connection is what makes a picker useful: for a provider it is
exactly the scope-filtered set the runner would register, and for a peer it is the
namespace glob until `/test` connects and returns the real list.

### Client credentials live on the connection

`ConnectionIn` takes `client_id` and `client_secret` for an `oauth` connection.
They resolve **connection first, environment second**: the manifest's
`client_id_env` / `client_secret_env` are a deployment-wide default, reported as
`has_deployment_default` on `/kinds`, and a connection carrying its own wins.

This is why there is no longer a `configured` flag to grey a Connect button out of.
It used to mean "this deployment has client credentials in `secrets.yaml`", which
made connecting a provider a deployment operation — edit a secrets file on the box,
reconcile, restart — for something a user should be able to do from a form. Missing
credentials are now a `422` at create time, naming the blank field, rather than a
`503` at the moment the browser was supposed to open.

**They are also settable afterwards**, through `POST /{id}/secret`:

```json
{"client_id": "…", "client_secret": "…"}
```

Create-time only was a hole with one very specific shape: a connection Aperture did
not create — which is *every* connection the builder makes for you — could never be
given an app registration, so `/oauth/start` answered "no client credentials … or
set `BLOOM_OAUTH_<PROVIDER>_CLIENT_ID`" and the environment really was the only route
left. The id rides with the secret because `client_for` refuses to mix a connection's
id with the environment's secret: an app registration is one intent, not two fields.

Omitted means *leave alone*. `client_id: ""` **clears** it, which is how a connection
goes back to this deployment's default — a state you have to be able to return to,
not only leave. An empty `secret` or `client_secret` is `422`: it is always a caller
bug, and stored it would be indistinguishable from a real credential. Client
credentials on a non-`oauth` connection are `422` too — a peer's bearer token is
`secret`.

### The redirect URI is Bloom's, and it must be registered first

`/admin/connections/kinds` reports `redirect_uri` per provider and `public_url` at
the top level:

```
https://<BLOOM_PUBLIC_URL>/admin/oauth/<provider>/callback
```

One route, `{provider_name}` templated, so a new provider needs no new route — only
a manifest. **Show it and let the user copy it; never rebuild it client-side.** It is
compared byte for byte, at the authorize step *and* again at the token exchange, so a
trailing slash or an `http` for an `https` fails at the last hop of a flow that looked
like it was working. Most providers also require `https` for anything that is not
loopback, and Spotify additionally refuses `localhost` in favour of `127.0.0.1` — all
of which the deployment's real public origin satisfies and a guess may not.

This is why Bloom hosts the callback rather than Aperture: the redirect target has to
be a stable public address registered in advance, and a desktop app has neither.

Both fields are `""` when this deployment has no `BLOOM_PUBLIC_URL`. That is worth
reporting as its own sentence — no browser flow can start at all — and `public_url`
is what distinguishes it from a provider that simply has no OAuth flow.

### The browser handoff

1. `POST /admin/connections` `{kind: "oauth", provider: "spotify", client_id, client_secret}`
   → a row with `status: "pending"`.
2. `POST /admin/connections/{id}/oauth/start` → `authorize_url`.
3. Open it with `shell.openExternal` — not a `BrowserWindow`: providers
   increasingly refuse embedded webviews, and the system browser has the session.
4. The provider redirects to `https://<bloom>/admin/oauth/spotify/callback`.
5. Bloom exchanges the code, encrypts and stores the tokens against **that
   connection**, and serves a page redirecting to
   `aperture://oauth-complete?provider=spotify&status=success`. It also renders
   readable text, so the flow works before the scheme is registered.
6. On the deep link: focus the window and **re-fetch** — do not trust the URL's
   `status`, since the server already recorded the outcome.

Re-running `/oauth/start` on a live connection is allowed and changes nothing until
the callback succeeds: opening the page and abandoning the tab must not break a
connection that already works.

`503` from `/start` or `/secret` means credentials cannot be stored at all
(`BLOOM_FEATURE_OAUTH`, `BLOOM_FERNET_KEYS`). Storing a secret without a key is
refused rather than downgraded. `/kinds` reports the same thing per kind as
`available` + `reason`, so it can be shown *before* the user picks.

## Suggested build order in Aperture

1. A small `fetch` client with base URL + bearer from settings, and one place that
   converts `{error, message}` into a thrown typed error. Everything else needs it.
2. Agent builder CRUD. Usable on its own, and the fastest way to prove the client.
3. Run history — a plain list, reusing the audit-log pattern from the SSH tab.
4. Test-run + live trace. `EventSource` gets `Last-Event-ID` resumption free; the
   renderer is the one the history view already needs.
5. The protocol handler and OAuth buttons last, since the server side degrades
   gracefully until then.
