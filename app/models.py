"""Which model an agent runs on — named by keyword, shared across the ecosystem.

`agent_runtime.model_router` offers three names: ``cheap`` / ``balanced`` /
``strong``. That is a capability *ladder*, and a ladder can only say "better" — it
has no way to say "for code" or "for a long document", which is how a model is
actually chosen. Amber solved this in `amber_v2/app/models.py`; this module is
Bloom's half of the same vocabulary, so an agent Bloom builds can be pointed at
``coding`` and mean the same model Amber means by it.

**Pull-only, deliberately.** Amber owns editing — Aperture sends her a ``set_model``
frame, she writes her table and pushes to the sync store. Bloom reads. Giving two
services write access to one shared table would need a merge story for a
single-person ecosystem that has no conflicts worth merging, and Bloom has no UI of
its own to edit from anyway.

Three layers, in resolution order:

1. a **literal model id** — anything containing ``/`` passes straight through, the
   same escape hatch `model_router` offers;
2. the **shared overrides** pulled from ``GET {sync_store_url}/models``;
3. the **built-in defaults** below, and past those `agent_runtime`'s own table.

The shared table carries *overrides only*: a keyword absent from it is not
undefined, it means nobody has re-pointed it, and each app falls back to what it
shipped with. That is the sync store's own documented contract
(``amber-infra/sync-store/app/main.py``) and the reason this file ships a full table
rather than trusting the network for the vocabulary.

**Resolution never raises.** It runs immediately before a model call, where the cost
of guessing wrong is one reply from the wrong model and the cost of raising is no
reply at all. An unreachable store degrades to "no overrides", never to a failed run.

**About the defaults.** Every keyword ships pointing at one of the three ids this
ecosystem has verified are real, so several start out identical. That is deliberate
rather than lazy: a table of plausible-looking ids invented at authoring time is
worse than useless, because a wrong id fails at request time with nothing to say
why. The keywords are the vocabulary; pointing them at the models *you* rate is the
feature, and Aperture is where you do it.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The ids this ecosystem has verified. `agent_runtime.model_router`'s own table and
# its fallback price list are built from exactly these three, and Amber's
# `app/models.py` names them identically — keep the three files in agreement.
_FAST = "anthropic/claude-haiku-4.5"
_CHEAP = "meta-llama/llama-3.1-8b-instruct"
_STRONG = "anthropic/claude-sonnet-4.6"


@dataclass(frozen=True, slots=True)
class Keyword:
    """One way of describing the model you want, and where it currently points."""

    name: str
    description: str
    model: str


# The vocabulary. Order is display order — the ladder first, then the axes, which is
# how a picker should read it. This list mirrors Amber's `BUILTIN`; if the two drift,
# a keyword means different things in different halves of one ecosystem.
BUILTIN: tuple[Keyword, ...] = (
    Keyword("fast", "First word out quickest. What a spoken turn actually notices.", _FAST),
    Keyword(
        "cheap",
        "The least you can spend and still hold a conversation. Bulk and background work.",
        _CHEAP,
    ),
    Keyword("balanced", "The everyday all-rounder, and the default.", _FAST),
    Keyword("strong", "Best general quality, at more latency and more money.", _STRONG),
    Keyword("coding", "Writing, reading and fixing code.", _STRONG),
    Keyword("reasoning", "Multi-step problems worth thinking slowly about.", _STRONG),
    Keyword("writing", "Long-form prose — tone, structure, keeping a voice.", _STRONG),
    Keyword("research", "Long tool-using chains: search, read, cross-check, summarise.", _STRONG),
    Keyword("vision", "Turns with an image in them.", _STRONG),
    Keyword("long", "Very large inputs — a whole document, a whole transcript.", _STRONG),
)

BUILTIN_MODELS: dict[str, str] = {k.name: k.model for k in BUILTIN}
_DESCRIPTIONS: dict[str, str] = {k.name: k.description for k in BUILTIN}

# A keyword is a bare word: lowercase, no slash (which would make it ambiguous with
# a model id) and short enough to be a label in a picker. Same rule as Amber's, so a
# keyword she accepts is one Bloom accepts.
_KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# A model id is ``vendor/model``. Requiring the slash is what makes rule 1 of the
# resolution order unambiguous, and it rejects the commonest mistake — pasting a
# keyword into a model field — before it reaches OpenRouter as a 400.
_MODEL_RE = re.compile(r"^[^\s/]+/[^\s]{1,120}$")


def valid_keyword(name: Any) -> bool:
    return isinstance(name, str) and bool(_KEYWORD_RE.match(name.strip().lower()))


def valid_model(model: Any) -> bool:
    return isinstance(model, str) and bool(_MODEL_RE.match(model.strip()))


class _Catalogue:
    """The shared overrides, fetched once and kept in memory.

    Resolution happens on every run immediately before the model call, so it must
    not touch the network — hence the cache, refreshed by the background pass in
    `refresh` rather than lazily on the latency path.

    A store that cannot be reached degrades to "no overrides" rather than failing.
    Losing a customisation is recoverable and visible; refusing to run is neither.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, dict] = {}
        self._fetched_at: str = ""
        self._lock = threading.Lock()

    def overrides(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._overrides.items()}

    def replace(self, rows: dict[str, dict]) -> int:
        """Swap in a freshly pulled table. Returns how many entries were kept."""
        clean = {
            name: dict(row)
            for name, row in rows.items()
            if valid_keyword(name) and valid_model((row or {}).get("model"))
        }
        with self._lock:
            self._overrides = clean
        return len(clean)

    def clear(self) -> None:
        """Forget the overrides. For tests, and for a store that was unconfigured."""
        with self._lock:
            self._overrides = {}


