"""SQLite storage for everything Bloom owns.

Follows the ecosystem's house pattern (`agent_mcp.usage_log`,
`amber-infra/sync-store/app/db.py`, `amber_v2/app/memory/store.py`): a module-level
``_SCHEMA`` of ``CREATE TABLE IF NOT EXISTS`` applied with ``executescript``, one
connection with ``check_same_thread=False`` serialized under a ``threading.Lock``,
rows out as plain dicts, and ISO-8601 UTC-seconds TEXT timestamps — never epoch
floats. Every method here is synchronous; callers wrap each in
``asyncio.to_thread`` so a write never blocks the event loop.

**Three tenants, one file.** ``bloom.db`` also holds ``agent_mcp_usage`` (the tool
log) and ``agent_runtime_usage`` (model spend), written by the two embedded
libraries through their *own* connections. That is deliberate: the three tables
share ``conversation_id`` / ``app_name`` / ``depth`` / ``created_at``, so what a run
cost can be joined to the calls that caused it. It is also why WAL and a busy
timeout are not optional here — WAL removes reader/writer blocking, not
writer/writer, and three writers on one file without a timeout is an intermittent
``database is locked`` in the middle of a model loop.

Storage decisions worth the words:

* **``config_json`` and ``scopes_json`` are TEXT, not child tables.** Bloom never
  queries inside them; it stores what was configured and projects on read.
* **``agent_connections`` is the only edge between an agent and a connection.**
  There used to be two — a binding table *and* an ``agent_config_id`` ownership
  column on the connection — and the pair was the reason a connection could not
  really be shared: the OAuth exchange always stamped an owner, so "shared" was a
  flag nothing could set. A connection is now a global library entry. Deleting an
  agent deletes its bindings and no credentials.
* **A connection's ``name`` is a tool namespace when ``kind='mcp'``.** `MCPClient`
  prefixes remote tools ``<server>__<tool>``, so the name is validated the same way
  a manifest's provider name is. Provider-backed kinds keep prefixing their tools
  with the *provider*, because that name is checked when the manifest loads and
  moving the check to run time would lose it.
* **``run_events`` is append-only with an autoincrement id.** That id is the SSE
  cursor a client resumes from, so it must be monotonic across the whole table and
  never reused. A crashed process therefore still leaves a readable partial trace.
* **Secret columns are excluded from every read projection.** `_connection_row`
  never returns ``secret``/``refresh_token``/``client_secret``; the one caller that
  needs the bytes asks for them by a method that says so. Note that sync-store's
  equivalent *does* return its ``token`` — that is a peer credential an admin sets,
  not a user's grant, and the shape should not be copied here.
* **There is no migration runner, and this file will refuse a database written
  before connections existed.** Bloom has never been deployed, so the honest
  handling of an old dev database is to say what it is and where the delete button
  is — see :class:`SchemaTooOld`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SlugTaken(ValueError):
    """Raised when an agent config is created with a slug already in use."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"An agent config with slug {slug!r} already exists.")
        self.slug = slug


