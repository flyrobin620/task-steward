"""Event reducers: how each event type changes the L1 projection.

Every event type ever written must keep a reducer here (docs/development/compatibility.md).
A ledger containing an unknown type stops the projection instead of being silently skipped.
"""

from __future__ import annotations

import json
import sqlite3

from tsw import lease
from tsw.errors import rejected
from tsw.events import Event
from tsw.host.inbox import reduce_acked
from tsw.machine.reducers import REDUCERS as MACHINE_REDUCERS
from tsw.projection import Reducer


def _task_registered(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    exists = conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (event.task_id,)).fetchone()
    if exists is not None:
        raise rejected(
            "账本中同一任务被登记了两次", reason="ledger_inconsistent", task_id=event.task_id
        )
    conn.execute(
        "INSERT INTO tasks(task_id, title, repo, kind, status, revision, version, spec_path, "
        "spec_sha256, registered_seq, last_activity, source_seq, goal, acceptance, "
        "business_changed_at) VALUES(?, ?, ?, ?, 'intake', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.task_id,
            data["title"],
            data["repo"],
            data["kind"],
            event.revision or 1,
            data["spec_path"],
            data["spec_sha256"],
            event.sequence,
            event.time,
            event.sequence,
            data.get("goal", ""),
            json.dumps(data.get("acceptance", []), ensure_ascii=False, sort_keys=True),
            event.time,
        ),
    )


def _task_note(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO task_notes(task_id, sequence, text) VALUES(?, ?, ?)",
        (event.task_id, event.sequence, event.data["text"]),
    )
    conn.execute("UPDATE tasks SET version = version + 1 WHERE task_id = ?", (event.task_id,))


REDUCERS: dict[str, Reducer] = {
    "task.registered": _task_registered,
    "task.note": _task_note,
    "controller.lease_acquired": lease.reduce_acquired,
    "controller.lease_released": lease.reduce_released,
    "controller.lease_taken_over": lease.reduce_taken_over,
    **MACHINE_REDUCERS,
    "orchestrator.inbox_acked": reduce_acked,
}
