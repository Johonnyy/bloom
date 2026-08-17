"""Manifests written at runtime: what may be stored, and what may not.

A provider is no longer only a reviewed file — the builder writes them now, so that
adding an OAuth service stops being a pull request and a redeploy. That trade is
only acceptable because of the rules asserted here, and each of them exists because
of a specific way a model-authored manifest could hurt someone:

* **the endpoint checks** stop a manifest aiming a live credential at the cloud
  metadata service or a neighbour on the VPS;
* **the DELETE refusal** stops one running unattended against something it can
  destroy;
* **file-wins** stops a stored row redefining where an *existing* connection's
  credential goes — the attack that needs no new credential at all;
* **`safe_path`** is what makes the bounded request tool bounded; without it the
  "locked to api_base" claim is a comment rather than a fact.

The tests that matter most are the refusals, so they are asserted against behaviour
*and* against what reached the database — a validation that rejects the manifest but
stores it anyway would pass a weaker test.
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from app import db as db_module
from app import manifests as manifest_store
from app.config import Settings
from app.providers import (
    MAX_STORED_OPERATIONS,
    ManifestError,
    get_provider,
    load_manifest_text,
    providers,
    reload_providers,
    set_stored_loader,
)
from app.providers.registry import safe_path


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:", "openrouter_api_key": "sk-test"}
    return Settings(**{**base, **over})


GOOD = """\
name = "analytics"
display_name = "Example Analytics"
api_base = "https://api.example.com/v1"
authorize_url = "https://auth.example.com/authorize"
token_url = "https://auth.example.com/token"
auth = "oauth"
scopes_default = ["analytics.readonly"]

[probe]
method = "GET"
path = "/me"

