"""The state machine driven through its tools (DN002): planning, dispatch and lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.machine.base import Caller


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as info:
        call()
    return str(info.value.details.get("reason"))


mark = pytest.mark


@mark.use_case("UC-PLAN-001")
@mark.transition("T02")
@mark.transition("T04")
@mark.transition("G-PLAN")
def test_intake_question_blocks_planning_until_the_user_answers(h: Harness) -> None:
    h.register("T001")
    asked = h.run("intake_question", task_id="T001", question="导出包含哪些列？")
    request = asked["events"][0]["data"]["request_id"]
    assert h.status("T001") == "intake"
    assert reason_of(lambda: h.plan("T001")) == "pending_questions"
    assert (
        reason_of(
            lambda: h.run(
                "input_resolve",
                task_id="T001",
                request_id=request,
                answer="全部",
                user_source=h.source("T001", 1, "reject", request_id=request),
            )
        )
        == "g_user"
    )

    def wrong_request() -> None:
        h.run(
            "input_resolve",
            task_id="T001",
            request_id=request,
            answer="全部",
            user_source=h.source("T001", 1, "answer", request_id="Q-T001-other"),
        )

    assert reason_of(wrong_request) == "g_user"
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request,
        answer="全部",
        user_source=h.source("T001", 1, "answer", request_id=request),
    )
    h.plan("T001")
    assert h.status("T001") == "ready"


@mark.use_case("UC-PLAN-001")
@mark.transition("G-PLAN")
def test_plan_needs_a_configured_repository_and_no_cycles(h: Harness) -> None:
    h.register("T001", repo="unknown")
    assert reason_of(lambda: h.plan("T001")) == "g_plan"
    h.register("T002")
    h.register("T003")
    h.plan("T002", ["T003"])
    assert reason_of(lambda: h.plan("T003", ["T002"])) == "dependency_cycle"
    h.register("T004")
    assert reason_of(lambda: h.plan("T004", ["T004"])) == "dependency_self"


@mark.use_case("UC-PLAN-001")
@mark.transition("T03")
@mark.transition("T05")
@mark.transition("G-DEP")
def test_waiting_task_becomes_ready_when_its_dependency_is_done(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.register("T002")
    h.plan("T002", ["T001"])
    assert h.status("T002") == "waiting_dependency"
    worker = h.dispatch("T001")
    h.finalize(worker, "T001")
    h.submit(worker, "T001")
    result = h.accept("T001")
    assert [e["type"] for e in result["sync"]["events"]] == ["dependency.satisfied"]
    assert h.status("T002") == "ready"


@mark.use_case("UC-LIFE-001")
@mark.transition("T06")
@mark.transition("T23")
def test_rejected_upstream_sends_ready_dependents_back_to_waiting(h: Harness) -> None:
    h.complete("T001")
    h.register("T002")
    h.plan("T002", ["T001"])
    assert h.status("T002") == "ready"
    h.run("user_reject", task_id="T001", reason="不对", user_source=h.source("T001", 1, "reject"))
    assert h.task("T001")["status"] == "intake"
    assert h.task("T001")["revision"] == 2
    assert h.status("T002") == "waiting_dependency"


@mark.use_case("UC-DISPATCH-001")
@mark.transition("T07")
@mark.transition("T08")
@mark.transition("G-WINDOW")
@mark.transition("G-LEASE")
def test_dispatch_creates_an_attempt_and_binds_the_session(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    other = Caller(role="orchestrator", session_id="orch-2", machine=MACHINE)
    assert (
        reason_of(
            lambda: h.run("task_dispatch", other, task_id="T001", workdir="w", user_turn=True)
        )
        == "lease_held"
    )
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T001", workdir="w", user_turn=False))
        == "g_window"
    )
    worker = h.dispatch("T001")
    row = h.task("T001")
    assert (row["status"], row["substate"], row["cycle"], row["current_attempt"]) == (
        "in_progress",
        "executing",
        1,
        "a1",
    )
    attempt = h.query("SELECT * FROM attempts")[0]
    assert (attempt["phase"], attempt["session_ref"], attempt["generation"]) == (
        "live",
        worker.session_id,
        1,
    )
    assert (h.home.root / "runs" / "T001" / "a1" / "brief.md").is_file()


@mark.use_case("UC-DISPATCH-001")
@mark.transition("G-SLOT")
@mark.transition("G-RES")
def test_slots_are_limited(home: Home) -> None:
    h = Harness(home, {"scheduling": {"max_workers": 1}})
    for task_id in ("T001", "T002", "T003"):
        h.register(task_id)
    h.plan("T001", resources=["db"])
    h.plan("T002")
    h.plan("T003", resources=["db"])
    h.dispatch("T001")
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T002", workdir="w", user_turn=True))
        == "g_slot"
    )


@mark.use_case("UC-DISPATCH-001")
@mark.transition("G-RES")
def test_resources_block_other_tasks_until_released(home: Home) -> None:
    h = Harness(home, {"scheduling": {"max_workers": 5}})
    h.register("T001")
    h.register("T002")
    h.plan("T001", resources=["db"])
    h.plan("T002", resources=["db"])
    h.dispatch("T001")
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T002", workdir="w", user_turn=True))
        == "g_res"
    )


@mark.use_case("UC-DISPATCH-001")
@mark.transition("T09")
def test_session_not_created_returns_to_ready_and_late_sessions_are_recorded(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", workdir="w", user_turn=True)
    first = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)["events"][0][
        "data"
    ]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=first["generation"],
        outcome="not_created",
    )
    assert h.status("T001") == "ready"
    assert h.query("SELECT phase, generation_valid FROM attempts")[0] == {
        "phase": "planned",
        "generation_valid": 0,
    }
    h.run("task_dispatch", task_id="T001", workdir="w", user_turn=True)
    second = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)["events"][
        0
    ]["data"]
    assert (second["attempt"], second["generation"], second["session_name"]) == (
        "a1",
        2,
        "t001-a1-g2",
    )
    late = h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=1,
        outcome="created",
        session_ref="old",
    )
    assert late["events"][0]["type"] == "attempt.late_session"
    assert h.status("T001") == "dispatching"


@mark.use_case("UC-DISPATCH-001")
@mark.transition("T10")
@mark.transition("T21")
@mark.transition("G-PAUSE")
def test_workdir_conflict_pauses_and_resumes_to_ready(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", workdir="w", user_turn=True)
    pause = {"owner": "用户", "next_action": "决定分支", "resume_condition": "用户选择"}
    assert (
        reason_of(lambda: h.run("task_pause", task_id="T001", category="external", **pause))
        == "invalid_state"
    )
    h.run("task_pause", task_id="T001", category="workdir_conflict", **pause)
    assert h.status("T001") == "paused"
    h.run("task_resume", task_id="T001", condition_met=True, note="用户选择保留")
    assert h.status("T001") == "ready"


@mark.use_case("UC-DISPATCH-001")
@mark.transition("G-ONE-ATTEMPT")
def test_prestart_then_deliver_the_brief_in_a_later_turn(h: Harness) -> None:
    h.register("T001")
    h.register("T002")
    h.plan("T001")
    h.plan("T002", ["T001"])
    started = h.run(
        "attempt_start", task_id="T002", purpose="prestart", user_turn=True, workdir="w"
    )["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T002",
        attempt="a1",
        generation=started["generation"],
        outcome="created",
        session_ref="w-T002",
    )
    assert h.query("SELECT phase FROM attempts WHERE task_id = 'T002'")[0]["phase"] == "standby"
    assert (
        reason_of(
            lambda: h.run(
                "attempt_start", task_id="T002", purpose="prestart", user_turn=True, workdir="w"
            )
        )
        == "g_one_attempt"
    )
    worker = h.dispatch("T001")
    h.finalize(worker, "T001")
    h.submit(worker, "T001")
    h.accept("T001")
    assert h.status("T002") == "ready"
    h.run(
        "task_dispatch", task_id="T002", workdir="w", user_turn=False
    )  # no window needed for standby
    h.run("attempt_deliver", task_id="T002")
    assert h.status("T002") == "in_progress"


@mark.use_case("UC-DISPATCH-001")
def test_prestart_limit(home: Home) -> None:
    h = Harness(home, {"scheduling": {"prestart": {"enabled": True, "max_standby": 1}}})
    h.register("T001")
    h.register("T002")
    h.plan("T001")
    h.plan("T002")
    h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True, workdir="w")
    assert (
        reason_of(
            lambda: h.run(
                "attempt_start", task_id="T002", purpose="prestart", user_turn=True, workdir="w"
            )
        )
        == "max_standby"
    )


@mark.use_case("UC-DISPATCH-001")
@mark.transition("T15")
def test_lost_session_returns_to_ready_and_redispatch_uses_a_new_attempt(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    h.run(
        "attempt_update",
        task_id="T001",
        attempt="a1",
        event="lost",
        evidence="get_session: not found",
    )
    assert h.status("T001") == "ready"
    h.dispatch("T001")
    assert h.task("T001")["current_attempt"] == "a2"


@mark.use_case("UC-DISPATCH-001")
@mark.use_case("UC-LEASE-001")
def test_a_handed_over_orchestrators_sessions_go_with_the_lease(h: Harness) -> None:
    """T254 (PR review): after orch-1 releases the lease, orch-2 holding it controls the
    sessions orch-1 started: it delivers to a standby session and continues a stopped one."""
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    h.register("T002")
    h.plan("T002")
    started = h.run("attempt_start", task_id="T002", purpose="prestart", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T002",
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref="w-T002-standby",
    )
    h.run(
        "task_pause",
        Caller("worker", "w-T001-1", MACHINE),
        task_id="T001",
        category="external",
        owner="外部团队",
        next_action="等待接口",
        resume_condition="接口上线",
        worker_stopped=True,
    )
    h.ledger.commit(lease.release("orch-1", machine=MACHINE, actor={"role": "orchestrator"}))
    h.ledger.commit(
        lease.acquire(
            "orch-2", machine=MACHINE, lease_minutes=10_000, actor={"role": "orchestrator"}
        )
    )
    orch2 = Caller("orchestrator", "orch-2", MACHINE)
    h.run("task_dispatch", orch2, task_id="T002", workdir=str(data.get("workdir") or "w"))
    h.run("attempt_deliver", orch2, task_id="T002")
    assert h.status("T002") == "in_progress"
    h.run("task_resume", orch2, task_id="T001", condition_met=True, note="接口已上线")
    assert h.status("T001") == "in_progress"  # T20: the stopped session is continued


@mark.use_case("UC-DISPATCH-001")
def test_attempt_from_another_orchestrator_is_not_continued(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    h.run(
        "task_pause",
        Caller("worker", "w-T001-1", MACHINE),
        task_id="T001",
        category="external",
        owner="外部团队",
        next_action="等待接口",
        resume_condition="接口上线",
        worker_stopped=True,
    )
    h.ledger.commit(
        lease.takeover(
            "orch-2",
            machine=MACHINE,
            from_session="orch-1",
            user_source={
                "channel": "main-session",
                "principal": "o",
                "message_ref": "m",
                "text": "接管",
            },
            lease_minutes=10_000,
            from_machine=False,
            actor={"role": "orchestrator"},
        )
    )
    orch2 = Caller("orchestrator", "orch-2", MACHINE)
    h.run("task_resume", orch2, task_id="T001", condition_met=True, note="接口已上线")
    assert h.status("T001") == "ready"  # T21: the stopped session belongs to orch-1
    assert (
        reason_of(
            lambda: h.run("task_dispatch", orch2, task_id="T001", workdir="w", user_turn=True)
        )
        == "attempt_uncontrollable"
    )
    h.run(
        "attempt_update",
        orch2,
        task_id="T001",
        attempt="a1",
        event="replaced",
        evidence="旧会话已停止",
    )
    h.run("task_dispatch", orch2, task_id="T001", workdir="w", user_turn=True)
    assert h.task("T001")["current_attempt"] == "a2"


@mark.use_case("UC-LIFE-001")
@mark.transition("T14")
@mark.transition("T20")
def test_worker_pause_and_resume_continues_the_same_session(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    worker = h.dispatch("T001")
    assert (
        reason_of(
            lambda: h.run(
                "task_pause",
                worker,
                task_id="T001",
                category="external",
                owner="o",
                next_action="n",
                resume_condition="r",
            )
        )
        == "worker_not_stopped"
    )
    h.run(
        "task_pause",
        worker,
        task_id="T001",
        category="external",
        owner="o",
        next_action="n",
        resume_condition="r",
        worker_stopped=True,
    )
    assert h.status("T001") == "paused"
    h.run("task_resume", task_id="T001", condition_met=True, note="条件满足")
    row = h.task("T001")
    assert (row["status"], row["cycle"], row["substate"]) == ("in_progress", 2, "executing")


@mark.use_case("UC-LIFE-001")
@mark.transition("T16")
def test_invalidated_dependency_stops_a_running_dependent(h: Harness) -> None:
    h.complete("T001")
    h.register("T002")
    h.plan("T002", ["T001"], resources=["db"])
    h.dispatch("T002")
    result = h.run(
        "user_reject", task_id="T001", reason="退回", user_source=h.source("T001", 1, "reject")
    )
    assert result["sync"]["needs_stop"][0]["task_id"] == "T002"
    assert h.status("T002") == "in_progress"
    assert reason_of(lambda: h.run("dependency_invalidate", task_id="T002")) == "worker_not_stopped"
    h.run("dependency_invalidate", task_id="T002", worker_stopped=True)
    assert h.status("T002") == "waiting_dependency"
    assert h.query("SELECT status FROM resource_claims")[0]["status"] == "released"


@mark.use_case("UC-LIFE-001")
@mark.transition("T22")
@mark.transition("T24")
@mark.transition("G-USER")
def test_user_acceptance_and_automatic_archiving(h: Harness) -> None:
    h.complete("T001")
    assert (
        reason_of(
            lambda: h.run("user_accept", task_id="T001", user_source={"channel": "main-session"})
        )
        == "g_user"
    )
    h.run("user_accept", task_id="T001", user_source=h.source("T001", 1, "accept"))
    assert h.status("T001") == "accepted"
    h.clock.now = "2026-11-11T23:59:59Z"
    h.run("sync")
    assert h.status("T001") == "accepted"
    h.clock.now = "2026-11-12T00:00:00Z"
    h.run("sync")
    assert h.status("T001") == "archived"


@mark.use_case("UC-LIFE-001")
@mark.transition("T25")
@mark.transition("T26")
def test_cancel_marks_dependents_stale_and_archive(h: Harness) -> None:
    h.complete("T001")
    h.register("T002")
    h.plan("T002", ["T001"])
    worker = h.dispatch("T002")
    h.finalize(worker, "T002")
    h.submit(worker, "T002")
    h.accept("T002")
    assert (
        reason_of(
            lambda: h.run("task_cancel", task_id="T001", user_source=h.source("T001", 1, "cancel"))
        )
        == "worker_not_stopped"
    )
    h.run(
        "task_cancel",
        task_id="T001",
        worker_stopped=True,
        user_source=h.source("T001", 1, "cancel"),
    )
    assert h.status("T001") == "cancelled"
    assert h.task("T002")["stale"] == 1
    assert (
        reason_of(
            lambda: h.run("user_accept", task_id="T002", user_source=h.source("T002", 1, "accept"))
        )
        == "stale"
    )
    h.run("stale_keep", task_id="T002", user_source=h.source("T002", 1, "keep"))
    assert h.task("T002")["stale"] == 0
    h.run("task_archive", task_id="T001", user_source=h.source("T001", 1, "archive"))
    assert h.status("T001") == "archived"


@mark.use_case("UC-PLAN-001")
def test_requirement_change_while_ready_and_rebind(h: Harness) -> None:
    h.complete("T001")
    h.register("T002")
    h.plan("T002", ["T001"])
    h.run("user_reject", task_id="T001", reason="r", user_source=h.source("T001", 1, "reject"))
    assert (
        reason_of(
            lambda: h.run("dependency_rebind", task_id="T002", dep_id="T009", revalidate_scope="x")
        )
        == "dependency_not_found"
    )
    h.run("dependency_rebind", task_id="T002", dep_id="T001", revalidate_scope="接口")
    assert h.query("SELECT bound_revision FROM dependencies")[0]["bound_revision"] == 2
    h.run(
        "task_update",
        task_id="T002",
        goal="新目标",
        acceptance=["新验收"],
        user_source=h.source("T002", 1, "change"),
    )
    row = h.task("T002")
    assert (row["revision"], row["goal"]) == (2, "新目标")
    assert "新目标" in (h.home.root / "todo" / "T002.md").read_text(encoding="utf-8")


def test_roles_are_enforced(h: Harness) -> None:
    h.register("T001")
    stranger = Caller("worker", "w-x", MACHINE)
    assert (
        reason_of(lambda: h.run("task_plan", stranger, task_id="T001", reason="r")) == "role_denied"
    )
    assert ORCH.role == "orchestrator"
