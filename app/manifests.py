"""Stored provider manifests: the half of a provider that is data, not code.

`app.providers.registry` knows how to parse and validate a manifest and nothing
about where one lives. This module is the other side — it reads rows out of
``provider_manifests``, hands them to the same parser, and installs itself as the
registry's source of stored providers. The split is deliberate and load-bearing:
the registry is imported by tests and tools that never open a database, so a direct
import of `app.db` there would make a provider definition require one.

**Why this exists at all.** Adding a provider used to be a TOML file in the code
tree and a redeploy. That is fine for the two worked examples and wrong as the
general answer — there is no version of "ship a manifest for every OAuth service"
that scales, and the whole point of the builder is that a capability is a row in a
table rather than a repo and a deploy. So the builder writes them, at runtime, from
the service's own documentation.

**What that costs, and what pays for it.** A manifest is not inert data:
`register_operations` turns each entry into a callable tool and
`CredentialResolver` attaches a live token to every request it makes. Four things
stand between that and a bad outcome, and all four matter:

1. `load_manifest_text(trusted=False)` — https-and-public endpoints, no ``DELETE``,
   bounded size and operation count, on top of every rule a file manifest already
   passes (the tool-name regex, `FORBIDDEN_PARAMS`, the header ban);
2. **a file always wins.** :func:`writable_name` refuses a name that
   `file_providers()` defines, and `providers()` overwrites a row with the file
   regardless — two enforcements in two modules, because only one of them is on
   the path a manifest arriving from the sync store takes;
3. **the credential is the real gate.** A manifest does nothing until someone
   attaches an account to it, and that has always been a human action. What the
   human lacked was the one fact worth knowing at that moment, which
   `Provider.credential_hosts()` now supplies: the hosts their key will be sent to;
4. **verification.** A manifest that has never answered a real request is marked
   unverified everywhere it is shown, rather than looking identical to one that
   works.

None of that makes a model-authored manifest as trustworthy as a reviewed file, and
`Provider.source` says which it is everywhere it is surfaced. That is the honest
position: this trades a review gate for a deploy gate, on purpose, and shows its
working instead of hiding it.
"""

from __future__ import annotations

import logging

from app.db import Store, get_store
from app.providers import (
    ManifestError,
    Provider,
    file_providers,
    load_manifest_text,
    reload_providers,
    set_stored_loader,
)

logger = logging.getLogger(__name__)


def writable_name(name: str) -> str:
    """The reason this provider name may not be stored, or empty if it may.

    A shipped file cannot be redefined by a row. `spotify.toml` and `github.toml`
    are reviewed code and the reference implementations of the format; a stored row
    claiming one of those names would silently change where an existing connection's
    credential is sent, on an account the user connected long before.
    """
    name = (name or "").strip().lower()
    if not name:
        return "A manifest needs a name."
    if name in file_providers():
        return (
            f"{name!r} is shipped with Bloom as a reviewed file and cannot be "
            "redefined by a stored manifest. If the shipped one is wrong, that is a "
            "code change; if you need different behaviour, choose another name."
        )
    return ""


def stored_providers(store: Store | None = None) -> dict[str, Provider]:
    """Every manifest in the database that still parses, by name.

    A row that no longer validates is logged and skipped rather than raised — the
    same tolerance `file_providers` applies, and it matters more here. These rows
    hold model output and arrive from other installs, so one bad manifest must cost
    exactly one provider and never the service's ability to start.
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
    """Detach the database source. Leaves file manifests working on their own."""
    set_stored_loader(None)


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
    "stored_providers",
    "uninstall_loader",
    "writable_name",
]
