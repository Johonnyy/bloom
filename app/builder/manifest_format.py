"""The manifest format, written for the model that has to produce one.

Its own module for the same reason `app.builder.prompt` is: this is reference
prose, it is long, and burying it in a function makes both harder to read. It is
returned verbatim by the `bloom_list_manifest_format` tool.

**Why a tool and not a paragraph in the system prompt.** Every build pays for the
system prompt, and most builds never write a manifest — the MCP registry or a
shipped provider answers first. Two hundred lines of TOML reference on every run,
to be used by one in five, is exactly the bloat that makes a prompt stop being read.
As a tool it is fetched by the builds that need it.

**Why it is a worked example rather than a grammar.** A model given a field list
produces something that parses and is wrong in the ways that matter — an
``api_base`` with a trailing path segment that every operation then duplicates, a
scope string invented rather than read. A complete, correct, annotated example is
what it actually copies from, and the annotations are where the rules that cannot
be expressed as a schema live.
"""

from __future__ import annotations

from app.providers.registry import (
    MAX_STORED_OPERATIONS,
    MAX_STORED_TOML_BYTES,
    REQUEST_METHODS,
)

FORMAT = f"""\
A provider manifest is TOML. It declares how to reach one service's API with a \
credential, and each [[operations]] entry becomes a tool the built agent can call.

## The rules that will reject your manifest

- `name` is snake_case, at most 20 characters. It prefixes every tool, so name \
`analytics` and the tools become `analytics_run_report`.
- `api_base`, `authorize_url`, `token_url` and `revoke_url` must all be https and \
resolve to a public address. This is enforced.
- No operation may be a DELETE. Bloom runs these unattended and there is no \
approval step, so deletion is refused outright.
- At most {MAX_STORED_OPERATIONS} operations, and at most {MAX_STORED_TOML_BYTES} \
bytes of TOML in total.
- No parameter may be named `authorization`, `token`, `access_token`, `api_key`, \
`apikey` or `cookie`, and none may live in a header. Bloom injects the credential \
itself; a parameter that could carry one would put it in the trace.
- Every bare key must appear ABOVE the first [table] header. TOML assigns a key to \
the most recently opened table, so a `scopes_default` written after [probe] \
silently becomes `probe.scopes_default` and the provider quietly gets no scopes. \
**Keep [probe] and [[operations]] last.** This has already happened once, to the \
shipped Spotify manifest.

## Read, do not recall

Every URL, scope string and path must come from a page you actually read with \
read_url. An invented scope fails at the consent screen, in front of the user; an \
invented path fails at the first call. If the documentation does not say, do not \
guess — write fewer operations, or set `allow_request = true` and let the agent \
work it out at call time.

## Worked example

```toml
name = "analytics"
display_name = "Google Analytics"
api_base = "https://analyticsdata.googleapis.com/v1beta"

# OAuth endpoints, read from the provider's own docs.
authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
token_url = "https://oauth2.googleapis.com/token"

# Environment variables holding this deployment's app registration. Name them
# even though they are not set yet — the checklist tells the human to set them.
client_id_env = "BLOOM_OAUTH_ANALYTICS_CLIENT_ID"
client_secret_env = "BLOOM_OAUTH_ANALYTICS_CLIENT_SECRET"

# "oauth", "api_key", or both: ["oauth", "api_key"]. Omitted means oauth.
auth = "oauth"

pkce = true              # true unless the docs say the provider does not support it
refresh_rotates = false  # true if a refresh returns a NEW refresh token
auth_style = "basic"     # how client credentials reach the token endpoint: basic | body

# The exact strings from the provider's scope reference, not a description of them.
scopes_default = ["https://www.googleapis.com/auth/analytics.readonly"]

docs_url = "https://developers.google.com/analytics/devguides/reporting/data/v1"

# Optional escape hatch. Set true when the API is too large or too irregular to
# model as operations — the agent gets one bounded tool ({", ".join(REQUEST_METHODS)},
# locked to api_base) instead of guessing which named tool to use. Prefer real
# operations; use this when you would otherwise have to stop.
allow_request = false

# --- tables last, always ---

# A cheap authenticated GET that proves a credential works. This is what "Test
# connection" calls, and passing it is what marks your manifest verified.
[probe]
method = "GET"
path = "/properties"

[[operations]]
name = "run_report"
method = "POST"
path = "/properties/{{property_id}}:runReport"
read_only = true
scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
description = \"\"\"Run a report over one Analytics property. Returns JSON with
.rows[], each carrying .dimensionValues[] and .metricValues[].\"\"\"
# `params` is a TABLE keyed by parameter name — NOT [[operations.params]].
# Each value is an inline table. An operation with no parameters still writes the
# empty header, as `now_playing` does in spotify.toml.
[operations.params]
property_id = {{ in = "path", type = "string", required = true, description = "Numeric property id, no 'properties/' prefix." }}
dateRanges = {{ in = "body", type = "array", items = "object", description = "e.g. [{{ startDate = '7daysAgo' }}]" }}
limit = {{ in = "query", type = "integer", default = 100, description = "1-1000." }}

[[operations]]
name = "list_properties"
method = "GET"
path = "/properties"
read_only = true
description = "Every property this account can read, with its id and display name."
[operations.params]
```

`in` is `path`, `query` or `body`. `type` is a JSON type — string, integer, \
number, boolean, array or object; an `array` also takes `items`. A parameter used \
in `path` must appear in the path as `{{name}}`.

## Writing good operations

`description` is the only thing the agent sees when choosing a tool. Say what it \
returns, not what it is called. "Run a report over one property, returning rows of \
metrics by dimension" beats "runs a report".

`read_only = true` on anything that only reads. A caller decides whether it may \
retry from that flag alone, so getting it wrong on a write is the expensive \
direction.

`scopes` on an operation is what hides it when the granted token cannot authorise \
it. Leave it empty only if the operation genuinely needs no scope — an operation \
with the wrong scopes listed is worse than one with none, because it will be hidden \
when it would have worked.

Write the operations the brief needs. Not the whole API: an agent choosing between \
twenty tools chooses worse than one choosing between four.
"""

__all__ = ["FORMAT"]
