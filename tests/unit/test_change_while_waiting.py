"""A requirement change keeps the questions the user is waiting on (DN002 §5.1, T292): the
``task.requirement_changed`` reducer against an in-memory projection."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.events import Event
from tsw.machine.ask_user import session_message
from tsw.machine.reducers import input_resolved, requirement_changed
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


# --------------------------------------------- answers and the replies the hook links to them


def _resolved(**extra: Any) -> Event:
    return Event(
        schema=1,
        sequence=10,
        time="2026-11-17T00:00:00Z",
        type="input.resolved",
        actor={"role": "orchestrator"},
        prev="",
        checksum="",
        task_id="T001",
        revision=1,
        data={"request_id": "Q-T001-1", "source": {"channel": "x"}, "answer": "好", **extra},
    )


def test_an_answer_recorded_before_t292_reduces_as_before(conn: sqlite3.Connection) -> None:
    input_resolved(conn, _resolved())
    row = conn.execute("SELECT resolution FROM input_requests WHERE request_id = 'Q-T001-1'")
    assert json.loads(row.fetchone()["resolution"]) == {"source": {"channel": "x"}, "answer": "好"}


def test_an_answer_keeps_the_revision_it_took_effect_in(conn: sqlite3.Connection) -> None:
    input_resolved(conn, _resolved(answered_revision=2))
    row = conn.execute("SELECT resolution FROM input_requests WHERE request_id = 'Q-T001-1'")
    assert json.loads(row.fetchone()["resolution"])["revision"] == 2


def _linked(conn: sqlite3.Connection, resolution: dict[str, Any], revision: int) -> bool:
    conn.execute(
        "INSERT INTO in_session_requests(request_id, task_id, session_ref, asked_at, "
        "handoff_after, reserved) VALUES('Q-T001-1', 'T001', 'w-1', 't', 't', 0)"
    )
    conn.execute(
        "UPDATE input_requests SET status = 'resolved', resolution = ? "
        "WHERE request_id = 'Q-T001-1'",
        (json.dumps(resolution),),
    )
    conn.execute("UPDATE tasks SET revision = ?", (revision,))
    draft = session_message(conn, "T001", "w-1", "用户的回复")
    assert draft is not None
    return draft.data["request_id"] == "Q-T001-1"


@pytest.mark.parametrize(
    ("resolution", "revision", "linked"),
    [
        # Answered before T292: the question's own revision (1) decides.
        ({"answer": "好"}, 1, True),
        ({"answer": "好"}, 2, False),
        # Answered in revision 2 after a change while waiting: linked until the next change.
        ({"answer": "好", "revision": 2}, 2, True),
        ({"answer": "好", "revision": 2}, 3, False),
    ],
)
def test_a_late_reply_belongs_to_the_answered_question_until_the_next_change(
    conn: sqlite3.Connection, resolution: dict[str, Any], revision: int, linked: bool
) -> None:
    assert _linked(conn, resolution, revision) is linked
