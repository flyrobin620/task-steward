"""A requirement change keeps the questions the user is waiting on (DN002 §5.1, T292): the
``task.requirement_changed`` reducer against an in-memory projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.events import Event
from tsw.machine.reducers import requirement_changed
from tsw.projection import SCHEMA_SQL

pytestmark = [pytest.mark.use_case("UC-CHANGE-001"), pytest.mark.transition("T12")]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO tasks(task_id, title, repo, kind, status, substate, revision, cycle, version, "
        "spec_path, spec_sha256, registered_seq, last_activity, source_seq, goal, acceptance) "
        "VALUES('T001', 't', 'demo', 'code', 'waiting_user', 'triaging', 1, 1, 1, 'todo/T001.md', "
        "'x', 1, 't', 1, 'g', '[\"a\"]')"
    )
    for request_id in ("Q-T001-1", "Q-T001-2"):
        db.execute(
            "INSERT INTO input_requests(request_id, task_id, kind, status, question, binding) "
            "VALUES(?, 'T001', 'in_session', 'pending', 'q', '{\"revision\": 1}')",
            (request_id,),
        )
    yield db
    db.close()


def _changed(**extra: Any) -> Event:
    return Event(
        schema=1,
        sequence=9,
        time="2026-11-17T00:00:00Z",
        type="task.requirement_changed",
        actor={"role": "orchestrator"},
        prev="",
        checksum="",
        task_id="T001",
        revision=2,
        data={
            "revision": 2,
            "goal": "新目标",
            "acceptance": ["新验收"],
            "spec_sha256": "y",
            "user_source": {},
            **extra,
        },
    )


def _statuses(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["request_id"]: r["status"]
        for r in conn.execute("SELECT request_id, status FROM input_requests")
    }


def test_a_change_while_waiting_keeps_the_questions_and_the_state(
    conn: sqlite3.Connection,
) -> None:
    requirement_changed(conn, _changed(superseded_requests=[]))
    assert _statuses(conn) == {"Q-T001-1": "pending", "Q-T001-2": "pending"}
    row = conn.execute("SELECT status, substate, revision, cycle FROM tasks").fetchone()
    # No E07 while waiting: the answer opens the new cycle (T12 across revisions).
    assert tuple(row) == ("waiting_user", "triaging", 2, 1)


def test_the_change_voids_only_the_requests_it_names(conn: sqlite3.Connection) -> None:
    requirement_changed(conn, _changed(superseded_requests=["Q-T001-2"]))
    assert _statuses(conn) == {"Q-T001-1": "pending", "Q-T001-2": "superseded"}


def test_a_change_recorded_before_t292_voids_every_pending_request(
    conn: sqlite3.Connection,
) -> None:
    requirement_changed(conn, _changed())  # no list: the ledger replays as it was written
    assert _statuses(conn) == {"Q-T001-1": "superseded", "Q-T001-2": "superseded"}
