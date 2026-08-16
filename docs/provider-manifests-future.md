# Reaching a service Bloom has no manifest for

What the builder does today when it finds neither an MCP server nor a shipped
provider manifest, the options that were considered instead, and what each would
cost. Written down because the decision is easy to re-litigate badly: "just let it
write the file" sounds obviously right until you notice what the file *is*.

## What happens today

The builder's fallback order is:

1. a usable MCP server from the official registry — usable meaning a
   `streamable-http` remote needing no header but `Authorization: Bearer`, which is
   all `agent_runtime.MCPClient` can speak;
2. a provider manifest Bloom already ships (`app/providers/*.toml`), connected as
   `oauth` or `api_key`;
3. **stop, and report.**

Step 3 is a deliberate outcome, not a gap. The build ends `failed` with a summary
naming what it found — the service's real API documentation URL, whether it uses
OAuth or a static key — and creates nothing. No agent, no connection.

Creating a half-built agent instead was considered and rejected: a Spotify agent
that cannot reach Spotify is worse than no agent, because it *looks* finished. It
would sit in the list, answer questions by hallucinating, and the reason would be a
`pending` connection nobody was looking at.

The gap this leaves is real: adding a provider is still a human writing a TOML file
and redeploying. What follows is how that might change.

## Option A — the builder writes the manifest at runtime

Give it a `bloom_write_provider_manifest(name, toml_text)` tool. The TOML is
validated by the existing `load_manifest_text`, stored in a `provider_manifests`
table, and `providers.cache_clear()` makes it live inside the same run.

**Why it is attractive.** It is the only option that makes "create a Notion agent"
work end to end with no human code change, which is the whole promise of the
builder. Everything else leaves a redeploy in the middle.

**What it costs.**

- *A model authors the file that defines HTTP calls made with your credentials.* A
  manifest is not inert data: `register_operations` turns each `[[operations]]`
  entry into a callable tool, and `CredentialResolver` attaches a live token to
  every request it makes. The existing load-time validation (`FORBIDDEN_PARAMS`,
  the header ban, the tool-name rule) is what stands between that and a secret in a
  log — it was written assuming a human wrote the file, and it holds, but it has
  never been asked to hold against adversarial input.
- *`@lru_cache` is process-wide.* `providers()` caches per process, so a manifest
  written by one worker stays invisible to the others until restart. Correct for a
  single uvicorn worker, wrong under `--workers N`. Same class of problem as the
  credential-refresh lock in `app/credentials.py`.
- *Prompt injection has somewhere to land.* `read_url` returns attacker-controlled
  text into a context that would then hold a tool for writing API definitions. Today
  the worst a poisoned page achieves is a badly-chosen MCP server that a human still
  has to attach a credential to. With this tool it could specify the endpoint.

**Extra validation it would need**, beyond what `load_manifest` already does:

- `api_base` must be `https://` and pass the same SSRF host check `read_url` uses —
  otherwise a manifest pointing at `http://169.254.169.254` turns the credential
  resolver into a metadata-service client with a real token in the header;
- refuse a name that a file-shipped manifest already defines, so a model can never
  shadow `spotify.toml` — and, more generally, a file must always win over a row;
- refuse `DELETE` operations outright: a manifest written by a model and used
  unattended should not be able to delete anything;
- caps — 20 operations, 16 KB of TOML — because model output is unbounded by nature.

**Where it should be stored.** A `provider_manifests` table in `bloom.db`, not a
writable directory. `data/` is the only mounted volume and the only thing
`backup-sqlite.sh` covers; a second directory needs a second volume and a second
backup path and gets silently lost on the first redeploy that forgets one.
`MANIFEST_DIR` is inside the code tree, so writing model output there is destroyed
by every image rebuild. The table also gives `run_id` and `created_at` for free, and
for a model-authored file "which run wrote this" is the first question anyone asks.
Reverting becomes `DELETE FROM` rather than an ssh session.

## Option B — the builder drafts it, a human commits it

Same authoring, but the TOML goes into the setup checklist as a reviewable block
instead of into a table. The human reads it, commits it to `app/providers/`, and
redeploys.

Safer, and every provider stays in git where it can be reviewed and blamed. The cost
is that "create a Notion agent" becomes a two-step flow with a deploy in the middle,
and the second step is easy to never do.

## Option C — written but inert until approved

The middle: the builder writes to the table, but the row is marked `pending_review`
and `providers()` skips it until someone approves it in Aperture. Autonomous
end-to-end, with one human gate on the security-sensitive artifact.

This is probably the right eventual answer. It needs a real review surface — a diff
view in Aperture, and an approve/reject endpoint — which is more UI than the feature
has earned yet. Note that the checklist already *is* an approval surface by accident;
making it one on purpose is most of the work.

## The related gap: `requires_confirmation`

All three options would be better with a human-approval step at the moment of the
call rather than at the moment of authoring. The ecosystem has the mechanism —
`agent_mcp` gates a tool on an inbound `X-Confirmed: true` — and no way to produce
it: Amber's wire protocol has no tool-approval frame. `run_task` has been waiting on
the same thing since it was written. When that frame lands, revisit this whole
document; it changes the trade in Option A materially.
