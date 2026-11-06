"""Event reducers: how each event type changes the L1 projection.

Every event type ever written must keep a reducer here (docs/development/compatibility.md).
A ledger containing an unknown type stops the projection instead of being silently skipped.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

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


def _task_imported(conn: sqlite3.Connection, event: Event) -> None:
    """A task brought over from another system (DN005), with its original business times."""
    data = event.data
    task_id = event.task_id
    if data.get("update"):  # a later snapshot of a task nobody has touched here since
        for table in ("tasks", "dependencies", "outputs", "imported_tasks"):
            conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM resource_claims WHERE owner_task = ?", (task_id,))
        conn.execute(
            "DELETE FROM input_requests WHERE task_id = ? AND kind = 'imported'", (task_id,)
        )
    elif conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
        raise rejected("账本中同一任务被登记了两次", reason="ledger_inconsistent", task_id=task_id)
    when = data.get("last_activity") or event.time
    conn.execute(
        "INSERT INTO tasks(task_id, title, repo, kind, status, revision, version, spec_path, "
        "spec_sha256, registered_seq, last_activity, source_seq, goal, acceptance, pause, "
        "attempt_count, previous_status, done_at, accepted_at, business_changed_at, "
        "resources, stale) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            data["title"],
            data["repo"],
            data["kind"],
            data["new_status"],
            event.revision or 1,
            data["spec_path"],
            data.get("spec_sha256") or "",
            event.sequence,
            when,
            event.sequence,
            data.get("goal", ""),
            json.dumps(data.get("acceptance", []), ensure_ascii=False, sort_keys=True),
            _json_or_none(data.get("pause")),
            int(data.get("attempt_count") or 0),
            data.get("previous_status"),
            data.get("done_at"),
            data.get("accepted_at"),
            when,
            json.dumps(data.get("resources") or [], ensure_ascii=False),
            1 if data.get("stale") else 0,
        ),
    )
    for resource in data.get("held_resources") or []:
        conn.execute(
            "INSERT INTO resource_claims(resource, owner_task, status) VALUES(?, ?, 'held') "
            "ON CONFLICT(resource, owner_task) DO UPDATE SET status = 'held'",
            (str(resource), task_id),
        )
    request: dict[str, Any] | None = data.get("input_request")
    if request:
        conn.execute(
            "INSERT OR REPLACE INTO input_requests(request_id, task_id, kind, status, question, "
            "binding) VALUES(?, ?, 'imported', 'pending', ?, ?)",
            (
                request["request_id"],
                task_id,
                request["question"],
                json.dumps(
                    {"revision": event.revision or 1, "source": request.get("source")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
    deps: list[dict[str, Any]] = data.get("dependencies") or []
    for dep in deps:
        conn.execute(
            "INSERT OR IGNORE INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
            "VALUES(?, ?, ?, ?, 'imported')",
            (
                task_id,
                dep["dep_id"],
                int(dep.get("bound_revision") or 0),
                dep.get("kind") or "explicit",
            ),
        )
    outputs: list[dict[str, Any]] = data.get("outputs") or []
    for out in outputs:
        conn.execute(
            "INSERT OR REPLACE INTO outputs(task_id, output_id, type, ref, role, delivery, "
            "verified_at, evidence) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                out["output_id"],
                out["type"],
                out["ref"],
                out.get("role"),
                out.get("delivery"),
                out.get("verified_at"),
                out.get("evidence") or "imported",
            ),
        )
    conn.execute(
        "INSERT INTO imported_tasks(task_id, source, event_seq, source_sequence, old_status, "
        "resumable, runs, legacy) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            data["source"],
            event.sequence,
            int(data["source_sequence"]),
            data.get("old_status"),
            1 if data.get("resumable") else 0,
            data.get("runs"),
            json.dumps(data.get("legacy") or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _json_or_none(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else None


def _import_marker(conn: sqlite3.Connection, event: Event) -> None:
    """import.started / import.completed: the facts live in the event; nothing to project."""


def _task_note(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO task_notes(task_id, sequence, text) VALUES(?, ?, ?)",
        (event.task_id, event.sequence, event.data["text"]),
    )
    conn.execute("UPDATE tasks SET version = version + 1 WHERE task_id = ?", (event.task_id,))


REDUCERS: dict[str, Reducer] = {
    "task.registered": _task_registered,
    "task.note": _task_note,
    "import.started": _import_marker,
    "import.task_imported": _task_imported,
    "import.completed": _import_marker,
    "controller.lease_acquired": lease.reduce_acquired,
    "controller.lease_released": lease.reduce_released,
    "controller.lease_taken_over": lease.reduce_taken_over,
    **MACHINE_REDUCERS,
    "orchestrator.inbox_acked": reduce_acked,
}
