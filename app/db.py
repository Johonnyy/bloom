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

* **``mcp_servers_json`` and ``scopes_json`` are TEXT, not child tables.** Bloom
  never queries inside them; it stores what was configured and projects on read.
* **``agent_config_oauth`` is the only binding between a config and a credential.**
  ``oauth_connections.agent_config_id`` looks like it says the same thing and does
  not: it records *ownership*, so deleting a config deletes the connections it
  created while leaving shared ones alone. The spec described both a
  ``list[UUID]`` on the config and a foreign key on the connection; those are a
  many-to-many and a one-to-many, and this is the reading that keeps both useful.
* **``run_events`` is append-only with an autoincrement id.** That id is the SSE
  cursor a client resumes from, so it must be monotonic across the whole table and
  never reused. A crashed process therefore still leaves a readable partial trace.
* **Token columns are excluded from every read projection.** `_connection_row`
  never returns ``access_token``/``refresh_token``; the one caller that needs the
  bytes asks for them explicitly. Note that sync-store's equivalent *does* return
  its ``token`` — that is a peer credential an admin sets, not a user's OAuth
  grant, and the shape should not be copied here.
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_configs (
    id               TEXT    PRIMARY KEY,          -- uuid4().hex
    slug             TEXT    NOT NULL UNIQUE,      -- how a caller names this agent
    name             TEXT    NOT NULL DEFAULT '',
    system_prompt    TEXT    NOT NULL DEFAULT '',
    model_tier       TEXT    NOT NULL DEFAULT 'balanced',
    mcp_servers_json TEXT    NOT NULL DEFAULT '[]',
    max_steps        INTEGER,                      -- NULL = fall back to settings
    max_cost_usd     REAL,                         -- NULL = fall back to settings
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_connections (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    agent_config_id TEXT,                          -- owner; NULL = shared/reusable
    access_token    BLOB,                          -- Fernet ciphertext, never plaintext
    refresh_token   BLOB,                          -- separate column: a partial write
                                                   -- must not half-brick the row
    expires_at      TEXT,
    scopes_json     TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
        -- pending | active | expired | needs_reauth | revoked
    encrypted_at    TEXT,                          -- which rotation era the bytes are from
    last_used_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conn_provider ON oauth_connections (provider, status);

CREATE TABLE IF NOT EXISTS agent_config_oauth (
    agent_config_id     TEXT NOT NULL,
    oauth_connection_id TEXT NOT NULL,
    PRIMARY KEY (agent_config_id, oauth_connection_id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state           TEXT PRIMARY KEY,              -- single use; deleted on exchange
    agent_config_id TEXT NOT NULL,
    provider        TEXT NOT NULL,
    code_verifier   TEXT NOT NULL DEFAULT '',      -- PKCE; '' when the provider lacks it
    redirect_uri    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT    PRIMARY KEY,           -- minted BEFORE the run starts
    agent_config_id TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL DEFAULT '',
    depth           INTEGER NOT NULL DEFAULT 0,
    origin          TEXT    NOT NULL,              -- mcp | test_run
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
        logger.info("Store ready at %s", path)

    # --- agent configs -------------------------------------------------------

    def create_config(
        self,
        *,
        slug: str,
        name: str = "",
        system_prompt: str = "",
        model_tier: str = "balanced",
        mcp_servers: list[str] | None = None,
        max_steps: int | None = None,
        max_cost_usd: float | None = None,
    ) -> dict:
        """Insert a config and return it. Raises :class:`SlugTaken` on collision."""
        now = _now()
        row_id = new_id()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO agent_configs (id, slug, name, system_prompt, "
                    "model_tier, mcp_servers_json, max_steps, max_cost_usd, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        slug,
                        name,
                        system_prompt,
                        model_tier,
                        json.dumps(list(mcp_servers or ())),
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
            if key == "mcp_servers" and value is not None:
                sets.append("mcp_servers_json = ?")
                values.append(json.dumps(list(value)))
            elif key in editable and value is not None:
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
        """Delete a config, its bindings, and the connections it owns.

        Shared connections (``agent_config_id IS NULL``) survive — that is the
        whole difference between the ownership column and the binding table.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM agent_configs WHERE id = ?", (config_id,))
            deleted = cur.rowcount > 0
            if deleted:
                self._conn.execute(
                    "DELETE FROM agent_config_oauth WHERE agent_config_id = ?", (config_id,)
                )
                self._conn.execute(
                    "DELETE FROM oauth_connections WHERE agent_config_id = ?", (config_id,)
                )
            self._conn.commit()
        return deleted

    def connection_ids_for(self, config_id: str) -> list[str]:
        """The OAuth connections bound to a config, in binding order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT oauth_connection_id FROM agent_config_oauth "
                "WHERE agent_config_id = ? ORDER BY oauth_connection_id",
                (config_id,),
            ).fetchall()
        return [r["oauth_connection_id"] for r in rows]

    # --- OAuth connections -----------------------------------------------------
    #
    # Every read here goes through _connection_row, which drops the two token
    # columns. The only way to obtain the ciphertext is :meth:`connection_secrets`,
    # which names what it does. That asymmetry is the point: a projection that
    # returns tokens by default eventually returns them somewhere they are logged.

    def upsert_connection(
        self,
        *,
        provider: str,
        agent_config_id: str | None,
        access_token: bytes,
        refresh_token: bytes | None,
        expires_at: str | None,
        scopes: list[str],
        status: str = "active",
        connection_id: str | None = None,
    ) -> dict:
        """Create or replace the connection for (provider, owning config).

        Reconnecting a provider must *replace* the grant rather than accumulate a
        second one: two live connections to one provider on one config collide on
        tool name, and the composite broker would silently keep whichever came
        first. The uniqueness is enforced here rather than by a constraint because
        ``agent_config_id`` is nullable and SQLite treats NULLs as distinct in a
        UNIQUE index — the constraint would not fire for shared connections.
        """
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM oauth_connections WHERE provider = ? AND agent_config_id IS ?",
                (provider, agent_config_id),
            ).fetchone()
            row_id = connection_id or (existing["id"] if existing else new_id())
            if existing:
                self._conn.execute(
                    "UPDATE oauth_connections SET access_token = ?, refresh_token = ?, "
                    "expires_at = ?, scopes_json = ?, status = ?, encrypted_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        access_token,
                        refresh_token,
                        expires_at,
                        json.dumps(scopes),
                        status,
                        now,
                        now,
                        row_id,
                    ),
                )
            else:
                self._conn.execute(
                    "INSERT INTO oauth_connections (id, provider, agent_config_id, "
                    "access_token, refresh_token, expires_at, scopes_json, status, "
                    "encrypted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        provider,
                        agent_config_id,
                        access_token,
                        refresh_token,
                        expires_at,
                        json.dumps(scopes),
                        status,
                        now,
                        now,
                        now,
                    ),
                )
            if agent_config_id:
                self._conn.execute(
                    "INSERT OR IGNORE INTO agent_config_oauth "
                    "(agent_config_id, oauth_connection_id) VALUES (?,?)",
                    (agent_config_id, row_id),
                )
            self._conn.commit()
        return self.get_connection(row_id)  # type: ignore[return-value]

    def get_connection(self, connection_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_connections WHERE id = ?", (connection_id,)
            ).fetchone()
        return _connection_row(row) if row else None

    def connections_for(self, agent_config_id: str) -> list[dict]:
        """Connections bound to a config, tokens excluded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.* FROM oauth_connections c "
                "JOIN agent_config_oauth b ON b.oauth_connection_id = c.id "
                "WHERE b.agent_config_id = ? ORDER BY c.provider",
                (agent_config_id,),
            ).fetchall()
        return [_connection_row(r) for r in rows]

    def connection_secrets(self, connection_id: str) -> dict | None:
        """The ciphertext columns. The only path to them, and it says so."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, provider, access_token, refresh_token, expires_at, status "
                "FROM oauth_connections WHERE id = ?",
                (connection_id,),
            ).fetchone()
        return dict(row) if row else None

    def refreshable_connections(self) -> list[dict]:
        """Active connections that carry a refresh token, tokens excluded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM oauth_connections WHERE status = 'active' "
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
        """Write a refreshed grant back.

        ``refresh_token=None`` leaves the stored one alone rather than clearing it:
        providers that do not rotate refresh tokens simply omit one from the
        response, and treating that omission as a revocation would brick the
        connection on its first successful refresh.
        """
        now = _now()
        with self._lock:
            if refresh_token is None:
                self._conn.execute(
                    "UPDATE oauth_connections SET access_token = ?, expires_at = ?, "
                    "status = 'active', encrypted_at = ?, updated_at = ? WHERE id = ?",
                    (access_token, expires_at, now, now, connection_id),
                )
            else:
                self._conn.execute(
                    "UPDATE oauth_connections SET access_token = ?, refresh_token = ?, "
                    "expires_at = ?, status = 'active', encrypted_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (access_token, refresh_token, expires_at, now, now, connection_id),
                )
            self._conn.commit()

    def set_connection_status(self, connection_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oauth_connections SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), connection_id),
            )
            self._conn.commit()

    def touch_connection(self, connection_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oauth_connections SET last_used_at = ? WHERE id = ?",
                (_now(), connection_id),
            )
            self._conn.commit()

    def revoke_connection(self, agent_config_id: str, provider: str) -> bool:
        """Mark revoked and zero the tokens. Returns False if there was nothing.

        The row survives so the UI can still show "Spotify — disconnected" rather
        than the provider silently vanishing from the agent's page.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT c.id FROM oauth_connections c "
                "JOIN agent_config_oauth b ON b.oauth_connection_id = c.id "
                "WHERE b.agent_config_id = ? AND c.provider = ?",
                (agent_config_id, provider),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE oauth_connections SET status = 'revoked', access_token = NULL, "
                "refresh_token = NULL, expires_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            self._conn.commit()
        return True

    def count_connections(self) -> int:
        """Used at startup to decide whether a missing key is fatal."""
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM oauth_connections WHERE access_token IS NOT NULL"
                ).fetchone()["n"]
            )

    # --- OAuth handshake state -------------------------------------------------

    def create_oauth_state(
        self,
        *,
        state: str,
        agent_config_id: str,
        provider: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_states (state, agent_config_id, provider, "
                "code_verifier, redirect_uri, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (state, agent_config_id, provider, code_verifier, redirect_uri, _now(), expires_at),
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _next_seq(store: Store, run_id: str) -> int:
    return store.max_seq(run_id) + 1


def _connection_row(row: sqlite3.Row) -> dict:
    """Project a connection row **without its tokens**.

    Aperture shows provider, status, scopes and last-used; it never needs a token
    value, and a projection that returned one by default would eventually put it
    in a log or a JSON response. `Store.connection_secrets` is the deliberate,
    named exception.
    """
    out = dict(row)
    out.pop("access_token", None)
    out.pop("refresh_token", None)
    out["scopes"] = _json_list(out.pop("scopes_json", "[]"))
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
    """Project a config row, decoding its JSON column."""
    out = dict(row)
    out["mcp_servers"] = _json_list(out.pop("mcp_servers_json", "[]"))
    return out


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
