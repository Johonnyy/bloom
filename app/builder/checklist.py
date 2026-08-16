"""The setup checklist: what a human must still do, in a shape a GUI can act on.

The builder cannot finish the job. Registering an OAuth application is a person at a
developer portal, and pasting an API key is a person with a password manager — so
the last thing every build produces is a list of the steps left.

**The steps are typed rather than prose**, and that is the whole design. A paragraph
saying "go and connect Spotify" is something a user has to translate into clicks; a
step of kind ``connect_oauth`` naming a connection is something Aperture renders as
the Connect button it already has. Typing them is what turns the checklist from a
report into a control surface.

**An unrecognised kind is coerced to ``manual``, never rejected.** A model will
eventually invent one, and the choice is between losing the step entirely and losing
only its button. Losing the button is obviously better — the title and detail still
tell the user what to do. Aperture's ``toSetupSteps`` performs the identical coercion
so the two ends cannot disagree about what a stored checklist means.

Everything here is pure and offline, so the coercion rules can be tested exhaustively.
"""

from __future__ import annotations

from typing import Any

#: What a step can ask for. Each maps to something Aperture can render as an action.
#:
#: * ``register_oauth_app`` — open a developer portal and create an application
#: * ``set_client_credentials`` — paste that application's client id and secret
#: * ``connect_oauth`` — press Connect and complete the browser flow
#: * ``paste_api_key`` — paste a static key for an api_key connection
#: * ``set_env`` — set an environment variable on the server and restart
#: * ``manual`` — anything else, rendered as text
STEP_KINDS = (
    "register_oauth_app",
    "set_client_credentials",
    "connect_oauth",
    "paste_api_key",
    "set_env",
    "manual",
)

FALLBACK_KIND = "manual"

#: Bounds on model output. Generous enough never to bite a real checklist, small
#: enough that a runaway generation cannot fill the database or a rendered page.
MAX_STEPS = 20
MAX_TITLE = 200
MAX_DETAIL = 2000
MAX_SUMMARY = 4000


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if value is not None else ""


def normalise_step(raw: Any) -> dict | None:
    """One validated step, or ``None`` if there is nothing usable in it.

    A step with no title is dropped: the title is the only part a user reads before
    deciding what to do, and a button labelled with nothing is worse than absent.
    """
    if not isinstance(raw, dict):
        return None

    title = _text(raw.get("title"), MAX_TITLE)
    if not title:
        return None

    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in STEP_KINDS:
        kind = FALLBACK_KIND

    step = {
        "kind": kind,
        "title": title,
        "detail": _text(raw.get("detail"), MAX_DETAIL),
        "url": _text(raw.get("url"), 500),
        "connection_name": _text(raw.get("connection_name"), 64),
        "env_name": _text(raw.get("env_name"), 128),
        "done_when": _text(raw.get("done_when"), MAX_TITLE),
    }
    # A url that is not http(s) would render as a link to nowhere, or worse as a
    # `javascript:` href in a renderer that trusted it.
    if step["url"] and not step["url"].lower().startswith(("http://", "https://")):
        step["url"] = ""
    return step


def normalise_steps(raw: Any) -> list[dict]:
    """Validate and bound a whole checklist. Never raises."""
    if not isinstance(raw, list):
        return []
    steps = [s for s in (normalise_step(item) for item in raw) if s is not None]
    return steps[:MAX_STEPS]


def render(summary: str, steps: list[dict], *, agent_slug: str = "") -> str:
    """The checklist as plain text, for a caller that cannot render a button.

    Amber gets this. A model cannot click anything, so the affordances are useless to
    it and the prose is the whole point — it reads these steps back to the user.
    """
    lines: list[str] = []
    if agent_slug:
        lines.append(f"Agent: {agent_slug}")
    if summary:
        lines.append(summary.strip())
    if steps:
        lines.append("")
        lines.append("To make it work:")
        for index, step in enumerate(steps, start=1):
            line = f"{index}. {step.get('title', '')}"
            detail = step.get("detail") or ""
            url = step.get("url") or ""
            if detail:
                line += f" — {detail}"
            if url:
                line += f" ({url})"
            lines.append(line)
    return "\n".join(lines).strip()
