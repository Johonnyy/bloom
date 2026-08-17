"""The builder's management surface: start a build, read one, tick its steps off.

Deliberately shaped like `app.admin.runs`. Starting a build answers **202 with the
id and a stream URL**, then runs in the background, for the reason that route
documents: a synchronous call could not hand you an id before the work ended, so
there would be nothing to attach a live panel to. Because a build *is* a run, the
stream and trace URLs point at the existing endpoints — Aperture reuses its trace
renderer and its SSE resumption with no new transport.

The checklist is served from the ``builds`` row rather than parsed out of the run's
result text, so it survives being read weeks later and can be ticked off one item at
a time. `done` is an index list rather than a flag per step because the steps
themselves are model output: rewriting them to record a tick would mean rewriting
what the model said.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.admin.deps import require_admin
from app.builder.service import UnknownAgent, edit_brief, resolve_edit_target, start_build
from app.config import get_settings
from app.db import get_store
from app.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["builder"], dependencies=[Depends(require_admin)])

# Held so the garbage collector cannot cancel a build mid-flight: asyncio keeps only
# a weak reference to a task, and a build that vanishes halfway leaves a `running`
# row and a stream that never terminates. Same reasoning as `app.admin.runs`.
_BACKGROUND: set[asyncio.Task] = set()


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(
        min_length=3,
        max_length=4000,
        description="Plain language: 'a Spotify agent that can play and search music'.",
    )


class EditIn(BaseModel):
    """A change to an agent that already exists.

    Separate from :class:`BuildIn` rather than a ``mode`` field on it, because the
    two carry different information: an edit must name its target, and a build must
    not. A single model would make ``agent_slug`` optional and push the "required
    when mode is edit" rule into a validator nobody reads.
    """

    model_config = ConfigDict(extra="forbid")

    agent_slug: str = Field(description="The agent to change, e.g. 'spotify-dj'.")
    change: str = Field(
        min_length=3,
        max_length=4000,
        description=(
            "Plain language: 'let it skip tracks', 'stop it answering questions about billing'."
        ),
    )


class BuildStarted(BaseModel):
    build_id: str
    run_id: str
    status: str
    mode: str = "build"
    # Relative paths, like test-run's. Join them to the base URL rather than using
    # them directly.
    stream_url: str
    trace_url: str


class SetupStep(BaseModel):
    kind: str
    title: str
    detail: str = ""
    url: str = ""
    connection_name: str = ""
    env_name: str = ""
    done_when: str = ""


class BuildOut(BaseModel):
    id: str
    run_id: str
    brief: str
    mode: str = "build"
    agent_config_id: str | None
    agent_slug: str
    status: str
    summary: str
    checklist: list[SetupStep]
    # What an edit actually altered, one line each. Empty for a build, whose
    # evidence of work is the agent it left behind.
    changes: list[str] = []
    done: list[int]
    error: str | None
    created_at: str
    updated_at: str


def _require_build(build_id: str) -> dict:
    row = get_store().get_build(build_id)
    if row is None:
        raise ApiError(404, "not_found", f"No build with id {build_id!r}.")
    return row


def _require_available() -> None:
    """Refuse before spending anything when the builder cannot work.

    Reported as 503 with the specific reason rather than a generic failure, because
    every cause is a setup problem a person can fix in a minute — and a build that
    starts, spends money and then reports it could not search is strictly worse.
    """
    reason = get_settings().builder_unavailable_reason()
    if reason:
        raise ApiError(503, "unavailable", reason)


@router.post("/builder/build", response_model=BuildStarted, status_code=202)
async def build(body: BuildIn, caller: str = Depends(require_admin)) -> BuildStarted:
    """Start a build and return where to watch it.

    202 rather than 200: a build takes minutes, and the id is what a live panel
    attaches to. There is no race — the trace stream replays from the beginning of
    the log before tailing, so connecting late loses nothing.
    """
    _require_available()
    store = get_store()

    # The row is created inside `start_build`, so its ids are read back from there
    # rather than minted twice. Awaiting only the creation would need a second
    # coroutine; instead the task is started and the ids are taken from the row it
    # writes first, which it commits before the model is called.
    from app.builder.agent import ensure_builder_config
    from app.db import new_id

    await asyncio.to_thread(ensure_builder_config, store, get_settings())
    build_id, run_id = new_id(), new_id()

    task = asyncio.create_task(
        _run_build(build_id, run_id, body.brief, caller),
        name=f"bloom-build-{build_id}",
    )
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    return BuildStarted(
        build_id=build_id,
        run_id=run_id,
        status="running",
        stream_url=f"/admin/runs/{run_id}/events",
        trace_url=f"/admin/builds/{build_id}",
    )


@router.post("/builder/edit", response_model=BuildStarted, status_code=202)
async def edit(body: EditIn, caller: str = Depends(require_admin)) -> BuildStarted:
    """Change an agent that already exists, using the same builder and the same trace.

    Shaped exactly like ``/builder/build`` — 202 and a stream URL — so Aperture
    reuses the build panel it already has rather than growing a second one.

    The slug is resolved *here*, synchronously, so an unknown agent is a 404 before
    anything is spent. Everything after that point costs a builder run.
    """
    _require_available()
    store = get_store()

    try:
        agent = await asyncio.to_thread(resolve_edit_target, body.agent_slug, store)
    except UnknownAgent as exc:
        raise ApiError(404, "not_found", str(exc)) from exc

    from app.builder.agent import ensure_builder_config
    from app.db import new_id

    await asyncio.to_thread(ensure_builder_config, store, get_settings())
    build_id, run_id = new_id(), new_id()

    task = asyncio.create_task(
        _run_build(build_id, run_id, edit_brief(agent["slug"], body.change), caller, agent),
        name=f"bloom-edit-{build_id}",
    )
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    return BuildStarted(
        build_id=build_id,
        run_id=run_id,
        status="running",
        mode="edit",
        stream_url=f"/admin/runs/{run_id}/events",
        trace_url=f"/admin/builds/{build_id}",
    )


async def _run_build(
    build_id: str, run_id: str, brief: str, caller: str, agent: dict | None = None
) -> None:
    """Body of the background task. Every failure is already recorded on the row."""
    try:
        await start_build(
            brief,
            origin="edit" if agent else "build",
            mode="edit" if agent else "build",
            agent=agent,
            caller=caller,
            build_id=build_id,
            run_id=run_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — start_build records it; this is the backstop
        logger.exception("Build %s failed outside start_build", build_id)


@router.get("/builds", response_model=list[BuildOut])
async def list_builds(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="running | needs_setup | ready | failed"),
    mode: str | None = Query(None, description="build | edit"),
) -> list[BuildOut]:
    """Builds, newest first. Filter by status to find what still needs a credential.

    ``mode='edit'`` is an agent's change history — what has been altered since it was
    built, and by which run.
    """
    rows = await asyncio.to_thread(
        get_store().list_builds, limit=limit, offset=offset, status=status, mode=mode
    )
    return [BuildOut(**r) for r in rows]


@router.get("/builds/{build_id}", response_model=BuildOut)
async def get_build(build_id: str) -> BuildOut:
    row = await asyncio.to_thread(_require_build, build_id)
    return BuildOut(**row)


@router.post("/builds/{build_id}/steps/{index}/done", response_model=BuildOut)
async def mark_step_done(build_id: str, index: int) -> BuildOut:
    """Tick one step off. Idempotent — ticking twice is not an error."""
    row = await asyncio.to_thread(_require_build, build_id)
    if index < 0 or index >= len(row["checklist"]):
        raise ApiError(
            404,
            "not_found",
            f"This build has {len(row['checklist'])} step(s); there is no step {index}.",
        )
    done = sorted({*row["done"], index})
    updated = await asyncio.to_thread(get_store().update_build, build_id, done=done)
    return BuildOut(**updated)  # type: ignore[arg-type]


@router.delete("/builds/{build_id}", status_code=204)
async def delete_build(build_id: str) -> None:
    """Forget a build. **The agent it created, and its run history, survive.**

    Deleting the record of how something was made is not deleting the thing — the
    same posture as deleting an agent leaving its connections alone.
    """
    await asyncio.to_thread(_require_build, build_id)
    await asyncio.to_thread(get_store().delete_build, build_id)
    logger.info("Deleted build %s", build_id)
