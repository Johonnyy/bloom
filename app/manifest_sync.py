"""Sharing provider manifests through the ecosystem's sync store.

The same shape as `app.models`' keyword sync, one level up, and for the same
reason: a manifest written on one install is work every other install would
otherwise repeat — the same research, the same model spend, to arrive at the same
TOML. Push what this box wrote; pull what other boxes wrote.

Three properties are deliberate and worth not softening:

**Local always wins, and a pull never overwrites a manifest this install edited.**
The whole point of `PUT /admin/manifests/{name}` is that a wrong operation is fixed
here, now; a background pass that quietly restored the broken shared copy an hour
later would make that fix a lie. A pulled manifest is written only when there is no
local row for that name.

**Local always wins**, and that is now the only precedence rule there is. Bloom
used to also refuse any shared manifest whose name matched a shipped file; there
are no shipped files, so a name is simply taken or it is not. A shared manifest
cannot redefine an existing connection's provider because a local row for that
name already exists — which is the same protection, resting on a fact about this
install rather than on a fact about the repo.

**Pulled manifests are validated exactly as locally-written ones are.** They arrive
with ``trusted=False``, through the same loader, because the sync store deliberately
does not parse what it holds and everything in it was written by somebody's model.
A shared manifest is not more trustworthy for having travelled.

Verification does not travel as proof. The store records that *some* install proved
a manifest against the real API, which is useful advice, and a pulled row still
starts unverified locally until this install's own credential probes successfully.
A manifest that worked against another account is evidence, not proof.

With no ``BLOOM_MCP_SYNC_STORE_URL`` the whole module is inert and manifests are
simply this install's own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import Settings, get_settings
from app.db import Store, get_store
from app.manifests import save, writable_name
from app.providers import ManifestError

logger = logging.getLogger(__name__)

TIMEOUT_S = 10.0


def sync_enabled(settings: Settings | None = None) -> bool:
    """Both a store to talk to and permission to talk to it."""
    settings = settings or get_settings()
    return settings.feature_manifest_sync and bool(settings.mcp_sync_store_url.strip())


def _endpoint(settings: Settings, path: str = "") -> str:
    base = settings.mcp_sync_store_url.strip().rstrip("/")
    return f"{base}/manifests{path}"


def _headers(settings: Settings) -> dict[str, str]:
    token = settings.mcp_sync_store_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def push(
    name: str,
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    client_factory: Any = None,
) -> bool:
    """Publish one locally-written manifest. Returns whether the store took it.

    Never raises. A push that fails is logged and forgotten — the manifest already
    works on this install, and the periodic pass will offer it again. Sharing is the
    optimisation; having it locally is the feature.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return False
    store = store or get_store()

    row = await asyncio.to_thread(store.get_manifest, name)
    if row is None:
        return False

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    try:
        async with factory() as client:
            response = await client.put(
                _endpoint(settings, f"/{name}"),
                headers=_headers(settings),
                json={"toml": row["toml"], "verified": bool(row["verified_at"])},
                timeout=TIMEOUT_S,
            )
    except Exception:  # noqa: BLE001 — an unreachable store is not a failed build
        logger.warning("Could not publish manifest %s; it still works here", name, exc_info=True)
        return False

    if response.status_code >= 400:
        logger.warning("Manifest %s rejected by the store: HTTP %s", name, response.status_code)
        return False
    logger.info("Published manifest %s to the sync store", name)
    return True


async def push_all(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    client_factory: Any = None,
) -> int:
    """Publish every manifest this install wrote. Returns how many the store took.

    Only ``source='stored'`` rows: re-publishing something pulled from the store back
    to the store is a no-op at best, and at worst it stamps this install's name on
    another's work in ``updated_by``.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return 0
    store = store or get_store()

    rows = await asyncio.to_thread(store.list_manifests)
    sent = 0
    for row in rows:
        if row["source"] != "stored":
            continue
        if await push(row["name"], settings=settings, store=store, client_factory=client_factory):
            sent += 1
    return sent


async def pull(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    client_factory: Any = None,
) -> int:
    """Fetch shared manifests and store the ones this install does not have.

    Returns how many were newly adopted. Never raises: a store that is down leaves
    every local manifest exactly as it was, which is the only acceptable failure
    mode for something on no critical path.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return 0
    store = store or get_store()

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    try:
        async with factory() as client:
            response = await client.get(
                _endpoint(settings), headers=_headers(settings), timeout=TIMEOUT_S
            )
        if response.status_code >= 400:
            logger.warning("Manifest sync: store answered HTTP %s", response.status_code)
            return 0
        payload = response.json()
    except Exception:  # noqa: BLE001 — an unreachable store is not a failure here
        logger.warning("Manifest sync failed; keeping what is stored locally", exc_info=True)
        return 0

    shared = (payload or {}).get("manifests")
    if not isinstance(shared, dict):
        logger.warning("Manifest sync: unexpected payload shape; ignoring")
        return 0

    existing = {row["name"] for row in await asyncio.to_thread(store.list_manifests)}
    adopted = 0

    for name, record in shared.items():
        if not isinstance(record, dict):
            continue
        # Local wins. A background pass must never undo an edit somebody made in
        # Aperture — that route exists precisely so a wrong manifest is fixable, and
        # restoring the broken shared copy an hour later would make the fix a lie.
        if name in existing:
            continue
        if writable_name(name):
            continue

        toml = record.get("toml")
        if not isinstance(toml, str) or not toml.strip():
            continue
        try:
            # trusted=False, like anything else a model wrote. Travelling through the
            # store confers nothing — it does not parse what it holds.
            await asyncio.to_thread(save, name=name, toml=toml, source="shared", store=store)
        except ManifestError as exc:
            logger.warning("Shared manifest %s rejected locally: %s", name, exc)
            continue
        adopted += 1
        logger.info("Adopted shared provider manifest %s", name)

    if adopted:
        logger.info("Manifest sync: adopted %d new provider(s)", adopted)
    return adopted


async def reconcile(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    client_factory: Any = None,
) -> tuple[int, int]:
    """One full pass: publish what is ours, adopt what is not. ``(pushed, pulled)``.

    Push first. A box that has just written a manifest should share it before it
    starts consuming, so that two installs building the same provider at the same
    time converge on one of them rather than both waiting.
    """
    pushed = await push_all(settings=settings, store=store, client_factory=client_factory)
    pulled = await pull(settings=settings, store=store, client_factory=client_factory)
    return pushed, pulled


async def _sync_loop(settings: Settings) -> None:
    """Reconcile at startup and then on an interval. Never raises."""
    while True:
        try:
            await reconcile(settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any single bad pass
            logger.exception("Manifest sync pass failed")
        await asyncio.sleep(max(60.0, settings.manifest_sync_interval_s))


def start_manifest_sync(settings: Settings | None = None):
    """Start the background pass, or return ``None`` when no store is configured."""
    settings = settings or get_settings()
    if not sync_enabled(settings):
        logger.info("Manifest sync disabled (no BLOOM_MCP_SYNC_STORE_URL)")
        return None
    return asyncio.create_task(_sync_loop(settings), name="bloom-manifest-sync")


async def stop_manifest_sync(task) -> None:
    """Cancel the background pass, tolerating ``None``."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = [
    "pull",
    "push",
    "push_all",
    "reconcile",
    "start_manifest_sync",
    "stop_manifest_sync",
    "sync_enabled",
]