_catalogue = _Catalogue()


def invalidate_cache() -> None:
    """Drop the cached overrides. Built-ins are unaffected."""
    _catalogue.clear()


def sync_enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).mcp_sync_store_url.strip())


def keyword_models(settings: Settings | None = None) -> dict[str, str]:
    """Every keyword this install knows, mapped to the model it currently means."""
    models = dict(BUILTIN_MODELS)
    models.update({k: v["model"] for k, v in _catalogue.overrides().items() if v.get("model")})
    return models


def keywords(settings: Settings | None = None) -> list[dict]:
    """The vocabulary as a client should show it: name, model, description, origin.

    ``overridden`` is what makes this worth returning over a bare mapping — it is the
    difference between "this is what Bloom shipped with" and "somebody re-pointed
    this", which is the first question anyone asks of a table like this.
    """
    overrides = _catalogue.overrides()
    names = list(BUILTIN_MODELS) + [n for n in sorted(overrides) if n not in BUILTIN_MODELS]
    resolved = keyword_models(settings)
    return [
        {
            "name": name,
            "model": resolved.get(name, ""),
            "description": (
                overrides.get(name, {}).get("description") or _DESCRIPTIONS.get(name, "")
            ),
            "builtin": name in BUILTIN_MODELS,
            "overridden": name in overrides,
        }
        for name in names
    ]


