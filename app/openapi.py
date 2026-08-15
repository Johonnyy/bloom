"""Dump the OpenAPI schema to stdout: ``python -m app.openapi``.

Aperture generates its HTTP client from ``docs/openapi.json`` **without running
Bloom**, so that file is checked in and CI fails when it drifts from the code. A
stale schema is worse than no schema: it produces a client that compiles, ships,
and disagrees with the server at runtime.

The MCP half is deliberately excluded — its routes are mounted by `agent_mcp` and
are not described by OpenAPI. MCP describes itself over the protocol, to a model;
this file describes the management surface, to a code generator. Two audiences,
two mechanisms.
"""

from __future__ import annotations

import json
import sys


def schema() -> dict:
    """Build the schema with MCP forced off.

    Otherwise generating it would require bearer keys and would open the database
    — `python -m app.openapi` must work in a clean CI checkout with no `.env`.
    """
    import os

    os.environ["BLOOM_FEATURE_MCP"] = "false"
    os.environ.setdefault("BLOOM_DB_PATH", ":memory:")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    return app.openapi()


def main() -> None:
    # sort_keys so the committed file has a stable diff: FastAPI's ordering
    # depends on import order, and a reordered-but-identical schema failing CI
    # would teach everyone to ignore that check.
    json.dump(schema(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
