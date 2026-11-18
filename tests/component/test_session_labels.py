"""Session labels for an embedding portal from a real data directory, ledger and projection
(UC-DIRECT-008, DN006 §8.10, T297): the sessions tsw records through its tools get the labels
workdeck's tsw annotator gave them, and asking writes nothing."""

from __future__ import annotations

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.status_app import StatusApp

mark = pytest.mark


@mark.use_case("UC-DIRECT-008")
def test_sessions_recorded_by_the_tools_are_labelled(home: Home) -> None:
    h = Harness(home)
    for task in ("T001", "T002"):
        h.register(task)
        h.plan(task)
    first = h.dispatch("T001", session="local_w1")
    h.dispatch("T002", session="local_w2")
    started = h.run("routine_start", user_turn=True)["events"][0]["data"]
    h.run("routine_bind", generation=started["generation"], session_id="local_r1")
    app = StatusApp(home, token="tok")
    tail = h.ledger.storage.tail()
    out = app.labels(
        {
            "sessions": [
                {"id": "orch-1"},
                {"id": str(first.session_id)},
                {"id": "local_w2"},
                {"id": "local_r1"},
                {"id": "local_other"},
            ]
        }
    )
    phase = {r["task_id"]: r["phase"] for r in h.query("SELECT task_id, phase FROM attempts")}
    assert out == {
        "labels": {
            "orch-1": [
                {
                    "text": "orchestrator",
                    "title": "role: orchestrator\nlease: scheduler",
                    "link": "/",
                }
            ],
            "local_w1": [
                {
                    "text": "T001 · a1 · worker",
                    "title": f"task: T001\nattempt: a1\nphase: {phase['T001']}\nrole: worker",
                    "link": "/tasks/T001",
                }
            ],
            "local_w2": [
                {
                    "text": "T002 · a1 · worker",
                    "title": f"task: T002\nattempt: a1\nphase: {phase['T002']}\nrole: worker",
                    "link": "/tasks/T002",
                }
            ],
            "local_r1": [
                {
                    "text": f"routine g{started['generation']}",
                    "title": f"role: routine\ngeneration: {started['generation']}\nphase: bound",
                }
            ],
            "local_other": [],
        }
    }
    assert phase["T001"] == "live"
    # Reading labels writes nothing to the ledger.
    assert h.ledger.storage.tail() == tail


@mark.use_case("UC-DIRECT-008")
def test_a_change_in_the_ledger_shows_at_the_next_query(home: Home) -> None:
    h = Harness(home)
    app = StatusApp(home, token="tok")
    ask = {"sessions": [{"id": "orch-1"}]}
    assert [x["text"] for x in app.labels(ask)["labels"]["orch-1"]] == ["orchestrator"]
    # The orchestrator hands the lease back: the same session is no longer labelled.
    h.ledger.commit(lease.release("orch-1", machine=MACHINE, actor=ORCH.actor()))
    assert app.labels(ask) == {"labels": {"orch-1": []}}


@mark.use_case("UC-DIRECT-008")
def test_a_body_without_sessions_is_refused(home: Home) -> None:
    Harness(home)
    with pytest.raises(TswError) as exc:
        StatusApp(home, token="tok").labels({"session": []})
    assert exc.value.details["reason"] == "invalid_input"