class ConnectionNameTaken(ValueError):
    """Raised when a connection is created with a name already in use.

    Names are unique across every kind, not merely within one. For ``kind='mcp'``
    the name is a tool namespace and a duplicate would make two servers' tools
    indistinguishable; for the rest it is the handle the API and the UI use, and
    one meaning per name is worth more than the freedom to repeat them.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"A connection named {name!r} already exists.")
        self.name = name


class SchemaTooOld(RuntimeError):
    """The database predates connections and cannot be read.

    Raised at :class:`Store` construction rather than from whichever route first
    hits a missing column, for the same reason `app.crypto.assert_usable` runs in
    the lifespan: a configuration problem should fail next to the configuration,
    at boot, with the fix in the message.

    Bloom has never been deployed, so there is nothing to preserve and no migration
    to write. The only databases that can be in this state are development ones.
    """


def _reject_legacy_schema(conn: sqlite3.Connection, path: str) -> None:
    """Refuse a database still shaped for ``mcp_servers`` and ``oauth_connections``.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing table, so without
    this an old file loads happily and then fails deep inside a route: ``SELECT *``
    hands ``_config_row`` an ``mcp_servers_json`` column, which reaches a pydantic
    model that has never heard of it.
    """
    legacy_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('oauth_connections', 'agent_config_oauth')"
        )
    }
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_configs)")}
    if not legacy_tables and "mcp_servers_json" not in columns:
        return
    found = ", ".join(sorted(legacy_tables | (columns & {"mcp_servers_json"})))
    raise SchemaTooOld(
        f"{path} was written before connections replaced mcp_servers and "
        f"oauth_connections (found: {found}). "
        "Bloom has never been deployed, so there is nothing to preserve: delete "
        f"{path}, {path}-wal and {path}-shm, then start again. The agent_mcp_usage "
        "and agent_runtime_usage logs share that file and will be recreated empty."
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_configs (
    id               TEXT    PRIMARY KEY,          -- uuid4().hex
    slug             TEXT    NOT NULL UNIQUE,      -- how a caller names this agent
    name             TEXT    NOT NULL DEFAULT '',
    system_prompt    TEXT    NOT NULL DEFAULT '',
    model_tier       TEXT    NOT NULL DEFAULT 'balanced',
    max_steps        INTEGER,                      -- NULL = fall back to settings
    max_cost_usd     REAL,                         -- NULL = fall back to settings
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

-- Every credential and every peer this deployment knows about: a global library.
-- Nothing here is owned by an agent, and deleting an agent deletes none of it.
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,                 -- oauth | api_key | mcp
    provider      TEXT,                          -- manifest name; NULL iff kind='mcp'
    name          TEXT NOT NULL,                 -- stable handle. For kind='mcp' this IS
                                                 -- the MCPClient tool namespace.
    label         TEXT NOT NULL DEFAULT '',      -- display only
    config_json   TEXT NOT NULL DEFAULT '{}',    -- NON-SECRET only: url, client_id
    secret        BLOB,                          -- Fernet: access token, pasted key,
                                                 -- or a peer's bearer
    refresh_token BLOB,                          -- oauth only; a separate column so a
                                                 -- partial write cannot half-brick the row
    client_secret BLOB,                          -- Fernet: the provider APP's secret.
                                                 -- Distinct from `secret`, the USER's
                                                 -- grant: they rotate independently, and
                                                 -- reconnecting replaces one not the other.
    expires_at    TEXT,
    scopes_json   TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'pending',
        -- pending | active | expired | needs_reauth | revoked
    encrypted_at  TEXT,                          -- which rotation era the bytes are from
    last_used_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- These constrain writes. `_json_list`'s tolerance below is about reads; a kind
    -- outside the enum would otherwise produce an agent with no tools, no error
    -- anywhere, and nothing to grep for.
    CHECK (kind IN ('oauth', 'api_key', 'mcp')),
    CHECK ((kind = 'mcp' AND provider IS NULL) OR (kind <> 'mcp' AND provider IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_name ON connections (name);
CREATE INDEX IF NOT EXISTS idx_connections_kind ON connections (kind, status);

-- The only edge between an agent and a connection, and it means "attached".
CREATE TABLE IF NOT EXISTS agent_connections (
    agent_config_id TEXT    NOT NULL,
    connection_id   TEXT    NOT NULL,
    -- Broker order is observable: MCPClient keeps the *first* of two colliding
    -- namespaced tool names and warns. Which one wins should be a stored decision
    -- rather than a rowid accident.
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (agent_config_id, connection_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_connections_conn ON agent_connections (connection_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,              -- single use; deleted on exchange
    connection_id TEXT NOT NULL,                 -- the flow belongs to a connection,
                                                 -- not to whichever agent started it
    provider      TEXT NOT NULL,
    code_verifier TEXT NOT NULL DEFAULT '',      -- PKCE; '' when the provider lacks it
    redirect_uri  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT    PRIMARY KEY,           -- minted BEFORE the run starts
    agent_config_id TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL DEFAULT '',
    depth           INTEGER NOT NULL DEFAULT 0,
    origin          TEXT    NOT NULL,              -- mcp | test_run | build
    caller          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL,
        -- running | succeeded | failed | cancelled | abandoned
    prompt          TEXT    NOT NULL,
    result_text     TEXT,
    stopped_by      TEXT,
    total_cost_usd  REAL    NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    heartbeat_at    TEXT    NOT NULL               -- touched per event; reaper input
);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs (agent_config_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status, heartbeat_at);

-- One invocation of the builder: the brief that went in, the agent that came out,
-- and the steps a human still has to take.
--
-- Not folded into `runs`. A run's `result_text` is prose, and what the builder
-- produces has to be addressable by agent, re-renderable weeks later, and tickable
-- one item at a time. `status` also carries a distinction no run status can:
-- `needs_setup` versus `ready` is the difference between "you have work to do" and
-- "it works now", which is the only question anyone asks of a finished build.
CREATE TABLE IF NOT EXISTS builds (
    id              TEXT    PRIMARY KEY,
    run_id          TEXT    NOT NULL,        -- the join to the trace; minted together
    brief           TEXT    NOT NULL,
    agent_config_id TEXT,                    -- NULL until the builder creates one
    agent_slug      TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL,
        -- running | needs_setup | ready | failed
    summary         TEXT    NOT NULL DEFAULT '',
    checklist_json  TEXT    NOT NULL DEFAULT '[]',
    done_json       TEXT    NOT NULL DEFAULT '[]',   -- indexes ticked off
    error           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_builds_run ON builds (run_id);
CREATE INDEX IF NOT EXISTS idx_builds_status ON builds (status, created_at DESC);

CREATE TABLE IF NOT EXISTS run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- the SSE cursor / Last-Event-ID
    run_id       TEXT    NOT NULL,
    seq          INTEGER NOT NULL,                   -- per-run ordinal, for display
    kind         TEXT    NOT NULL,
        -- run_started | text | tool_started | tool_finished | step_finished | run_finished
    step_index   INTEGER,
    tool_name    TEXT,
    tool_call_id TEXT,
    ok           INTEGER,
    latency_ms   INTEGER,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    cost_usd     REAL,
    payload_json TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, id);
"""


