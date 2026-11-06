"""States, the transition table and projection helpers of the task state machine (DN002).

Every state-changing event carries the transition it takes (``data.transition``). The reducer
checks the task's current state against this table before moving it, so the ledger can never
describe a transition the design does not allow.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from tsw.errors import rejected

STATES = (
    "intake",
    "waiting_dependency",
    "ready",
    "dispatching",
    "in_progress",
    "waiting_user",
    "paused",
    "verifying",
    "done",
    "accepted",
    "cancelled",
    "archived",
)
KINDS_ALLOWED = ("research", "design", "code")
OCCUPYING = frozenset({"dispatching", "in_progress", "waiting_user", "verifying"})
LIVE_PHASES = frozenset({"standby", "starting", "live", "stopped"})
SUBSTATES = ("executing", "reviewing", "triaging", "revising")
PAUSE_CATEGORIES = (
    "external",
    "user_park",
    "failure",
    "workdir_conflict",
    "integration_blocked",
    "import_handover",
)
CANCELLABLE = frozenset(
    {
        "intake",
        "waiting_dependency",
        "ready",
        "dispatching",
        "in_progress",
        "waiting_user",
        "paused",
        "verifying",
        "done",
    }
)


@dataclass(frozen=True)
class Transition:
    id: str
    sources: frozenset[str]
    target: str


def _t(tid: str, sources: str, target: str) -> Transition:
    return Transition(tid, frozenset(sources.split()), target)


TRANSITIONS: dict[str, Transition] = {
    t.id: t
    for t in (
        _t("T02", "intake", "intake"),
        _t("T03", "intake", "waiting_dependency"),
        _t("T04", "intake", "ready"),
        _t("T05", "waiting_dependency", "ready"),
        _t("T06", "ready", "waiting_dependency"),
        _t("T07", "ready", "dispatching"),
        _t("T08", "dispatching", "in_progress"),
        _t("T09", "dispatching", "ready"),
        _t("T10", "dispatching", "paused"),
        _t("T11", "in_progress", "waiting_user"),
        _t("T12", "waiting_user", "in_progress"),
        _t("T13", "in_progress", "verifying"),
        _t("T14", "in_progress", "paused"),
        _t("T15", "in_progress", "ready"),
        _t("T16", "dispatching in_progress waiting_user verifying", "waiting_dependency"),
        _t("T17", "verifying", "in_progress"),
        _t("T18", "verifying", "done"),
        _t("T19", "verifying", "paused"),
        _t("T20", "paused", "in_progress"),
        _t("T21", "paused", "ready"),
        _t("T22", "done", "accepted"),
        _t("T23", "done", "intake"),
        _t("T24", "accepted", "archived"),
        _t("T25", " ".join(sorted(CANCELLABLE)), "cancelled"),
        _t("T26", "cancelled", "archived"),
        _t("T27", "paused", "verifying"),
    )
}

# Worker sub-states inside in_progress (DN002 §3.3).
WORKER_TRANSITIONS: dict[str, Transition] = {
    t.id: t
    for t in (
        _t("E01", "executing", "reviewing"),
        _t("E02", "reviewing", "reviewing"),
        _t("E03", "reviewing", "triaging"),
        _t("E04", "triaging", "revising"),
        _t("E05", "revising", "reviewing"),
        _t("E06", "triaging", "executing"),
        _t("E07", "executing reviewing triaging revising", "executing"),
    )
}


def loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def next_item_id(
    conn: sqlite3.Connection, table: str, column: str, prefix: str, task_id: str
) -> str:
    """``<prefix>-<task>-<n>`` after the task's own items, skipping ids used anywhere in the table.

    A renumbered task keeps the ids it was given under its old number, so a task that later
    takes that number must not be handed the same id again.
    """
    number = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE task_id = ?", (task_id,)).fetchone()[
            "n"
        ]
    )
    while True:
        number += 1
        candidate = f"{prefix}-{task_id}-{number}"
        used = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (candidate,)).fetchone()
        if used is None:
            return candidate


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    found = row_dict(conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if found is None:
        raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
    return found


def attempt(conn: sqlite3.Connection, task_id: str, attempt_id: str) -> dict[str, Any] | None:
    return row_dict(
        conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? AND attempt = ?", (task_id, attempt_id)
        ).fetchone()
    )


def current_attempt(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = task(conn, task_id)
    if row["current_attempt"] is None:
        return None
    return attempt(conn, task_id, str(row["current_attempt"]))


def review_cycle(
    conn: sqlite3.Connection, task_id: str, attempt_id: str, cycle: int
) -> dict[str, Any]:
    found = row_dict(
        conn.execute(
            "SELECT * FROM review_cycles WHERE task_id = ? AND attempt = ? AND cycle = ?",
            (task_id, attempt_id, cycle),
        ).fetchone()
    )
    if found is None:
        return {
            "rounds": 0,
            "orchestrator_extension": 0,
            "user_extension": 0,
            "last_round": None,
            "last_reviewed_head": None,
            "last_decision": None,
            "retries": 0,
        }
    return found


def last_round_number(conn: sqlite3.Connection, task_id: str, attempt_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(round) AS r FROM review_rounds WHERE task_id = ? AND attempt = ?",
        (task_id, attempt_id),
    ).fetchone()
    return int(row["r"] or 0)


def require_status(row: dict[str, Any], *allowed: str) -> None:
    if row["status"] not in allowed:
        raise rejected(
            "任务当前状态不允许该操作",
            reason="invalid_state",
            task_id=row["task_id"],
            status=row["status"],
            allowed=list(allowed),
        )


def require_substate(row: dict[str, Any], *allowed: str) -> None:
    require_status(row, "in_progress")
    if row["substate"] not in allowed:
        raise rejected(
            "worker 子状态不允许该操作",
            reason="invalid_substate",
            substate=row["substate"],
            allowed=list(allowed),
        )
