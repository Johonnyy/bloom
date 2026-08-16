"""The builder's identity: a reserved slug, a seeded row, and one predicate.

Bloom's builder is a real row in ``agent_configs``, not an in-memory special case.
That is forced by the schema rather than chosen for elegance: ``runs.agent_config_id``
is a real column, `Store.list_all_runs` LEFT JOINs ``agent_configs`` for the slug,
and `/admin/agents/{id}/runs/{run_id}/trace` checks that a run belongs to an agent.
A synthetic id would give every build a ``null`` slug in the activity feed and 404
on the trace endpoint — which is the endpoint the whole feature reuses for free.

**Privilege is the slug, and nothing else.** `is_builder` is the single predicate
`app.runtime_service` keys off when it decides whether to register the tools that
write configurations and connections. Keeping it one line in one place is the point:
there is exactly one thing to grep for, and exactly one thing to get right.

Three independent things have to fail before a normal agent can reach those tools:

1. ``agent_configs.slug`` carries a ``UNIQUE`` index, so there cannot be a second
   row with this slug;
2. `app.admin.agents` refuses the reserved slug at create *and* at patch, so no
   caller can claim it through the API;
3. :func:`ensure_builder_config` runs in the lifespan before anything serves, so the
   row exists — and therefore the slug is taken — before the first request.

**The prompt is re-seeded from code on every boot.** It lives in git, is versioned
like any other code change, and is never editable through the API — a model that
could rewrite its own instructions is a different and much worse product than one
whose instructions are reviewed. What this install *is* allowed to choose is the
model keyword and the ceilings, so those are written once at creation and never
touched again.
"""

from __future__ import annotations

import logging

from app.builder.prompt import BUILDER_NAME, SYSTEM_PROMPT
from app.config import Settings, get_settings
from app.db import SlugTaken, Store

logger = logging.getLogger(__name__)

#: The one string privilege keys off. Reserved by the API validators.
BUILDER_SLUG = "bloom-builder"


def is_builder(config: dict | None) -> bool:
    """Whether this config row is Bloom's own builder."""
    return bool(config) and config.get("slug") == BUILDER_SLUG


def ensure_builder_config(store: Store, settings: Settings | None = None) -> dict:
    """Create or refresh the builder's row. Idempotent; safe on every boot.

    On first boot this creates the row with the configured keyword and ceilings. On
    every boot after, it rewrites ``name`` and ``system_prompt`` from code and leaves
    ``model_tier`` / ``max_steps`` / ``max_cost_usd`` exactly as they are — so an
    operator who re-pointed the builder at a cheaper model keeps that decision
    across restarts, while nobody can leave a stale prompt behind.

    A row that already exists under this slug is *adopted* rather than refused. It
    is harmless: privilege comes from the slug, the slug is reserved, and the prompt
    is about to be overwritten with the one from git anyway.
    """
    settings = settings or get_settings()

    existing = store.get_config_by_slug(BUILDER_SLUG)
    if existing is None:
        try:
            row = store.create_config(
                slug=BUILDER_SLUG,
                name=BUILDER_NAME,
                system_prompt=SYSTEM_PROMPT,
                model_tier=settings.builder_keyword,
                max_steps=settings.builder_max_steps,
                max_cost_usd=settings.builder_max_cost_usd,
            )
        except SlugTaken:
            # Raced with another process starting at the same moment. The row it
            # wrote is as good as the one we were about to write.
            row = store.get_config_by_slug(BUILDER_SLUG)  # type: ignore[assignment]
        logger.info("Seeded the builder config (%s)", BUILDER_SLUG)
        return row  # type: ignore[return-value]

    if existing["system_prompt"] != SYSTEM_PROMPT or existing["name"] != BUILDER_NAME:
        logger.info("Re-seeded the builder's prompt from code")
        return store.update_config(  # type: ignore[return-value]
            existing["id"], name=BUILDER_NAME, system_prompt=SYSTEM_PROMPT
        )
    return existing
