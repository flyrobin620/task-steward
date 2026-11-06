"""The task.renumbered reducer: only the system's own tasks follow the renumbered task."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tsw.events import Event
from tsw.host.status import _href  # pyright: ignore[reportPrivateUsage]
from tsw.importer.renumber import reduce
from tsw.machine.model import next_item_id
from tsw.projection import SCHEMA_SQL

pytestmark = pytest.mark.use_case("UC-IMPORT-001")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _task(conn: sqlite3.Connection, task_id: str, **extra: str) -> None:
    columns = {
        "task_id": task_id,
        "title": task_id,
        "repo": "demo",
        "kind": "code",
        "status": "accepted",
        "spec_path": f"todo/{task_id}.md",
        "spec_sha256": "x",
        "registered_seq": 1,
        "last_activity": "t",
        "source_seq": 1,
        "goal": "g",
        "acceptance": "[]",
        "revision": 1,
        "version": 1,
        "business_changed_at": "t",
        **extra,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO tasks({names}) VALUES({marks})", tuple(columns.values()))


def test_submissions_read_sets_and_dependencies_of_local_tasks_follow() -> None:
    conn = _conn()
    _task(conn, "T001")
    submission = {"revision": 1, "dependencies": {"T001": 1}}
    _task(conn, "T002", status="verifying", submission=json.dumps(submission))
    _task(conn, "T008")  # imported: its T001 is the source system's task
    _task(conn, "T009", submission=json.dumps({"revision": 1, "dependencies": {"T001": 1}}))
    for task_id in ("T008", "T009"):
        conn.execute(
            "INSERT INTO imported_tasks(task_id, source, event_seq, source_sequence, old_status, "
            "resumable, runs, legacy) VALUES(?, 'orchestrator-agent', 1, 1, 'done', 1, NULL, '{}')",
            (task_id,),
        )
    for task_id in ("T002", "T008"):
        conn.execute(
            "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
            "VALUES(?, 'T001', 1, 'explicit', 'r')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO attempts(task_id, attempt, phase, read_set) VALUES(?, 'a1', 'live', ?)",
            (task_id, json.dumps(sorted([task_id, "T001"]))),
        )
    event = Event(
        schema=1,
        sequence=9,
        time="t",
        type="task.renumbered",
        actor={},
        prev="",
        checksum="",
        task_id="T011",
        data={
            "from": "T001",
            "spec_path": "todo/T011.md",
            "moved": {"runs/T001/": "runs/T011/"},
        },
    )
    reduce(conn, event)

    def one(sql: str, *args: str) -> str:
        return str(conn.execute(sql, args).fetchone()[0])

    assert json.loads(one("SELECT submission FROM tasks WHERE task_id = 'T002'"))[
        "dependencies"
    ] == {"T011": 1}
    assert json.loads(one("SELECT submission FROM tasks WHERE task_id = 'T009'"))[
        "dependencies"
    ] == {"T001": 1}
    assert one("SELECT dep_id FROM dependencies WHERE task_id = 'T002'") == "T011"
    assert one("SELECT dep_id FROM dependencies WHERE task_id = 'T008'") == "T001"
    assert json.loads(one("SELECT read_set FROM attempts WHERE task_id = 'T002'")) == [
        "T002",
        "T011",
    ]
    assert json.loads(one("SELECT read_set FROM attempts WHERE task_id = 'T008'")) == [
        "T001",
        "T008",
    ]


def test_new_item_ids_skip_ids_a_renumbered_task_kept() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO input_requests(request_id, task_id, kind, status, question, binding) "
        "VALUES('Q-T001-1', 'T011', 'question', 'resolved', 'q', '{}')"
    )
    assert next_item_id(conn, "input_requests", "request_id", "Q", "T001") == "Q-T001-2"
    assert next_item_id(conn, "input_requests", "request_id", "Q", "T011") == "Q-T011-2"
    for n in (1, 2):  # consecutive ids kept by the renumbered task are all skipped
        conn.execute(
            "INSERT INTO decisions(decision_id, task_id, trigger, decision, rationale, basis, "
            "confidence, sequence) VALUES(?, 'T011', 't', 'd', 'r', 'b', 'c', 1)",
            (f"D-T001-{n}",),
        )
    assert next_item_id(conn, "decisions", "decision_id", "D", "T001") == "D-T001-3"


def test_status_links_to_local_results_are_percent_encoded() -> None:
    assert _href("runs/T004/a1/report#1.html") == "../runs/T004/a1/report%231.html"
    assert _href("legacy://orchestrator-agent/runs/T1/a 1/x?.md") == (
        "../legacy/orchestrator-agent/runs/T1/a%201/x%3F.md"
    )
    assert _href("https://github.com/o/r/pull/7") == "https://github.com/o/r/pull/7"
