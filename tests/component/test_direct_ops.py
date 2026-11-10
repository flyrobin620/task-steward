"""The user's direct operations outside any session (DN006 §8.10, UC-DIRECT-001)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw import direct
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.status_app import history
from tsw.machine.base import Caller

mark = pytest.mark
USER = Caller(role="user", session_id=None, machine="m1")


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as caught:
        call()
    return str(caught.value.details.get("reason"))


def perform(h: Harness, action: str, task_id: str, **extra: Any) -> dict[str, Any]:
    return direct.perform(
        h.ledger,
        h.config,
        action,
        task_id,
        channel=extra.pop("channel", "user-cli"),
        message_ref=extra.pop("message_ref", "cli:2026-11-10T00:00:00Z:1"),
        text=extra.pop("text", f"tsw task {action} {task_id}"),
        **extra,
    )


@mark.use_case("UC-DIRECT-001")
@mark.transition("T22")
@mark.transition("G-USER")
def test_the_user_accepts_from_the_terminal_and_the_source_names_the_cli(h: Harness) -> None:
    h.complete("T001")
    row = h.task("T001")
    with h.ledger.view() as conn:
        found = direct.availability(conn, row)
    assert found.actions[:2] == ("accept", "reject")
    result = perform(h, "accept", "T001", text="tsw task accept T001：看过报告，可以")
    assert result["event"] == "task.user_accepted"
    assert h.status("T001") == "accepted"
    event = next(e for e in h.ledger.storage.read(since=0) if e.sequence == result["sequence"])
    source = event.data["user_source"]
    assert source["channel"] == "user-cli"
    assert source["target"] == ["T001", 1, "accept"]
    assert "@" in source["principal"]
    assert source["text"] == "tsw task accept T001：看过报告，可以"
    assert event.actor == {"role": "user"}


@mark.use_case("UC-DIRECT-001")
@mark.transition("T23")
def test_the_user_rejects_with_a_reason_and_the_task_returns_to_intake(h: Harness) -> None:
    h.complete("T001")
    assert reason_of(lambda: perform(h, "reject", "T001")) == "invalid_input"
    perform(h, "reject", "T001", reason="少了导出功能", channel="status-app")
    row = h.task("T001")
    assert (row["status"], row["revision"]) == ("intake", 2)


@mark.use_case("UC-DIRECT-001")
@mark.transition("G-USER")
def test_a_decision_on_an_older_revision_is_refused(h: Harness) -> None:
    h.complete("T001")
    assert reason_of(lambda: perform(h, "accept", "T001", revision=2)) == "g_user"
    assert h.status("T001") == "done"


@mark.use_case("UC-DIRECT-001")
@mark.transition("G-USER")
def test_the_direct_channels_belong_to_the_user_caller_only(h: Harness) -> None:
    h.complete("T001")
    cli_source = {**h.source("T001", 1, "accept"), "channel": "user-cli"}
    # The orchestrator cannot record the user's own terminal or page operation...
    assert reason_of(lambda: h.run("user_accept", task_id="T001", user_source=cli_source)) == (
        "g_user"
    )
    # ...and the user caller records nothing else.
    session_source = h.source("T001", 1, "accept")
    assert (
        reason_of(lambda: h.run("user_accept", USER, task_id="T001", user_source=session_source))
        == "g_user"
    )
    # The user caller runs only the direct actions.
    archive = {"task_id": "T001", "user_source": h.source("T001", 1, "archive")}
    assert reason_of(lambda: h.run("task_archive", USER, **archive)) == "role_denied"
    assert h.status("T001") == "done"


@mark.use_case("UC-DIRECT-001")
@mark.transition("T25")
def test_cancel_is_offered_only_without_an_active_worker(h: Harness) -> None:
    h.register("T001")
    h.register("T002")
    h.plan("T002")
    h.dispatch("T002")
    rows = h.task("T001"), h.task("T002")
    with h.ledger.view() as conn:
        idle, busy = (direct.availability(conn, row) for row in rows)
    assert idle.actions == ("cancel",)
    assert busy.actions == ()
    assert busy.cancel_blocked is not None and "worker" in busy.cancel_blocked
    assert reason_of(lambda: perform(h, "cancel", "T002")) == "worker_not_stopped"
    perform(h, "cancel", "T001")
    assert h.status("T001") == "cancelled"
    row = h.task("T001")
    with h.ledger.view() as conn:
        assert direct.availability(conn, row).actions == ()


def test_the_direct_operations_refuse_inside_a_host_session() -> None:
    for marker in direct.SESSION_MARKERS:
        assert direct.in_host_session({marker: "x"})
        with pytest.raises(TswError) as caught:
            direct.require_outside_session({marker: "x"}, "tsw task accept ")
        assert caught.value.details["reason"] == "host_session_cli"
    assert not direct.in_host_session({})


@mark.use_case("UC-DIRECT-002")
def test_the_status_app_reads_every_revision_from_the_ledger(h: Harness) -> None:
    h.complete("T001")
    perform(h, "reject", "T001", reason="缺少导出")
    h.run(
        "task_update",
        task_id="T001",
        goal="实现功能并导出",
        acceptance=["功能可用", "可以导出"],
        user_source=h.source("T001", 2, "change", text="加上导出"),
    )
    past = history(h.ledger.storage.read(since=0))["T001"]
    assert [(r.revision, r.how) for r in past] == [(1, "登记"), (2, "用户退回"), (3, "需求变更")]
    assert past[1].words == "缺少导出" and past[1].goal == past[0].goal
    assert (past[2].goal, past[2].acceptance, past[2].words) == (
        "实现功能并导出",
        ("功能可用", "可以导出"),
        "加上导出",
    )
    assert h.task("T001")["revision"] == 3
