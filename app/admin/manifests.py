"""Provider manifests as a management surface: read one, fix one, throw one away.

**This router is the reason storing manifests is acceptable at all.** A definition
written by a model will sometimes be wrong — an operation with the wrong path, a
scope string that does not exist, a description that makes the agent choose badly.
If the only way to correct that were editing a TOML file in the repository and
redeploying, then dynamic manifests would have moved the problem rather than solved
it: you would have traded "open the editor to add a provider" for "open the editor
to fix one", which is the same editor.

So the whole lifecycle is here. ``PUT`` in particular is not a convenience — it is
the acceptance criterion. Correcting a manifest has to be a form.

Three shapes are deliberate:

* **``PUT`` takes raw TOML and validates it exactly as the builder's write does**,
  through `app.manifests.save`. One validation path, so a manifest a human fixes
  cannot bypass a rule the model's had to satisfy, and a 422 carries the parser's
  own message — which names the line.
* **``PUT`` clears the verified mark**, because the proof belonged to the previous
  text. Editing an operation and inheriting the old manifest's tick would be the
  one way a broken provider could look proven.
* **``DELETE`` leaves connections alone.** A credential the user pasted is theirs,
  and a wrong definition should not cost them the account. The connection's tools
  simply stop being registered until a manifest for that name exists again, which
  is what makes "delete it and let the builder try again" a safe recovery.

Every manifest here is writable. Bloom used to ship two as reviewed files that no
row could redefine, which made a gap in one of them a pull request; see
`app.manifests`. Nothing is read-only any more.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app import manifests as manifest_store
from app.admin.deps import require_admin
from app.db import get_store
from app.errors import ApiError
from app.providers import ManifestError, providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["manifests"], dependencies=[Depends(require_admin)])


class ManifestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    toml: str = Field(
        min_length=10,
        max_length=64_000,
        description="The complete manifest. Replaces what is stored; never merged.",
    )


class OperationOut(BaseModel):
    """One operation, flattened for display. The TOML remains the source of truth."""

    name: str
    tool_name: str
    method: str
    path: str
    description: str
    read_only: bool
    scopes: list[str]


class ManifestOut(BaseModel):
    name: str
    display_name: str
    # stored | shared | seed. All three are editable; none has been reviewed.
    source: str
    editable: bool
    # Whether a human has read this definition. Always false now that nothing is
    # shipped as reviewed code; kept so a UI states it next to the credential form
    # rather than implying review by saying nothing.
    reviewed: bool
    verified_at: str | None = None
    verified_note: str = ""
    # Every host a credential for this provider would be sent to, api_base first.
    # The one fact worth showing someone about to paste a key.
    credential_hosts: list[str]
    auth_methods: list[str]
    api_base: str
    docs_url: str = ""
    scopes_default: list[str]
    allow_request: bool
    operations: list[OperationOut]
    # Every manifest has one now: there is no definition whose text lives in git.
    toml: str | None = None
    run_id: str = ""
    created_at: str = ""
    updated_at: str = ""


def _out(name: str) -> ManifestOut:
    """Project one provider, merging the live definition with its stored row."""
    provider = providers().get(name)
    if provider is None:
        raise ApiError(404, "not_found", f"No provider manifest named {name!r}.")
    row = get_store().get_manifest(name) or {}
    return ManifestOut(
        name=provider.name,
        display_name=provider.display_name,
        source=provider.source,
        editable=not provider.reviewed,
        reviewed=provider.reviewed,
        verified_at=row.get("verified_at"),
        verified_note=row.get("verified_note", ""),
        credential_hosts=list(provider.credential_hosts()),
        auth_methods=list(provider.auth_methods),
        api_base=provider.api_base,
        docs_url=provider.docs_url,
        scopes_default=list(provider.scopes_default),
        allow_request=provider.allow_request,
        operations=[
            OperationOut(
                name=op.name,
                tool_name=op.tool_name(provider.name),
                method=op.method,
                path=op.path,
                description=op.description,
                read_only=op.read_only,
                scopes=list(op.scopes),
            )
            for op in provider.operations
        ],
        toml=row.get("toml"),
        run_id=row.get("run_id", ""),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
    )


@router.get("/manifests", response_model=list[ManifestOut])
async def list_manifests() -> list[ManifestOut]:
    """Every provider this Bloom can reach, shipped and stored alike.

    One list rather than two, because "which services can I connect" is the question
    being asked and where a definition came from is an attribute of the answer, not
    a reason to split it. `source` and `editable` carry that distinction.
    """
    return [_out(name) for name in sorted(providers())]


@router.get("/manifests/{name}", response_model=ManifestOut)
async def get_manifest(name: str) -> ManifestOut:
    return _out(name.strip().lower())


@router.put("/manifests/{name}", response_model=ManifestOut)
async def put_manifest(name: str, body: ManifestIn) -> ManifestOut:
    """Create or correct a stored manifest.

    The route that means a wrong operation is fixed in Aperture rather than in the
    repository. Validation is `app.manifests.save`'s — the same one the builder's
    tool goes through — so a 422 here means the manifest would have been refused
    from either direction, and its message names what to change.
    """
    name = name.strip().lower()
    refusal = manifest_store.writable_name(name)
    if refusal:
        raise ApiError(409, "conflict", refusal)
    try:
        await asyncio.to_thread(manifest_store.save, name=name, toml=body.toml, source="stored")
    except ManifestError as exc:
        raise ApiError(422, "unprocessable", str(exc)) from exc
    logger.info("Manifest %s written through the admin API", name)
    return _out(name)


@router.delete("/manifests/{name}", status_code=204)
async def delete_manifest(name: str) -> None:
    """Forget a stored manifest. **Connections using it survive, with their credentials.**

    The same posture as deleting an agent leaving its connections alone: deleting a
    definition is not deleting the account someone connected through it. Their tools
    stop being registered until a manifest for the name exists again, which makes
    this a safe way to let the builder retry rather than a destructive one.
    """
    name = name.strip().lower()
    if not await asyncio.to_thread(manifest_store.delete, name):
        raise ApiError(404, "not_found", f"No stored manifest named {name!r}.")