def known(name: Any, settings: Settings | None = None) -> bool:
    """Whether this string is something `resolve` can turn into a real model id.

    Used by the admin API to reject a typo at write time. Note it accepts a literal
    ``vendor/model`` as well as a keyword: both are legitimate values for a config's
    ``model_tier``, and refusing the escape hatch would make pinning one agent to a
    model released this morning impossible.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    value = name.strip()
    if "/" in value:
        return bool(_MODEL_RE.match(value))
    if value.lower() in keyword_models(settings):
        return True
    try:
        from agent_runtime.model_router import TIERS

        return value.lower() in TIERS
    except Exception:  # noqa: BLE001 — the library not being importable is not a typo
        return False


def resolve(keyword: str | None, settings: Settings | None = None) -> str:
    """Return the concrete model id a keyword means right now.

    ``None`` or empty means the install default. A value containing ``/`` is already
    a model id and is returned untouched. An unrecognised keyword falls back — to the
    install default, and past that to ``balanced`` — with a warning, never an
    exception.

    Bloom resolves here rather than handing the keyword to `AgentRunner`, because
    `agent_runtime.model_router` has never heard of ``coding`` and would raise
    `UnknownTier` on it.
    """
    settings = settings or get_settings()
    name = (keyword or "").strip() or settings.default_tier.strip()

    if "/" in name:
        return name

    models = keyword_models(settings)
    resolved = models.get(name.lower())
    if resolved:
        return resolved

    # Not ours — but `agent_runtime`'s table may still know it, and a tier name
    # configured before this module existed should keep working.
    try:
        from agent_runtime.model_router import resolve as runtime_resolve

        return runtime_resolve(name)
    except Exception:  # noqa: BLE001 — unknown there too; fall through to the default
        pass

    fallback = models.get(settings.default_tier.strip().lower()) or BUILTIN_MODELS["balanced"]
    logger.warning(
        "Unknown model keyword %r; falling back to %s. Known keywords: %s",
        name,
        fallback,
        ", ".join(sorted(models)),
    )
    return fallback


# --- pulling the shared table -------------------------------------------------

TIMEOUT_S = 5.0


def _endpoint(settings: Settings) -> str:
    return f"{settings.mcp_sync_store_url.strip().rstrip('/')}/models"


def _headers(settings: Settings) -> dict[str, str]:
    token = settings.mcp_sync_store_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def refresh(settings: Settings | None = None, *, client_factory: Any = None) -> int:
    """Pull the shared keyword table into the cache. Returns how many were kept.

    Never raises. Every failure is logged and leaves the previous cache in place —
    a store that goes down must not silently re-point every keyword back to its
    built-in default mid-flight.

    ``client_factory`` exists so a test can drive this without a network.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return 0

    import httpx2

    factory = client_factory or httpx2.AsyncClient
    try:
        async with factory() as client:
            response = await client.get(
                _endpoint(settings), headers=_headers(settings), timeout=TIMEOUT_S
            )
        if response.status_code >= 400:
            logger.warning("Model keyword sync: store answered HTTP %s", response.status_code)
            return len(_catalogue.overrides())
        payload = response.json()
    except Exception:  # noqa: BLE001 — an unreachable store is not a failed run
        logger.warning("Model keyword sync failed; keeping the current table", exc_info=True)
        return len(_catalogue.overrides())

    rows = (payload or {}).get("keywords")
    if not isinstance(rows, dict):
        logger.warning("Model keyword sync: unexpected payload shape; ignoring")
        return len(_catalogue.overrides())

    kept = _catalogue.replace(rows)
    logger.info("Model keyword sync: %d override(s) in effect", kept)
    return kept


async def _sync_loop(settings: Settings) -> None:
    """Pull the shared table at startup and then on an interval. Never raises."""
    import asyncio

    while True:
        await refresh(settings)
        try:
            await asyncio.sleep(max(30.0, settings.model_sync_interval_s))
        except asyncio.CancelledError:
            raise


def start_keyword_sync(settings: Settings | None = None):
    """Start the background pull, or return ``None`` when no store is configured.

    A task rather than a fetch-on-read: resolution happens immediately before a model
    call, and putting an HTTP round trip there would add the store's latency — and
    its outages — to every run.
    """
    import asyncio

    settings = settings or get_settings()
    if not sync_enabled(settings):
        logger.info("Model keyword sync disabled (no BLOOM_MCP_SYNC_STORE_URL)")
        return None
    return asyncio.create_task(_sync_loop(settings), name="bloom-model-keywords")


async def stop_keyword_sync(task) -> None:
    """Cancel the background pull, tolerating ``None``."""
    if task is None:
        return
    import asyncio

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown must not raise
        pass