def _now() -> str:
    """ISO-8601 UTC to the second, matching every other table in the ecosystem."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id() -> str:
    """A row id. Hex rather than a dashed UUID so it is URL-safe unquoted."""
    return uuid.uuid4().hex


def _json_list(raw: Any) -> list:
    """Decode a JSON TEXT column, tolerating anything that is not a list.

    A hand-edited database should degrade to an empty list rather than take down
    every read of the table it appears in.
    """
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_obj(raw: Any) -> dict:
    """Decode a JSON TEXT column expected to hold an object, tolerating anything else.

    Same posture as :func:`_json_list`: a hand-edited database degrades to an empty
    object rather than taking down every read of the table it appears in.
    """
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class Store:
    """Bloom's tables. Synchronous by design; wrap calls in ``asyncio.to_thread``."""

    def __init__(self, path: str = "data/bloom.db") -> None:
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the connection is reused from asyncio.to_thread
        # worker threads. The lock below, not sqlite's own check, is what makes
        # that safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Three writers share this file (see the module docstring). Without a
            # busy timeout the losers raise instead of waiting.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            # After the script, not before: a brand-new file has no agent_configs
            # table to inspect until the schema has been applied to it.
            _reject_legacy_schema(self._conn, path)
        logger.info("Store ready at %s", path)

    # --- agent configs -------------------------------------------------------

    def create_config(
        self,
        *,
        slug: str,
        name: str = "",
        system_prompt: str = "",
        model_tier: str = "balanced",
        max_steps: int | None = None,
        max_cost_usd: float | None = None,
    ) -> dict:
        """Insert a config and return it. Raises :class:`SlugTaken` on collision.

        Note what is *not* here: nothing about what the agent may reach. A new agent
        is a prompt and a tier, and connections are attached afterwards — asking at
        creation meant asking before the answer was knowable.
        """
        now = _now()
        row_id = new_id()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO agent_configs (id, slug, name, system_prompt, "
                    "model_tier, max_steps, max_cost_usd, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        slug,
                        name,
                        system_prompt,
                        model_tier,
                        max_steps,
                        max_cost_usd,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The only UNIQUE constraint on this table is the slug, so this is
                # unambiguous — but check rather than assume, because a future
                # constraint would otherwise be reported as the wrong error.
                if "slug" in str(exc):
                    raise SlugTaken(slug) from exc
                raise
            self._conn.commit()
        return self.get_config(row_id)  # type: ignore[return-value]

    def get_config(self, config_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_configs WHERE id = ?", (config_id,)
            ).fetchone()
        return _config_row(row) if row else None

    def get_config_by_slug(self, slug: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_configs WHERE slug = ?", (slug,)
            ).fetchone()
        return _config_row(row) if row else None

    def list_configs(self) -> list[dict]:
        """Every config, newest first — the order an agent builder lists them in."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_configs ORDER BY created_at DESC, id"
            ).fetchall()
        return [_config_row(r) for r in rows]

    def update_config(self, config_id: str, **fields: Any) -> dict | None:
        """Patch named columns. Unknown keys are ignored; no field is required.

        Only the columns a caller may edit are settable — ``id`` and ``created_at``
        are not among them, so a PATCH body carrying either changes nothing rather
        than rewriting the row's identity.
        """
        editable = {
            "slug",
            "name",
            "system_prompt",
            "model_tier",
            "max_steps",
            "max_cost_usd",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key in editable and value is not None:
                sets.append(f"{key} = ?")
                values.append(value)
        if not sets:
            return self.get_config(config_id)

        sets.append("updated_at = ?")
        values.extend([_now(), config_id])
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"UPDATE agent_configs SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                    values,
                )
            except sqlite3.IntegrityError as exc:
                if "slug" in str(exc):
                    raise SlugTaken(str(fields.get("slug"))) from exc
                raise
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_config(config_id)

    def delete_config(self, config_id: str) -> bool:
        """Delete a config and its bindings. **Connections survive, always.**

        This is the exact inversion of what this method used to do, and it is the
        point of the library model: a connection is not owned by the agent that
        happened to create it, so deleting that agent must not revoke a credential
        another agent is still using — or one you would only have to enter again.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM agent_configs WHERE id = ?", (config_id,))
            deleted = cur.rowcount > 0
            if deleted:
                self._conn.execute(
                    "DELETE FROM agent_connections WHERE agent_config_id = ?", (config_id,)
                )
            self._conn.commit()
        return deleted

    def connection_ids_for(self, config_id: str) -> list[str]:
        """The connections attached to a config, in broker order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT connection_id FROM agent_connections WHERE agent_config_id = ? "
                "ORDER BY position, created_at, connection_id",
                (config_id,),
            ).fetchall()
        return [r["connection_id"] for r in rows]

    # --- connections -----------------------------------------------------------
    #
    # Every read here goes through _connection_row, which drops all three secret
    # columns. The only way to obtain the ciphertext is :meth:`connection_secrets`,
    # which names what it does. That asymmetry is the point: a projection that
    # returns secrets by default eventually returns them somewhere they are logged.

    def create_connection(
        self,
        *,
        kind: str,
        name: str,
        provider: str | None = None,
        label: str = "",
        config: dict | None = None,
        secret: bytes | None = None,
        refresh_token: bytes | None = None,
        client_secret: bytes | None = None,
        expires_at: str | None = None,
        scopes: list[str] | None = None,
        status: str = "pending",
        attach_to: list[str] | None = None,
    ) -> dict:
        """Insert a connection, optionally attaching it to agents in one transaction.

        ``attach_to`` is a convenience, not ownership: the connection is a library
        entry the moment it exists, and detaching every agent leaves it alone. It
        exists so "add a connection to this agent" cannot half-succeed — a created
        connection whose attach failed is a state the caller would have to detect,
        name and recover from.

        Raises :class:`ConnectionNameTaken` on a duplicate name.
        """
        now = _now()
        row_id = new_id()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO connections (id, kind, provider, name, label, config_json, "
                    "secret, refresh_token, client_secret, expires_at, scopes_json, status, "
                    "encrypted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        kind,
                        provider,
                        name,
                        label,
                        json.dumps(config or {}),
                        secret,
                        refresh_token,
                        client_secret,
                        expires_at,
                        json.dumps(list(scopes or ())),
                        status,
                        now if (secret or client_secret) else None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "name" in str(exc):
                    raise ConnectionNameTaken(name) from exc
                raise
            for agent_config_id in attach_to or ():
                # Appended per agent, not enumerated from zero: two connections
                # created in separate calls would otherwise both sit at position 0
                # and their relative order would fall through to the row id, which
                # is a uuid. Broker order is observable, so it must not be random.
                existing = self._conn.execute(
                    "SELECT COALESCE(MAX(position), -1) AS p FROM agent_connections "
                    "WHERE agent_config_id = ?",
                    (agent_config_id,),
                ).fetchone()
                self._conn.execute(
                    "INSERT OR IGNORE INTO agent_connections "
                    "(agent_config_id, connection_id, position, created_at) VALUES (?,?,?,?)",
                    (agent_config_id, row_id, int(existing["p"]) + 1, now),
                )
            self._conn.commit()
        return self.get_connection(row_id)  # type: ignore[return-value]

    def get_connection(self, connection_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM connections WHERE id = ?", (connection_id,)
            ).fetchone()
        return _connection_row(row) if row else None

    def get_connection_by_name(self, name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM connections WHERE name = ?", (name,)).fetchone()
        return _connection_row(row) if row else None

    def list_connections(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """The library, newest first — what a connection picker paginates through."""
        where, params = [], []
        for column, value in (("kind", kind), ("provider", provider), ("status", status)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM connections {clause} ORDER BY created_at DESC, id",  # noqa: S608
                params,
            ).fetchall()
        return [_connection_row(r) for r in rows]

    def connections_for(self, agent_config_id: str) -> list[dict]:
        """Connections attached to a config, in broker order, secrets excluded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.* FROM connections c "
                "JOIN agent_connections b ON b.connection_id = c.id "
                "WHERE b.agent_config_id = ? ORDER BY b.position, b.created_at, c.id",
                (agent_config_id,),
            ).fetchall()
        return [_connection_row(r) for r in rows]

    def connections_by_agent(self) -> dict[str, list[dict]]:
        """Every attachment in one join, keyed by agent id.

        For the callers that need this for *all* agents at once — `app/mcp.py`
        publishes what each configured agent can reach — where a per-agent read
        would be one query per row of the list it is building.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.agent_config_id AS agent_config_id, c.* FROM connections c "
                "JOIN agent_connections b ON b.connection_id = c.id "
                "ORDER BY b.agent_config_id, b.position, b.created_at, c.id"
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for row in rows:
            projected = _connection_row(row)
            out.setdefault(projected.pop("agent_config_id"), []).append(projected)
        return out

    def agents_for_connection(self, connection_id: str) -> list[dict]:
        """Which agents use this connection — what makes deleting one answerable."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.id, a.slug, a.name FROM agent_configs a "
                "JOIN agent_connections b ON b.agent_config_id = a.id "
                "WHERE b.connection_id = ? ORDER BY a.slug",
                (connection_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def attach_connection(
        self, agent_config_id: str, connection_id: str, *, position: int | None = None
    ) -> bool:
        """Attach a library connection to an agent. False if it already was.

        ``position`` defaults to the end, so attaching never reorders what an agent
        already had — and broker priority stays a decision rather than a surprise.
        """
        with self._lock:
            if position is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(position), -1) AS p FROM agent_connections "
                    "WHERE agent_config_id = ?",
                    (agent_config_id,),
                ).fetchone()
                position = int(row["p"]) + 1
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO agent_connections "
                "(agent_config_id, connection_id, position, created_at) VALUES (?,?,?,?)",
                (agent_config_id, connection_id, position, _now()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def detach_connection(self, agent_config_id: str, connection_id: str) -> bool:
        """Unbind one connection from one agent. The connection itself survives."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM agent_connections WHERE agent_config_id = ? AND connection_id = ?",
                (agent_config_id, connection_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_connection(self, connection_id: str, **fields: Any) -> dict | None:
        """Patch the editable, non-secret columns.

        ``kind`` and ``provider`` are not editable: both would invalidate the stored
        secret, and a connection whose credential no longer matches its provider is
        worse than a second connection. Secrets are not editable here either —
        :meth:`set_connection_secret` is the named path.
        """
        editable = {"name", "label", "status"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if value is None:
                continue
            if key == "config":
                sets.append("config_json = ?")
                values.append(json.dumps(value))
            elif key == "scopes":
                sets.append("scopes_json = ?")
                values.append(json.dumps(list(value)))
            elif key in editable:
                sets.append(f"{key} = ?")
                values.append(value)
        if not sets:
            return self.get_connection(connection_id)

        sets.append("updated_at = ?")
        values.extend([_now(), connection_id])
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"UPDATE connections SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                    values,
                )
            except sqlite3.IntegrityError as exc:
                if "name" in str(exc):
                    raise ConnectionNameTaken(str(fields.get("name"))) from exc
                raise
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_connection(connection_id)

    def set_connection_secret(
        self,
        connection_id: str,
        *,
        secret: bytes | None = None,
        refresh_token: bytes | None = None,
        client_secret: bytes | None = None,
        expires_at: str | None = None,
        scopes: list[str] | None = None,
        status: str | None = None,
    ) -> dict | None:
        """Write ciphertext: the OAuth exchange's path and the pasted-key path.

        Every argument is optional and ``None`` means *leave alone*, never *clear*.
        That rule is why a provider which does not rotate refresh tokens — and so
        omits one from its response — cannot brick a connection on its first
        successful refresh, and it now covers the app's client secret too, which
        rotates on a completely different schedule from the user's grant.
        """
        now = _now()
        sets = ["updated_at = ?"]
        values: list[Any] = [now]
        for column, value in (
            ("secret", secret),
            ("refresh_token", refresh_token),
            ("client_secret", client_secret),
            ("expires_at", expires_at),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        if scopes is not None:
            sets.append("scopes_json = ?")
            values.append(json.dumps(list(scopes)))
        if status is not None:
            sets.append("status = ?")
            values.append(status)
        if secret is not None or client_secret is not None:
            sets.append("encrypted_at = ?")
            values.append(now)
        values.append(connection_id)

        with self._lock:
            cur = self._conn.execute(
                f"UPDATE connections SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                values,
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: str) -> bool:
        """Delete a connection and detach it from every agent.

        The caller decides whether being attached should stop this — the API asks
        for confirmation, because removing a shared connection silently strips
        capability from agents nobody was looking at.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
            deleted = cur.rowcount > 0
            if deleted:
                self._conn.execute(
                    "DELETE FROM agent_connections WHERE connection_id = ?", (connection_id,)
                )
            self._conn.commit()
        return deleted

    def connection_secrets(self, connection_id: str) -> dict | None:
        """The ciphertext columns. The only path to them, and it says so.

        ``kind``, ``provider`` and ``config_json`` ride along because presenting a
        credential needs them — where the key goes is a property of the provider,
        and which app it belongs to is a property of the connection.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, provider, name, config_json, secret, refresh_token, "
                "client_secret, expires_at, status FROM connections WHERE id = ?",
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["config"] = _json_obj(out.pop("config_json", "{}"))
        return out

    def refreshable_connections(self) -> list[dict]:
        """Grants close enough to expiry to be worth refreshing, secrets excluded.

        Restricted to ``kind='oauth'``: an API key has no expiry and no refresh
        endpoint, and a sweep that treated one as refreshable would mark a perfectly
        good key ``needs_reauth`` for failing to do something it cannot do.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM connections WHERE kind = 'oauth' AND status = 'active' "
                "AND refresh_token IS NOT NULL AND refresh_token != '' "
                "AND expires_at IS NOT NULL"
            ).fetchall()
        return [_connection_row(r) for r in rows]

    def update_connection_tokens(
        self,
        connection_id: str,
        *,
        access_token: bytes,
        refresh_token: bytes | None,
        expires_at: str | None,
    ) -> None:
        """Write a refreshed grant back, and mark the connection live again."""
        self.set_connection_secret(
            connection_id,
            secret=access_token,
            # None leaves the stored one alone. A provider that does not rotate
            # simply omits the field, and treating that as a revocation would brick
            # the connection on its first successful refresh.
            refresh_token=refresh_token,
            expires_at=expires_at,
            status="active",
        )

    def set_connection_status(self, connection_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE connections SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), connection_id),
            )
            self._conn.commit()

    def touch_connection(self, connection_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE connections SET last_used_at = ? WHERE id = ?",
                (_now(), connection_id),
            )
            self._conn.commit()

    def revoke_connection(self, connection_id: str) -> bool:
        """Mark revoked and zero the user's credential. False if there was no row.

        By connection id rather than ``(agent, provider)``: a connection is a
        library entry now, and revoking it is an act on the credential itself
        rather than on one agent's view of it.

        The row survives, so the UI can still show "Spotify — disconnected" instead
        of the provider silently vanishing. The *client* secret survives too: the
        app registration is still yours, and clearing it would mean re-entering it
        to reconnect something you only disconnected.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE connections SET status = 'revoked', secret = NULL, "
                "refresh_token = NULL, expires_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), connection_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def count_connections(self) -> int:
        """How many stored secrets exist — startup's input for "is a missing key fatal"."""
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM connections "
                    "WHERE secret IS NOT NULL OR client_secret IS NOT NULL"
                ).fetchone()["n"]
            )

    # --- OAuth handshake state -------------------------------------------------

    def create_oauth_state(
        self,
        *,
        state: str,
        connection_id: str,
        provider: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_states (state, connection_id, provider, "
                "code_verifier, redirect_uri, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (state, connection_id, provider, code_verifier, redirect_uri, _now(), expires_at),
            )
            self._conn.commit()

    def consume_oauth_state(self, state: str) -> dict | None:
        """Read and delete in one transaction. Single use is the security property.

        The callback is the one unauthenticated route in the service, so replaying
        a captured ``state`` must not be able to bind a second connection.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_states WHERE state = ?", (state,)
            ).fetchone()
            if row is not None:
                self._conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            self._conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (_now(),))
            self._conn.commit()
        return dict(row) if row else None

    # --- runs and their event log ---------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        agent_config_id: str,
        prompt: str,
        origin: str,
        conversation_id: str = "",
        depth: int = 0,
        caller: str = "",
    ) -> dict:
        """Open a run in ``running`` state.

        The row is committed *before* the model is called, so a client can attach
        to the trace while the run is still in flight. That ordering is the whole
        reason the id is minted by the caller rather than generated here.
        """
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, agent_config_id, conversation_id, depth, origin, "
                "caller, status, prompt, started_at, heartbeat_at) "
                "VALUES (?,?,?,?,?,?, 'running', ?,?,?)",
                (run_id, agent_config_id, conversation_id, depth, origin, caller, prompt, now, now),
            )
            self._conn.commit()
        return self.get_run(run_id)  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result_text: str | None = None,
        stopped_by: str | None = None,
        total_cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Close a run. Idempotent by intent — the caller's ``finally`` may retry."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, result_text = ?, stopped_by = ?, "
                "total_cost_usd = ?, error = ?, finished_at = ?, heartbeat_at = ? "
                "WHERE id = ?",
                (status, result_text, stopped_by, total_cost_usd, error, now, now, run_id),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, agent_config_id: str, *, limit: int = 50, offset: int = 0) -> list[dict]:
        """A config's runs, newest first — what a status panel paginates through."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE agent_config_id = ? "
                "ORDER BY started_at DESC, id LIMIT ? OFFSET ?",
                (agent_config_id, max(1, min(limit, 200)), max(0, offset)),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        origin: str | None = None,
    ) -> list[dict]:
        """Runs across every agent, newest first, with the agent's slug joined in.

        Not merely a convenience over calling :meth:`list_runs` per agent: N per-agent
        pages cannot be *ordered* against each other without fetching all of them, so
        an activity feed built that way is wrong as soon as there are two agents.

        The slug is joined here rather than looked up client-side because a run whose
        config has since been deleted still belongs in the history — LEFT JOIN leaves
        the slug NULL rather than dropping the row.
        """
        where = []
        params: list[Any] = []
        if status:
            where.append("r.status = ?")
            params.append(status)
        if origin:
            where.append("r.origin = ?")
            params.append(origin)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([max(1, min(limit, 200)), max(0, offset)])

        with self._lock:
            rows = self._conn.execute(
                "SELECT r.*, c.slug AS agent_slug FROM runs r "
                "LEFT JOIN agent_configs c ON c.id = r.agent_config_id "
                f"{clause} ORDER BY r.started_at DESC, r.id LIMIT ? OFFSET ?",  # noqa: S608
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def runs_rollup(self, since: str | None = None) -> dict:
        """What Bloom's own runs cost, which the library usage tables cannot say.

        ``agent_runtime_usage`` knows model spend and ``agent_mcp_usage`` knows tool
        calls, but neither knows what a *run* is — so neither can answer "how many
        tasks were delegated, and how many of them failed".
        """
        clause = " WHERE started_at >= ?" if since else ""
        params: tuple = (since,) if since else ()
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) AS runs, "
                "COALESCE(SUM(total_cost_usd), 0) AS cost_usd, "
                "SUM(status = 'succeeded') AS succeeded, "
                "SUM(status = 'failed') AS failed, "
                "SUM(status = 'cancelled') AS cancelled, "
                "SUM(status = 'running') AS running "
                f"FROM runs{clause}",  # noqa: S608
                params,
            ).fetchone()
            by_agent = self._conn.execute(
                "SELECT COALESCE(c.slug, r.agent_config_id) AS agent, COUNT(*) AS runs, "
                "COALESCE(SUM(r.total_cost_usd), 0) AS cost_usd "
                "FROM runs r LEFT JOIN agent_configs c ON c.id = r.agent_config_id"
                + (" WHERE r.started_at >= ?" if since else "")
                + " GROUP BY agent ORDER BY cost_usd DESC LIMIT 50",
                params,
            ).fetchall()
        out = dict(totals)
        # COUNT returns 0 for an empty table but SUM(bool) returns NULL, and a JSON
        # null where a client expects a number is a rendering bug at the far end.
        for key in ("succeeded", "failed", "cancelled", "running"):
            out[key] = int(out.get(key) or 0)
        out["by_agent"] = [dict(r) for r in by_agent]
        return out

    def append_event(
        self,
        run_id: str,
        *,
        seq: int,
        kind: str,
        step_index: int | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        ok: bool | None = None,
        latency_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        payload: dict | None = None,
    ) -> int:
        """Append one trace event and return its autoincrement id.

        That id is the SSE cursor a client resumes from, so it must be monotonic
        across the whole table and never reused — which is exactly what
        ``INTEGER PRIMARY KEY AUTOINCREMENT`` guarantees and what a per-run
        ordinal would not.

        The run's heartbeat is touched in the same transaction. A run that is
        emitting events is alive by definition, so the reaper needs no separate
        signal.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO run_events (run_id, seq, kind, step_index, tool_name, "
                "tool_call_id, ok, latency_ms, tokens_in, tokens_out, cost_usd, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    kind,
                    step_index,
                    tool_name,
                    tool_call_id,
                    None if ok is None else int(ok),
                    latency_ms,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    json.dumps(payload or {}),
                    now,
                ),
            )
            self._conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (now, run_id))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def events_after(self, run_id: str, after_id: int = 0, *, limit: int = 500) -> list[dict]:
        """Trace events with an id greater than ``after_id``, oldest first.

        This is both the live tail and the completed-trace read: the same rows
        through two transports, so a client writes one renderer.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                (run_id, after_id, max(1, min(limit, 2000))),
            ).fetchall()
        return [_event_row(r) for r in rows]

    def sweep_abandoned_runs(self) -> list[str]:
        """Close runs left ``running`` by a dead process. Returns their ids.

        Called at startup. Both halves matter: the status must change so the row
        stops claiming to be in flight, **and** a terminal event must be appended,
        because that event is the only thing that ends an SSE stream. Without the
        second half a client tailing across a restart hangs forever.
        """
        with self._lock:
            rows = self._conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
            ids = [r["id"] for r in rows]
        for run_id in ids:
            self.finish_run(
                run_id,
                status="abandoned",
                error="abandoned: the process running this task restarted",
            )
            self.append_event(
                run_id,
                seq=_next_seq(self, run_id),
                kind="run_finished",
                ok=False,
                payload={"status": "abandoned"},
            )
        if ids:
            logger.warning("Swept %d run(s) abandoned by a previous process", len(ids))
        return ids

    def max_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS s FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["s"])

    # --- builds ----------------------------------------------------------------

    def create_build(self, *, build_id: str, run_id: str, brief: str) -> dict:
        """Open a build in ``running`` state.

        Committed *before* the run starts, the same ordering and for the same reason
        as :meth:`create_run`: a client that posted a brief needs something to attach
        to while the work is still in flight.
        """
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO builds (id, run_id, brief, status, created_at, updated_at) "
                "VALUES (?,?,?, 'running', ?,?)",
                (build_id, run_id, brief, now, now),
            )
            self._conn.commit()
        return self.get_build(build_id)  # type: ignore[return-value]

    def get_build(self, build_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        return _build_row(row) if row else None

    def get_build_by_run(self, run_id: str) -> dict | None:
        """The build a run belongs to. How a tool holding only a run id finds its row."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM builds WHERE run_id = ?", (run_id,)).fetchone()
        return _build_row(row) if row else None

    def list_builds(
        self, *, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict]:
        """Builds, newest first. ``status`` narrows to e.g. everything still unfinished."""
        clause = "WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        params.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM builds {clause} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",  # noqa: S608
                params,
            ).fetchall()
        return [_build_row(r) for r in rows]

    def update_build(self, build_id: str, **fields: Any) -> dict | None:
        """Patch a build. ``None`` means leave alone, never clear.

        ``checklist`` and ``done`` are taken as Python lists and stored as JSON; the
        rest are plain columns.
        """
        editable = {"agent_config_id", "agent_slug", "status", "summary", "error"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if value is None:
                continue
            if key == "checklist":
                sets.append("checklist_json = ?")
                values.append(json.dumps(list(value)))
            elif key == "done":
                sets.append("done_json = ?")
                values.append(json.dumps(sorted({int(i) for i in value})))
            elif key in editable:
                sets.append(f"{key} = ?")
                values.append(value)
        if not sets:
            return self.get_build(build_id)

        sets.append("updated_at = ?")
        values.extend([_now(), build_id])
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE builds SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                values,
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_build(build_id)

    def delete_build(self, build_id: str) -> bool:
        """Forget a build. The agent it created, and its run history, survive."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def sweep_abandoned_builds(self) -> list[str]:
        """Fail builds left ``running`` by a dead process, mirroring the run sweep.

        Without this a build whose process restarted claims to be in flight forever,
        and Aperture shows a spinner with nothing behind it. The run sweep already
        closed the trace; this closes the thing the user was actually watching.
        """
        with self._lock:
            rows = self._conn.execute("SELECT id FROM builds WHERE status = 'running'").fetchall()
            ids = [r["id"] for r in rows]
        for build_id in ids:
            self.update_build(
                build_id,
                status="failed",
                error="abandoned: the process running this build restarted",
            )
        if ids:
            logger.warning("Swept %d build(s) abandoned by a previous process", len(ids))
        return ids

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _next_seq(store: Store, run_id: str) -> int:
    return store.max_seq(run_id) + 1


def _connection_row(row: sqlite3.Row) -> dict:
    """Project a connection row **without any of its secrets**.

    Aperture shows kind, provider, status, scopes and last-used; it never needs a
    secret value, and a projection that returned one by default would eventually
    put it in a log or a JSON response. `Store.connection_secrets` is the
    deliberate, named exception.

    ``has_secret`` / ``has_client_secret`` are what a UI actually wanted from a
    secret column — whether one is set — so nothing is tempted to ask for the real
    thing to find out.
    """
    out = dict(row)
    out["has_secret"] = out.pop("secret", None) is not None
    out["has_client_secret"] = out.pop("client_secret", None) is not None
    out.pop("refresh_token", None)
    out["scopes"] = _json_list(out.pop("scopes_json", "[]"))
    out["config"] = _json_obj(out.pop("config_json", "{}"))
    return out


def _build_row(row: sqlite3.Row) -> dict:
    """Project a build row, decoding both JSON columns.

    ``checklist`` degrades to an empty list rather than taking down the read, the
    same tolerance `_json_list` applies everywhere else — and it matters more here,
    because this column holds model output.
    """
    out = dict(row)
    out["checklist"] = _json_list(out.pop("checklist_json", "[]"))
    out["done"] = [int(i) for i in _json_list(out.pop("done_json", "[]")) if isinstance(i, int)]
    return out


def _event_row(row: sqlite3.Row) -> dict:
    """Project an event row, decoding its payload and normalising ``ok`` to bool."""
    out = dict(row)
    try:
        out["payload"] = json.loads(out.pop("payload_json", "{}") or "{}")
    except ValueError:
        out["payload"] = {}
    if out.get("ok") is not None:
        out["ok"] = bool(out["ok"])
    return out


def _config_row(row: sqlite3.Row) -> dict:
    """Project a config row.

    Nothing to decode any more: what an agent may reach lives in
    ``agent_connections``, not in a JSON column on the agent itself.
    """
    return dict(row)


@lru_cache
def get_store() -> Store:
    """Process-wide store singleton.

    Cached because the connection and its lock must be shared — two Store objects
    would mean two connections racing on the same file with no lock between them.
    Tests clear it with ``get_store.cache_clear()``, or construct ``Store(path)``
    directly and inject it, which touches no global state.
    """
    from app.config import get_settings

    return Store(get_settings().db_path)
