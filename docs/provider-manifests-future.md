# Reaching a service Bloom has no manifest for

**Status: built.** This document used to lay out three options and pick none of
them; Option A shipped, hardened, with the storage question resolved differently
from the sketch. What follows is what exists, why it was chosen over the safer
alternatives, and what is still open. The security reasoning that argued *against*
this is preserved in "What it costs", because none of it stopped being true — it
was priced, not refuted.

## What happens today

The builder's fallback order:

1. a usable MCP server from the official registry — usable meaning a
   `streamable-http` remote needing no header but `Authorization: Bearer`, which is
   all `agent_runtime.MCPClient` can speak;
2. a provider manifest Bloom already has — shipped as a file, written here
   previously, or pulled from the sync store;
3. **write one**, from the service's own documentation, with
   `bloom_write_provider_manifest`;
4. stop and report — now only when the API is genuinely undocumented, behind a
   login, or reachable solely through blog posts.

Step 3 is why this document changed. "Adding a provider is a TOML file and a
redeploy" is fine for two worked examples and wrong as the general answer: there is
no version of *ship a manifest for every OAuth service* that scales, and it
contradicts the premise that a capability is a row in a table rather than a repo and
a deploy.

Step 4 still exists and still creates nothing. A manifest written from guesses is
worse than no manifest — it looks finished and fails later, in front of a user, with
a credential attached.

## Where they live

A `provider_manifests` row in `bloom.db`, not a file — the sketch's reasoning held
up exactly:

- `app/providers/` is inside the code tree, so model output written there is
  destroyed by the next image rebuild;
- `data/` is the only mounted volume and the only thing `backup-sqlite.sh` covers, so
  a second writable directory needs a second volume and a second backup path and gets
  silently lost the first time somebody forgets one;
- the table gives `run_id` and `created_at` free, and for a model-authored file
  "which run wrote this" is the first question anyone asks;
- reverting is `DELETE FROM`, or one button, rather than an ssh session.

`providers()` reads stored rows and nothing else.

**The two shipped files were removed, and that is a correction rather than a
tightening.** `spotify.toml` and `github.toml` were kept as reference
implementations that beat any row of the same name — which sounded conservative and
inverted the whole point. The two services most likely to already have a live
credential attached were the two whose definitions could not be repaired without a
release. It failed exactly there: Spotify shipped with `play`, `pause`, `search` and
`now_playing` and no `next`, so "skip this song" ran an agent that called nothing and
reported success, while the user's grant already carried
`user-modify-playback-state`. The capability existed, the definition did not, and the
definition was the one part that needed a pull request.

What replaced the precedence rule is weaker on paper and stronger in practice: a
manifest arriving from the sync store is ignored when a local row of that name
exists. That covers strictly more cases, since every provider anyone has actually
connected to has a row — and unlike the file rule, it does not also block the owner
of the install from fixing their own manifest.

The format's worked examples now live in `app/builder/manifest_format.py` (a
reference, not a provider) and in `tests/fixtures/`, where they are still parsed and
asserted on every run. `BLOOM_MANIFEST_SEED_DIR` imports a directory of manifests as
ordinary editable rows, never overwriting a name that already exists — an on-ramp,
not a tier.

## What it costs

Every concern that argued against this is still true. Each is now priced:

**A model authors the file that defines HTTP calls made with your credentials.**
Unchanged, and it is the real cost. `load_manifest_text(trusted=False)` adds, on top
of the rules every manifest passes: `api_base`/`authorize_url`/`token_url`/
`revoke_url` must be https and pass the same SSRF host check `read_url` uses; no
`DELETE` operation; at most 20 operations and 16 KB. The metadata-service case
(`https://169.254.169.254`) is the one that mattered most — without that check a
manifest naming it turns `CredentialResolver` into an authenticated client of the
instance's own identity service, looking like an ordinary provider in every list.