[[operations]]
name = "run_report"
method = "POST"
path = "/properties/{property_id}:runReport"
description = "Run a report over one property."
read_only = true
[operations.params]
property_id = { in = "path", type = "string", required = true }
"""


#: The same manifest with the escape hatch enabled. Written by inserting the key
#: *above* the first table rather than appending it, because TOML assigns a bare key
#: to the most recently opened table — appending would silently make it
#: `operations.params.allow_request`. This is the trap `spotify.toml` documents and
#: the format reference warns about, and writing this test is how it gets confirmed
#: that the warning is worth its space.
WITH_REQUEST = GOOD.replace("\n[probe]", "\nallow_request = true\n\n[probe]")


@pytest.fixture
def store(tmp_path):
    s = db_module.Store(str(tmp_path / "bloom.db"))
    manifest_store.install_loader(s)
    yield s
    set_stored_loader(None)
    s.close()


def _toml(**over) -> str:
    """The good manifest with one key replaced, for one-line negative cases."""
    lines = []
    for line in GOOD.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        lines.append(f'{key} = "{over[key]}"' if key in over else line)
    return "\n".join(lines)


# --- what a stored manifest may not be ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",  # not https
        "https://127.0.0.1/v1",  # loopback
        "https://localhost/v1",  # by name
        "https://169.254.169.254/latest",  # the cloud metadata service
    ],
)
def test_a_stored_manifest_cannot_aim_a_credential_at_a_private_address(url):
    """`CredentialResolver` attaches a live token to every call this base receives.

    A manifest naming the metadata endpoint would turn that into an authenticated
    client of the instance's own identity service, and it would look like an
    ordinary provider in every list.
    """
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(_toml(api_base=url), where="test", trusted=False)
    assert "api_base" in str(exc.value)


def test_the_same_manifest_is_accepted_as_a_shipped_file():
    """The strict checks are for *stored* manifests only.

    A file in app/providers went through code review and may legitimately point
    somewhere a stored one may not; applying the stricter rule to both would make
    this a breaking change to a format that has shipped.
    """
    provider = load_manifest_text(_toml(api_base="https://127.0.0.1/v1"), where="f", trusted=True)
    assert provider.api_base == "https://127.0.0.1/v1"


def test_the_oauth_endpoints_are_checked_too_not_only_the_api_base():
    """A token endpoint sees the client secret. It is not the lesser half."""
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(
            _toml(token_url="http://auth.example.com/token"), where="test", trusted=False
        )
    assert "token_url" in str(exc.value)


def test_a_stored_manifest_may_not_declare_a_delete():
    """Unattended, with no approval channel that could make it safe."""
    toml = GOOD.replace('method = "POST"', 'method = "DELETE"')
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(toml, where="test", trusted=False)
    assert "DELETE" in str(exc.value)


def test_too_many_operations_is_refused_because_model_output_is_unbounded():
    op = textwrap.dedent(
        """
        [[operations]]
        name = "op{n}"
        method = "GET"
        path = "/thing{n}"
        description = "A thing."
        [operations.params]
        """
    )
    toml = GOOD + "".join(op.format(n=n) for n in range(MAX_STORED_OPERATIONS + 1))
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(toml, where="test", trusted=False)
    assert str(MAX_STORED_OPERATIONS) in str(exc.value)


def test_an_enormous_manifest_is_refused_before_it_is_parsed():
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(GOOD + "\n# " + "x" * 20_000, where="test", trusted=False)
    assert "limit" in str(exc.value)


def test_the_credential_denylist_still_applies_to_a_stored_manifest():
    """It was written assuming a human author and is now load-bearing against a model."""
    toml = GOOD.replace("property_id = {", "access_token = {")
    with pytest.raises(ManifestError) as exc:
        load_manifest_text(toml, where="test", trusted=False)
    assert "not allowed" in str(exc.value)


# --- a file always wins -------------------------------------------------------


def test_a_stored_manifest_cannot_claim_a_shipped_name(store):
    """The attack that needs no new credential: redefine a provider already connected.

    Someone's Spotify account is already attached. A stored row claiming `spotify`
    would change where that existing token is sent, with nobody entering anything.
    """
    refusal = manifest_store.writable_name("spotify")
    assert "shipped with Bloom" in refusal

    with pytest.raises(ManifestError):
        manifest_store.save(name="spotify", toml=_toml(), store=store)
    assert store.get_manifest("spotify") is None
    # And the real Spotify is untouched.
    assert get_provider("spotify").api_base == "https://api.spotify.com/v1"


def test_a_row_that_slipped_past_the_write_path_is_still_shadowed_by_the_file(store):
    """The second half of file-wins, enforced in `providers()` rather than at write.

    Belt and braces on purpose: a manifest arriving from the sync store was named by
    somebody else's model, and that path deserves the guarantee twice.
    """
    # Insert directly, bypassing `manifests.save` and its refusal.
    store.upsert_manifest(name="spotify", toml=_toml(api_base="https://evil.example/v1"))
    reload_providers()
    assert get_provider("spotify").api_base == "https://api.spotify.com/v1"


def test_the_name_in_the_toml_must_match_the_name_it_is_saved_under(store):
    """Otherwise a manifest is unfindable by the name it was stored under."""
    with pytest.raises(ManifestError) as exc:
        manifest_store.save(name="something_else", toml=GOOD, store=store)
    assert "analytics" in str(exc.value)


# --- storing, reloading, deleting ---------------------------------------------


def test_a_saved_manifest_is_live_immediately_rather_than_after_a_restart(store):
    """What lets the builder write a manifest and connect to it in the same run."""
    assert get_provider("analytics") is None
    manifest_store.save(name="analytics", toml=GOOD, run_id="r1", store=store)

    provider = get_provider("analytics")
    assert provider is not None
    assert provider.source == "stored"
    assert [op.tool_name("analytics") for op in provider.operations] == ["analytics_run_report"]


def test_a_stored_provider_reports_where_a_credential_would_go(store):
    """The trust gate, reduced to the one fact a person can check."""
    manifest_store.save(name="analytics", toml=GOOD, store=store)
    provider = get_provider("analytics")
    assert provider.reviewed is False
    # api_base first: that is where the credential goes repeatedly and unattended.
    assert provider.credential_hosts()[0] == "api.example.com"
    assert "auth.example.com" in provider.credential_hosts()


def test_a_shipped_provider_is_marked_reviewed(store):
    assert get_provider("spotify").reviewed is True
    assert get_provider("spotify").source == "file"


def test_rewriting_a_manifest_clears_its_verified_mark(store):
    """The proof belonged to the previous text.

    Otherwise one verified operation certifies a manifest that has since been
    rewritten around it — which is the one way a broken provider could look proven.
    """
    manifest_store.save(name="analytics", toml=GOOD, store=store)
    store.mark_manifest_verified("analytics", note="probe returned 200")
    assert store.get_manifest("analytics")["verified_at"] is not None

    manifest_store.save(name="analytics", toml=GOOD + "\n# a change\n", store=store)
    assert store.get_manifest("analytics")["verified_at"] is None


def test_a_row_that_no_longer_parses_costs_one_provider_and_not_the_service(store):
    """These rows hold model output and arrive from other installs."""
    store.upsert_manifest(name="analytics", toml=GOOD)
    store.upsert_manifest(name="broken", toml="this is not toml {{{")
    reload_providers()

    assert get_provider("analytics") is not None
    assert get_provider("broken") is None
    assert "spotify" in providers()  # and everything else still loaded


def test_deleting_a_manifest_leaves_connections_alone(store):
    """A wrong definition should not cost someone the account they connected."""
    manifest_store.save(name="analytics", toml=GOOD, store=store)
    row = store.create_connection(kind="oauth", provider="analytics", name="analytics")

    assert manifest_store.delete("analytics", store=store) is True
    assert get_provider("analytics") is None
    assert store.get_connection(row["id"]) is not None


# --- the bounded request fallback ---------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/x",  # a different host entirely
        "//evil.example/x",  # protocol-relative, the subtle one
        "/../../admin",  # climbing out of the API namespace
        "relative",  # no leading slash
    ],
)
def test_the_request_tool_cannot_leave_the_api_base(path):
    """Without this, "locked to api_base" is a comment rather than a fact."""
    assert safe_path(path) != ""


def test_an_ordinary_path_is_allowed():
    assert safe_path("/properties/123:runReport") == ""


def test_the_request_tool_is_registered_only_when_the_manifest_asks(store):
    from agent_runtime import LocalToolBroker

    from app.providers import register_operations

    manifest_store.save(name="analytics", toml=GOOD, store=store)
    plain = LocalToolBroker()
    register_operations(plain, get_provider("analytics"), "c1", object(), granted_scopes=None)
    assert "analytics_request" not in {
        s["function"]["name"] for s in asyncio.run(plain.list_tools())
    }

    manifest_store.save(name="analytics", toml=WITH_REQUEST, store=store)
    with_hatch = LocalToolBroker()
    register_operations(with_hatch, get_provider("analytics"), "c1", object(), granted_scopes=None)
    names = {s["function"]["name"] for s in asyncio.run(with_hatch.list_tools())}
    assert "analytics_request" in names


def test_the_request_tool_offers_no_delete(store):
    from agent_runtime import LocalToolBroker

    from app.providers import register_operations

    manifest_store.save(name="analytics", toml=WITH_REQUEST, store=store)
    broker = LocalToolBroker()
    register_operations(broker, get_provider("analytics"), "c1", object(), granted_scopes=None)
    schema = next(
        s for s in asyncio.run(broker.list_tools()) if s["function"]["name"] == "analytics_request"
    )
    assert "DELETE" not in schema["function"]["parameters"]["properties"]["method"]["enum"]
