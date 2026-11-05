"""The orchestrator's inbox of reportable events and the read-only ``attention`` self-check."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import Event, EventDraft
from tsw.machine.guards import invalidated
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


def ack(sequences: Sequence[Any], *, actor: Mapping[str, Any], config: EffectiveConfig) -> Command:
    if not sequences or not all(isinstance(s, int) and not isinstance(s, bool) for s in sequences):
        raise rejected("sequences 应为非空整数数组", reason="invalid_input", field="sequences")

    def build(ctx: CommitContext) -> list[EventDraft]:
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
        "pending_input": [
            {
                "request_id": r["request_id"],
                "task_id": r["task_id"],
                "kind": r["kind"],
                "question": r["question"],
            }
            for r in conn.execute("SELECT * FROM input_requests WHERE status = 'pending'")
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