**`@lru_cache` is process-wide.** Still true, and now deliberate rather than
overlooked: `providers()` is invalidated by `reload_providers()` on every write, which
is what makes a manifest live inside the run that wrote it. Under `--workers N` a
manifest written by one worker is invisible to the others until they reload — the same
class of problem as the credential-refresh lock in `app/credentials.py`, and Bloom
still runs one worker.

**Prompt injection has somewhere to land.** `read_url` returns attacker-controlled
text into a context that now holds a tool for writing API definitions. This is the
sharpest remaining edge. What bounds it: the endpoint checks above, and the fact that
a manifest does nothing until a human attaches a credential — which is where
`credential_hosts` is surfaced.

**Two things replaced the review gate**, and neither is as strong as one:

- *the credential is the gate*. A manifest is inert until an account is attached, and
  that was always a human action. What the human lacked was the one fact worth
  knowing at that moment. `ConnectionOut` now carries `provider_reviewed`,
  `provider_source` and `credential_hosts` — "your key will be sent to
  api.example.com" is checkable against the service they meant to connect; a 90-line
  TOML document is not;
- *verification*. Validation proves a manifest parses and is safely shaped, never
  that its `api_base` describes a real API. A successful `POST /connections/{id}/test`
  is the only evidence that exists, and it marks the manifest verified. Anything
  unverified says so wherever it is shown.

## Options B and C, and why neither won

**B — the builder drafts it, a human commits it.** Safer, and every provider stays in
git. Rejected because it leaves a deploy in the middle of "create a Notion agent",
and the second step is the one nobody does. It also fails the actual requirement:
the goal is never opening the code editor.

**C — written but inert until approved in Aperture.** The document's own preferred
answer, and still the better security posture. Not built, because "inert until
approved" needs a review surface before *anything* works end to end, and the thing it
protects against — a manifest whose `api_base` is wrong or hostile — is now surfaced
at the credential form instead, which is a screen the user is already on. The
`review_manifest` checklist step is C's approval gesture without the block: it puts
the definition and its hosts in front of someone before they connect an account, and
does not stop them.

If prompt injection through `read_url` ever produces a real incident, C is the
correction, and `PUT /admin/manifests/{name}` is most of its plumbing already.

## Sharing

Manifests travel through the sync store's `/manifests`, the same way model keywords
travel through `/models` — a manifest written on one install is research and model
spend every other install would otherwise repeat.

The store treats the TOML as **opaque**: it does not parse it and must not start,
because the format's rules live in Bloom and are versioned with Bloom. So a pulled
manifest is untrusted input and goes through `trusted=False` exactly like a local
one. Travelling confers nothing.

**Local always wins.** A pull never overwrites a manifest this install has, which is
what makes `PUT /admin/manifests/{name}` trustworthy: a correction made in Aperture
cannot be silently undone by a background pass an hour later. `verified` travels as
advice — some install proved this live — and a puller still starts unverified until
its own credential probes successfully.

## The related gap: `requires_confirmation`

Unchanged, and it is what Option C is really waiting for. The ecosystem has the
mechanism — `agent_mcp` gates a tool on an inbound `X-Confirmed: true` — and no way
to produce it: Amber's wire protocol has no tool-approval frame. `run_task` has been
waiting on the same thing since it was written. When that frame lands, an approval
step at the moment of the *call* would be strictly better than one at the moment of
authoring, and this document should be revisited.

## Still open

- **Operation quality.** The endpoint checks bound the damage a manifest can do;
  nothing bounds how *wrong* its operations can be short of a live probe. A wrong
  path fails at the first call. `allow_request` is the honest escape when the API is
  too irregular to model, and `PUT` is the fix when it is merely wrong.
- **Deeper verification.** The probe proves the credential and the API base. It does
  not exercise any declared operation, so a manifest can be verified and still have a
  broken `run_report`.
- **Multi-worker cache invalidation**, if Bloom ever runs more than one.
