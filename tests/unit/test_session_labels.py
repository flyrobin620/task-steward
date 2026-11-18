"""Session labels for an embedding portal from an in-memory projection (UC-DIRECT-008, DN006
§8.10, T297). The expected labels are the ones workdeck's tsw annotator made from the same rows
(``readAnnotations`` in its ``src/annotators/tsw.ts``; the title is its ``detail`` as the portal
showed it, one ``key: value`` per line)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.errors import TswError
from tsw.host.session_labels import MAX_LABELS, MAX_TEXT, all_labels, answer, requested_ids
from tsw.projection import SCHEMA_SQL

mark = pytest.mark


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def attempt(conn: sqlite3.Connection, task: str, name: str, phase: str, ref: str | None) -> None:
    conn.execute(
        "INSERT INTO attempts(task_id, attempt, phase, session_ref) VALUES (?, ?, ?, ?)",
        (task, name, phase, ref),
    )


def lease(conn: sqlite3.Connection, name: str, holder: str | None) -> None:
    conn.execute(
        "INSERT INTO leases(name, holder_session, source_seq) VALUES (?, ?, 1)", (name, holder)
    )


def routine(conn: sqlite3.Connection, generation: int, status: str, ref: str | None) -> None:
    conn.execute(
        "INSERT INTO routine_sessions(generation, session_name, workdir, status, session_ref, "
        "requested_at) VALUES (?, 'routine', 'w', ?, ?, 't')",
        (generation, status, ref),
    )


def worker(task: str, name: str, phase: str) -> dict[str, str]:
    return {
        "text": f"{task} · {name} · worker",
        "title": f"task: {task}\nattempt: {name}\nphase: {phase}\nrole: worker",
        "link": f"/tasks/{task}",
    }


ORCHESTRATOR = {
    "text": "orchestrator",
    "title": "role: orchestrator\nlease: scheduler",
    "link": "/",
}


@mark.use_case("UC-DIRECT-008")
def test_labels_match_the_annotator_by_role(conn: sqlite3.Connection) -> None:
    attempt(conn, "T001", "a1", "closed", "w-1")
    attempt(conn, "T001", "a2", "live", "w-2")
    attempt(conn, "T002", "a1", "starting", None)  # not bound yet: no session, no label
    attempt(conn, "T003", "a1", "standby", "orch")  # the orchestrator also works on T003
    lease(conn, "scheduler", "orch")
    lease(conn, "other", None)
    routine(conn, 1, "replaced", "r-1")
    routine(conn, 2, "bound", "r-2")
    routine(conn, 3, "requested", None)
    assert all_labels(conn) == {
        "w-1": [worker("T001", "a1", "closed")],
        "w-2": [worker("T001", "a2", "live")],
        # Several labels on one session: workers first, then the orchestrator.
        "orch": [worker("T003", "a1", "standby"), ORCHESTRATOR],
        # A routine session works on no task: no link.
        "r-1": [{"text": "routine g1", "title": "role: routine\ngeneration: 1\nphase: replaced"}],
        "r-2": [{"text": "routine g2", "title": "role: routine\ngeneration: 2\nphase: bound"}],
    }


@mark.use_case("UC-DIRECT-008")
def test_the_answer_has_every_session_asked_for_within_the_limits(
    conn: sqlite3.Connection,
) -> None:
    long_task = "T" + "9" * 400
    attempt(conn, long_task, "a1", "live", "w-long")
    attempt(conn, "T 1/x", "a1", "live", "w-odd")
    for n in range(MAX_LABELS + 5):
        attempt(conn, f"T{n:03}", "a1", "closed", "w-many")
    out = answer(conn, ["w-long", "nobody", "w-odd", "w-many"])["labels"]
    assert list(out) == ["w-long", "nobody", "w-odd", "w-many"]
    assert out["nobody"] == []
    text, title = out["w-long"][0]["text"], out["w-long"][0]["title"]
    assert len(text) == MAX_TEXT == len(title) and text.startswith(long_task[:100])
    assert out["w-odd"][0]["link"] == "/tasks/T%201%2Fx"  # one path segment, encoded
    assert len(out["w-many"]) == MAX_LABELS and out["w-many"][0]["text"] == "T000 · a1 · worker"


@mark.use_case("UC-DIRECT-008")
@pytest.mark.parametrize(
    ("body", "ids"),
    [
        ({"sessions": []}, []),
        ({"sessions": [{"id": "a"}, {"id": "b", "kind": "new"}], "since": 3}, ["a", "b"]),
        ({"sessions": [{"id": "a"}, {"id": "a"}]}, ["a"]),
        (
            {"sessions": [{"id": ""}, {"id": 7}, {}, "c", {"id": "x" * 513}, {"id": "x" * 512}]},
            ["x" * 512],
        ),
    ],
)
def test_the_request_lists_session_ids(body: Any, ids: list[str]) -> None:
    assert requested_ids(body) == ids


@mark.use_case("UC-DIRECT-008")
@pytest.mark.parametrize("body", [None, [], {}, {"sessions": {"id": "a"}}, {"sessions": "a"}])
def test_a_request_without_a_session_list_is_refused(body: Any) -> None:
    with pytest.raises(TswError) as exc:
        requested_ids(body)
    assert exc.value.details["reason"] == "invalid_input"
