"""Stored provider manifests: the half of a provider that is data, not code.

`app.providers.registry` knows how to parse and validate a manifest and nothing
about where one lives. This module is the other side — it reads rows out of
``provider_manifests``, hands them to the same parser, and installs itself as the
registry's source of stored providers. The split is deliberate and load-bearing:
the registry is imported by tests and tools that never open a database, so a direct
import of `app.db` there would make a provider definition require one.

**Why this exists at all.** Adding a provider used to be a TOML file in the code
tree and a redeploy. There is no version of "ship a manifest for every OAuth
service" that scales, and the whole point of the builder is that a capability is a
row in a table rather than a repo and a deploy. So the builder writes them, at
runtime, from the service's own documentation.

Bloom kept two of them — `spotify.toml` and `github.toml` — as reviewed worked
examples that beat any row of the same name. That exemption is gone, because it
failed in exactly the case it was meant to serve: the two providers most likely to
already be connected were the two whose gaps could not be repaired by asking. A
Spotify manifest with no `next` operation made "skip this song" unanswerable, and
the refusal pointed at a pull request. Every manifest is a row now. The worked
example the builder needs is `app.builder.manifest_format.FORMAT`, which is a
*reference*, not a provider — it teaches the shape without deciding what exists.

**What that costs, and what pays for it.** A manifest is not inert data:
`register_operations` turns each entry into a callable tool and
`CredentialResolver` attaches a live token to every request it makes. Four things
stand between that and a bad outcome, and all four matter:

1. `load_manifest_text(trusted=False)` — https-and-public endpoints, no ``DELETE``,
   bounded size and operation count, on top of every rule a file manifest already
   passes (the tool-name regex, `FORBIDDEN_PARAMS`, the header ban);
2. **operations are bounded and named.** Every tool a manifest contributes is
   prefixed with the provider name and shown before a credential is attached, so
   a manifest cannot quietly grow reach the connection screen did not disclose;
3. **the credential is the real gate.** A manifest does nothing until someone
   attaches an account to it, and that has always been a human action. What the
   human lacked was the one fact worth knowing at that moment, which
   `Provider.credential_hosts()` now supplies: the hosts their key will be sent to;
4. **verification.** A manifest that has never answered a real request is marked
   unverified everywhere it is shown, rather than looking identical to one that
   works.

`Provider.source` still says where a manifest came from — written here, shared by
another install, or seeded — everywhere one is surfaced. That is the honest
position: this trades a review gate for a deploy gate, on purpose, and shows its
working instead of hiding it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.db import Store, get_store
from app.providers import (
    ManifestError,
    Provider,
    load_manifest,
    load_manifest_text,
    reload_providers,
    set_stored_loader,
)

logger = logging.getLogger(__name__)


def writable_name(name: str) -> str:
    """The reason this provider name may not be stored, or empty if it may.

    Every name is writable now. This used to refuse the two shipped files, and the
    refusal is what made a missing Spotify operation a code change; see the module
    docstring. It stays as a function because the *rest* of the write path calls it
    and a future reservation (a name the runtime itself uses) would belong here
    rather than scattered across three call sites.
    """
    name = (name or "").strip().lower()
    if not name:
        return "A manifest needs a name."
    return ""


def stored_providers(store: Store | None = None) -> dict[str, Provider]:
    """Every manifest in the database that still parses, by name.

    A row that no longer validates is logged and skipped rather than raised: one
    bad manifest must cost exactly one provider and never the service's ability to
    start. That matters more than it looks now that these rows are the only source
    of providers there is — they hold model output and arrive from other installs.
    """
    store = store or get_store()
    found: dict[str, Provider] = {}
    for row in store.list_manifests():
        try:
            provider = load_manifest_text(
                row["toml"],
                where=f"stored manifest {row['name']!r}",
                trusted=False,
                source=row["source"],
            )
        except ManifestError:
            logger.exception("Ignoring stored provider manifest %s", row["name"])
            continue
        if provider.name != row["name"]:
            # The row is keyed by name and `providers()` keys by the parsed name;
            # letting them disagree would make a manifest unfindable by the name it
            # was stored under, and undeletable through the admin surface.
            logger.warning(
                "Stored manifest row %r declares name %r; ignoring", row["name"], provider.name
            )
            continue
        found[provider.name] = provider
    return found


def install_loader(store: Store | None = None) -> None:
    """Point the registry at the database. Called once, in the lifespan.

    Idempotent, and safe to call in a test to rebind a fresh store — which is the
    other reason this is a setter rather than an import: a per-test store must be
    able to replace a previous one without the registry holding a stale handle.
    """
    resolved = store

    def loader() -> dict[str, Provider]:
        return stored_providers(resolved)

    set_stored_loader(loader)


def uninstall_loader() -> None:
    """Detach the database source. Nothing is left — every manifest is a row."""
    set_stored_loader(None)


def seed_from_dir(directory: Path | str, store: Store | None = None) -> list[str]:
    """Import ``*.toml`` from a directory as ordinary rows. Returns names imported.

    The one supported way a manifest reaches Bloom from disk, and deliberately weak:
    it **never overwrites** a name that already has a row, and what it writes is an
    ordinary editable manifest with ``source='seed'`` — not a tier, not an override.
    That is the whole difference from the shipped files this replaced. A seeded
    manifest can be edited by the builder, by `/admin/manifests`, and by asking, the
    same as one written here; re-running the import will not undo those edits.

    Unset by default, so a stock Bloom starts with no providers at all and learns
    every one of them. It exists for the cases where that is not what you want: a
    test fixture, an install restoring from an export, an operator with a manifest
    they would rather not have researched twice.

    A file that does not parse is logged and skipped — importing four of five
    manifests beats refusing to start over the fifth.
    """
    store = store or get_store()
    directory = Path(directory)
    if not directory.is_dir():
        logger.warning("Manifest seed directory %s does not exist; nothing imported", directory)
        return []

    existing = set(stored_providers(store))
    imported: list[str] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            provider = load_manifest(path)
        except ManifestError:
            logger.exception("Ignoring seed manifest %s", path.name)
            continue
        if provider.name in existing:
            continue
        try:
            save(
                name=provider.name,
                toml=path.read_text(encoding="utf-8"),
                source="seed",
                store=store,
            )
        except ManifestError:
            logger.exception("Ignoring seed manifest %s", path.name)
            continue
        imported.append(provider.name)

    if imported:
        logger.info("Seeded %d provider manifest(s): %s", len(imported), ", ".join(imported))
    return imported


def save(
    *, name: str, toml: str, run_id: str = "", source: str = "stored", store: Store | None = None
) -> Provider:
    """Validate and store one manifest, then make it live immediately.

    Raises :class:`ManifestError` for anything a stored manifest may not be — the
    caller turns that into prose for a model or a 422 for Aperture, which are the
    two audiences and the only two.

    Parsing happens *before* the write, so a manifest that would not load never
    reaches the table. The cache is dropped after, which is what lets the builder
    write a manifest and create a connection against it in the same run instead of
    reporting success and waiting for a restart.
    """
    store = store or get_store()
    name = (name or "").strip().lower()

    refusal = writable_name(name)
    if refusal:
        raise ManifestError(refusal)

    provider = load_manifest_text(toml, where=f"manifest {name!r}", trusted=False, source=source)
    if provider.name != name:
        raise ManifestError(
            f"The manifest declares name {provider.name!r} but is being saved as "
            f"{name!r}. They must match — the name is the provider's identity and "
            "the prefix on every tool it contributes."
        )

    store.upsert_manifest(name=name, toml=toml, source=source, run_id=run_id)
    reload_providers()
    logger.info(
        "Stored provider manifest %s (%d operation(s), source=%s)",
        name,
        len(provider.operations),
        source,
    )
    return provider


def delete(name: str, store: Store | None = None) -> bool:
    """Remove a stored manifest and make the removal live."""
    store = store or get_store()
    removed = store.delete_manifest((name or "").strip().lower())
    if removed:
        reload_providers()
        logger.info("Deleted stored provider manifest %s", name)
    return removed


def mark_verified(name: str, *, note: str = "", store: Store | None = None) -> None:
    """Record that this manifest answered a real authenticated request."""
    store = store or get_store()
    store.mark_manifest_verified((name or "").strip().lower(), note=note)


__all__ = [
    "delete",
    "install_loader",
    "mark_verified",
    "save",
    "seed_from_dir",
    "stored_providers",
    "uninstall_loader",
    "writable_name",
]
