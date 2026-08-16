"""Bloom's builder: the agent that writes other agents.

The place a brief — "a Spotify agent" — becomes a real configuration: a slug, a
prompt, a model keyword, and the connections it needs, plus the list of steps a human
must still take. It runs through `app.runtime_service.execute_run` like every other
agent, so it gets the run trace, the SSE stream and the activity feed for free.

Imports here are kept shallow deliberately. `app.admin.agents` needs only
`BUILDER_SLUG` and `is_builder` to reserve the slug, and pulling the tool set (and
through it `agent_runtime`, the provider registry and the research stack) into that
module just to ask a question about a string would be a circular import waiting to
happen. `builder_broker` and `start_build` are imported from their own modules by the
two places that actually run a build.
"""

from __future__ import annotations

from app.builder.agent import BUILDER_SLUG, ensure_builder_config, is_builder

__all__ = ["BUILDER_SLUG", "ensure_builder_config", "is_builder"]
