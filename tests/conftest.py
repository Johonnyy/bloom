"""Provider manifests for the suite.

Bloom ships none. `spotify.toml` and `github.toml` used to live in `app/providers`
as reviewed files that beat any stored row of the same name, and that exemption is
gone — it made a gap in one of them (Spotify with no ``next`` operation) a pull
request and a redeploy, which is the deploy gate the builder exists to remove.

They are test fixtures now, in `tests/fixtures`, and they reach the tests the same
two ways a real install gets a manifest:

* tests that never open a database read them through a stored loader installed
  here, so `providers()` answers without one — the role `file_providers()` used to
  play, minus the precedence;
* tests that start the app get them through `BLOOM_MANIFEST_SEED_DIR`, the real
  import path, so what the suite exercises is the mechanism an operator would use
  rather than a test-only shortcut.

Keeping them means the manifest format still has two worked examples under test.
What it no longer means is that a stock Bloom can reach Spotify.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.providers import ManifestError, load_manifest, set_stored_loader

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
_SEED_ENV = "BLOOM_MANIFEST_SEED_DIR"


def fixture_providers() -> dict:
    """Every fixture manifest, parsed under the rules a stored manifest must pass."""
    found = {}
    for path in sorted(FIXTURE_DIR.glob("*.toml")):
        try:
            provider = load_manifest(path)
        except ManifestError as exc:  # A broken fixture should name itself loudly.
            raise AssertionError(f"Fixture manifest {path.name} does not load: {exc}") from exc
        found[provider.name] = provider
    return found


@pytest.fixture(autouse=True)
def _manifest_fixtures():
    """Make the fixture manifests reachable both ways, then put the registry back.

    Autouse because the alternative is threading it through seventeen files' own
    ``client`` fixtures, and because a test that forgot it would fail in a way that
    looks like a provider bug rather than a missing fixture.

    Deliberately does **not** request ``monkeypatch``. Requesting it from the first
    autouse fixture in the session would build it before every per-module fixture,
    which inverts teardown order: a module fixture that undoes a ``setattr`` in its
    own teardown would then run before monkeypatch restored the attribute. That is
    a real failure this fixture caused in `test_mcp.py`, not a hypothetical.
    """
    before = os.environ.get(_SEED_ENV)
    os.environ[_SEED_ENV] = str(FIXTURE_DIR)
    set_stored_loader(fixture_providers)
    try:
        yield
    finally:
        set_stored_loader(None)
        if before is None:
            os.environ.pop(_SEED_ENV, None)
        else:
            os.environ[_SEED_ENV] = before
