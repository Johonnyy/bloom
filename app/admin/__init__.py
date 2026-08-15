"""The management surface: plain REST for a GUI, not MCP.

MCP is for agent-to-agent traffic — one tool, ``run_task``, described well enough
for a model to choose it. This half is CRUD over configuration, consumed by
Aperture, where a hand-written HTTP client and an OpenAPI schema are simpler than
a protocol built for tool selection.
"""
