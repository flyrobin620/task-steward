"""The orchestrator's inbox of reportable events and the read-only ``attention`` self-check."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import Event, EventDraft, utc_now
from tsw.lease import require_lease
from tsw.machine.ask_user import handoff_due
from tsw.machine.guards import dependency_problems, invalidated, topological
from tsw.machine.model import OCCUPYING, loads
from tsw.storage.base import StorageBackend


def reportable(config: EffectiveConfig) -> set[str]:
    return set(config.get("scheduling.wake.reportable_events") or [])


def acked(conn: sqlite3.Connection) -> set[int]:
    return {int(r["sequence"]) for r in conn.execute("SELECT sequence FROM inbox_acks")}


def inbox(
    conn: sqlite3.Connection, storage: StorageBackend, config: EffectiveConfig, limit: int = 50
) -> list[dict[str, Any]]:
    """Reportable events not yet acknowledged, oldest first."""
    kinds = reportable(config)
    done = acked(conn)
    out: list[dict[str, Any]] = []
    for event in storage.read(since=0):
        if event.type in kinds and event.sequence not in done:
            out.append(_summary(event))
            if len(out) >= limit:
                break
    return out


def _summary(event: Event) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "type": event.type,
        "task_id": event.task_id,
        "time": event.time,
        "data": dict(event.data),
    }


def ack(
    sequences: Sequence[Any],
    *,
    actor: Mapping[str, Any],
    config: EffectiveConfig,
    session_id: str | None,
    machine: str,
) -> Command:
    """Acknowledge reportable events; like every orchestrator write it needs the lease (G-LEASE)."""
    if not sequences or not all(isinstance(s, int) and not isinstance(s, bool) for s in sequences):
        raise rejected("sequences 应为非空整数数组", reason="invalid_input", field="sequences")

    def build(ctx: CommitContext) -> list[EventDraft]:
        if not session_id:
            raise rejected("主会话工具需要会话 ID", reason="lease_missing")
        require_lease(ctx.conn, session_id, machine, ctx.now)
        kinds = reportable(config)
        wanted = set(sequences)
        found = {
            e.sequence: e for e in ctx.storage.read(since=min(wanted) - 1) if e.sequence in wanted
        }
        missing = sorted(s for s in wanted if s not in found or found[s].type not in kinds)
        if missing:
            raise rejected("只能确认可回传的事件", reason="not_reportable", sequences=missing)
        fresh = sorted(wanted - acked(ctx.conn))
        if not fresh:
            return []
        return [EventDraft(type="orchestrator.inbox_acked", data={"sequences": fresh})]

    return Command(
        actor=actor, input={"op": "inbox_ack", "sequences": sorted(sequences)}, build=build
    )


def reduce_acked(conn: sqlite3.Connection, event: Event) -> None:
    for sequence in event.data["sequences"]:
        conn.execute("INSERT OR IGNORE INTO inbox_acks(sequence) VALUES(?)", (int(sequence),))


def attention(
    conn: sqlite3.Connection, storage: StorageBackend, config: EffectiveConfig
) -> dict[str, list[dict[str, Any]]]:
    """What needs the orchestrator now. Read-only: it never changes state or times."""
    items: dict[str, list[dict[str, Any]]] = {
        "judgments": [
            {"item_id": r["item_id"], "task_id": r["task_id"], "trigger": r["trigger"]}
            for r in conn.execute("SELECT * FROM judgments WHERE status = 'open' ORDER BY item_id")
        ],
        # A question still waiting in a worker's session is the worker's and the user's: the
        # orchestrator sees that it is open, not what it asks (DN006 §8.2).
        "pending_input": [
            {
                "request_id": r["request_id"],
                "task_id": r["task_id"],
                "kind": r["kind"],
                **(
                    {"channel": "in-session", "handoff_after": r["handoff_after"]}
                    if r["kind"] == "in_session" and r["handed_off_at"] is None
                    else {"question": r["question"]}
                ),
            }
            for r in conn.execute(
                "SELECT r.*, s.handoff_after, s.handed_off_at FROM input_requests r "
                "LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
                "WHERE r.status = 'pending'"
            )
        ],
        "handoff_due": [
            {"request_id": r["request_id"], "task_id": r["task_id"]}
            for r in handoff_due(conn, utc_now())
        ],
        "reconcile": [
            {"task_id": r["task_id"], "attempt": r["attempt"], "session_name": r["session_name"]}
            for r in conn.execute("SELECT * FROM attempts WHERE phase = 'starting'")
        ],
        "needs_stop": [],
        "stale": [
            {"task_id": r["task_id"]}
            for r in conn.execute("SELECT task_id FROM tasks WHERE stale = 1")
        ],
        "to_start": [],
        "needs_prestart": needs_prestart(conn, config),
        "verifying": [
            {"task_id": r["task_id"]}
            for r in conn.execute("SELECT task_id FROM tasks WHERE status = 'verifying'")
        ],
        "paused": [
            {"task_id": r["task_id"], "pause": loads(r["pause"], {})}
            for r in conn.execute("SELECT task_id, pause FROM tasks WHERE status = 'paused'")
        ],
        "inbox": [{"count": len(inbox(conn, storage, config, limit=1000))}],
    }
    invalid = invalidated(conn)
    for row in conn.execute("SELECT task_id, status FROM tasks"):
        if row["status"] in OCCUPYING and row["task_id"] in invalid:
            items["needs_stop"].append({"task_id": row["task_id"]})
    for row in conn.execute("SELECT * FROM tasks WHERE status = 'ready'"):
        current = conn.execute(
            "SELECT phase FROM attempts WHERE task_id = ? AND attempt = ?",
            (row["task_id"], row["current_attempt"]),
        ).fetchone()
        if current is None or current["phase"] in ("planned", "replaced", "closed"):
            items["to_start"].append({"task_id": row["task_id"]})
    return items


# Attempt phases in which a task has a session or is getting one: a prestart is refused then.
_HAS_SESSION = ("starting", "standby", "live", "stopped")
# Dependency problems no upstream progress clears: a session prestarted for them would only wait.
_DEAD_DEPENDENCY = ("missing", "cancelled")


def needs_prestart(conn: sqlite3.Connection, config: EffectiveConfig) -> list[dict[str, Any]]:
    """Tasks a user turn should prestart a session for (DN002 §5.6).

    A task qualifies when it is ``ready`` or waiting for a dependency and has no session yet.
    Ready tasks come first, then the waiting ones in dependency order (upstream first); the list
    is no longer than the standby places still free under ``scheduling.prestart.max_standby``.
    """
    if not config.get("scheduling.prestart.enabled"):
        return []
    standby = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE phase = 'standby' "
        "OR (phase = 'starting' AND purpose = 'prestart')"
    ).fetchone()["n"]
    free = int(config.get("scheduling.prestart.max_standby")) - int(standby)
    if free <= 0:
        return []
    invalid = invalidated(conn)
    ready: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for row in topological(conn):
        if row["status"] not in ("ready", "waiting_dependency"):
            continue
        current = conn.execute(
            "SELECT phase FROM attempts WHERE task_id = ? AND attempt = ?",
            (row["task_id"], row["current_attempt"]),
        ).fetchone()
        if current is not None and current["phase"] in _HAS_SESSION:
            continue
        if row["status"] == "ready":
            ready.append({"task_id": row["task_id"], "status": "ready"})
            continue
        problems = dependency_problems(conn, str(row["task_id"]), invalid)
        if any(p["reason"] in _DEAD_DEPENDENCY for p in problems):
            continue
        waiting.append(
            {
                "task_id": row["task_id"],
                "status": "waiting_dependency",
                "waiting_on": sorted(str(p["dep_id"]) for p in problems),
            }
        )
    return [*ready, *waiting][:free]
