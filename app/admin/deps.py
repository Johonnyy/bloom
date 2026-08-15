"""Bearer auth for the management surface.

`agent_mcp.auth` is *imported*, not copied. sync-store deliberately mirrors that
module instead of depending on the library, for two reasons that do not apply here:
every app calls sync-store to register, so it must not inherit the MCP SDK's
availability, and depending on the library would invert the direction its own
clients point. Bloom depends on `agent-mcp-py` for its entire execution surface, so
a second copy of `verify_bearer` would be pure drift with nothing bought.

**Why a separate key set.** ``BLOOM_ADMIN_KEYS`` is not ``BLOOM_MCP_KEYS``. Aperture
edits configurations and reads run history; a peer agent calls ``run_task`` and
spends money. Giving a desktop GUI a token that also authorises task execution
means one leaked key buys both, and there is no cheaper time to separate them than
before either exists.

Both fail closed: no configured keys means every call is rejected, not that auth is
off.
"""

from __future__ import annotations

import logging

from agent_mcp.auth import parse_keys, verify_bearer
from fastapi import Header

from app.config import get_settings
from app.errors import ApiError

logger = logging.getLogger(__name__)


async def require_admin(authorization: str | None = Header(default=None)) -> str:
    """Authenticate an ``/admin`` caller, returning its name.

    The name is what gets logged — never the token. A key written ``name:token``
    identifies its holder directly; a bare token falls back to `agent_mcp`'s
    ``sha256:`` fingerprint, which correlates lines without being reversible.

    ``get_settings()`` is called here rather than captured at import so a test can
    clear the cache and have the next request see new keys.
    """
    settings = get_settings()
    result = verify_bearer(authorization, parse_keys(settings.admin_keys), allow_anonymous=False)
    if not result.ok:
        logger.warning("Rejected /admin call: %s", result.reason)
        raise ApiError(401, "unauthorized", result.reason or "invalid or missing bearer token")
    return result.caller
