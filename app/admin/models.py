"""What the model keywords mean on this Bloom.

Read-only, deliberately. Amber owns editing — Aperture sends her a ``set_model``
frame, she writes her table and pushes to the shared sync store — and Bloom pulls.
Two writers on one shared table would need a conflict story that a single-person
ecosystem has no conflicts to justify.

It is still worth serving, because "what does `coding` mean *here*" is a real
question with a Bloom-specific answer: this deployment may not have a sync store
configured, or may not have reached it since it started, in which case an agent
configured as `coding` is running on Bloom's built-in default rather than on
whatever Amber currently thinks. ``overridden`` is what makes that visible.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import models
from app.admin.deps import require_admin
from app.config import get_settings

router = APIRouter(prefix="/admin", tags=["models"], dependencies=[Depends(require_admin)])


class KeywordOut(BaseModel):
    name: str
    model: str
    description: str
    builtin: bool
    # False means this is what Bloom shipped with. True means the shared table
    # re-pointed it — which is the difference between the fleet agreeing and this
    # box being on its own.
    overridden: bool


class KeywordsOut(BaseModel):
    keywords: list[KeywordOut]
    # Whether a shared table is configured at all. With no sync store the built-ins
    # are the whole truth, and a client should say so rather than implying drift.
    shared: bool
    default: str


@router.get("/models/keywords", response_model=KeywordsOut)
async def list_keywords() -> KeywordsOut:
    """The vocabulary an agent's ``model_tier`` may name, and where each points."""
    settings = get_settings()
    return KeywordsOut(
        keywords=[KeywordOut(**k) for k in models.keywords(settings)],
        shared=models.sync_enabled(settings),
        default=settings.default_tier,
    )
