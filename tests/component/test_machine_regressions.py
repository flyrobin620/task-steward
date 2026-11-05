"""Regressions for review round 1 of M3 (R1–R14) and the trial reduce before every append."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import ORCH, Harness
from tsw.commands import Command
from tsw.errors import TswError
from tsw.events import EventDraft
from tsw.home import Home
from tsw.machine.base import Caller
from tsw.storage.fs import FsStorage

mark = pytest.mark
HIGH = {"id": "F1", "severity": "high", "category": "correctness", "claim": "c", "evidence": "e"}


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as info:
        call()
    return str(info.value.details.get("reason"))


def started(home: Home, **config: Any) -> tuple[Harness, Caller]:
    h = Harness(home, config or None)
    h.register("T001")
    h.plan("T001")
    return h, h.dispatch("T001")


def exhaust(h: Harness, w: Caller) -> int:
    r = h.review_round(w, "T001", "h1", [HIGH])
    h.run("review_triage", w, task_id="T001", round=r, items=[{"finding": "F1", "action": "fix"}])
    return r


@mark.use_case("UC-LEDGER-001")
def test_an_event_its_reducer_refuses_is_never_appended(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    tail = FsStorage(home).tail()
    bad = Command(
        actor=ORCH.actor(),
        input={"op": "bad"},
        build=lambda ctx: [
            EventDraft(
                type="task.planned",
                task_id="T001",
                revision=1,
                data={
                    "transition": "T04",
                    "dependencies": [
                        {"task_id": "T001", "bound_revision": 1, "kind": "x", "reason": ""},
                        {"task_id": "T001", "bound_revision": 1, "kind": "x", "reason": ""},
                    ],
                    "resources": [],
                },
            )
        ],
    )
    assert reason_of(lambda: h.ledger.commit(bad)) == "reducer_rejected"
    assert FsStorage(home).tail() == tail
    assert h.ledger.audit()["ok"]
    h.plan("T001")  # the ledger keeps working


@mark.use_case("UC-REVIEW-001")
def test_r1_escalations_in_a_second_attempt_get_their_own_ids(home: Home) -> None:
    h, w = started(home, review={"max_rounds": 1})
    r = exhaust(h, w)
    h.run(
        "review_decide",
        w,
        task_id="T001",
        round=r,
        decision="escalate",
        head="h1",
        escalate_kind="budget_exhausted",
        suggestion="s",
    )
    h.run("attempt_update", task_id="T001", attempt="a1", event="lost", evidence="会话不存在")
    w2 = h.dispatch("T001")
    r2 = h.review_round(w2, "T001", "h1", [HIGH])
    h.run("review_triage", w2, task_id="T001", round=r2, items=[{"finding": "F1", "action": "fix"}])
    h.run(
        "review_decide",
        w2,
        task_id="T001",
        round=r2,
        decision="escalate",
        head="h1",
        escalate_kind="budget_exhausted",
        suggestion="s",
    )
    ids = [j["item_id"] for j in h.query("SELECT item_id FROM judgments")]
    assert len(ids) == len(set(ids)) == 2
    h.ledger.rebuild()


@mark.use_case("UC-PLAN-001")
def test_r2_duplicate_dependencies_are_refused(h: Harness) -> None:
    h.register("T001")
    h.register("T002")
    deps = [{"task_id": "T001", "reason": "a"}, {"task_id": "T001", "reason": "b"}]
    assert (
        reason_of(lambda: h.run("task_plan", task_id="T002", dependencies=deps, reason="r"))
        == "dependency_duplicate"
    )
    assert h.ledger.audit()["ok"]


@mark.use_case("UC-JUDGE-001")
@mark.transition("T12")
def test_r3_requirement_change_while_waiting_is_asked_again(home: Home) -> None:
    h, w = started(home)
    h.run("question", w, task_id="T001", text="q")
    h.run(
        "judge",
        task_id="T001",
        items=["J-T001-1"],
        decision="escalate",
        question="旧问题",
        rationale="r",
        basis=["b"],
        confidence="low",
    )
    h.run(
        "task_update",
        task_id="T001",
        goal="新目标",
        acceptance=["新验收"],
        user_source=h.source("T001", 1, "change"),
    )
    assert h.query("SELECT status FROM input_requests")[0]["status"] == "superseded"
    assert h.status("T001") == "waiting_user"
    asked = h.run(
        "judge",
        task_id="T001",
        items=[],
        decision="escalate",
        question="按新需求还要问吗？",
        rationale="revision 2",
        basis=["b"],
        confidence="low",
    )
    request = asked["events"][1]["data"]["request_id"]
    assert h.status("T001") == "waiting_user"
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request,
        answer="是",
        user_source=h.source("T001", 2, "answer", request_id=request),
    )
    row = h.task("T001")
    assert (row["status"], row["substate"], row["cycle"]) == ("in_progress", "executing", 2)


@mark.use_case("UC-LIFE-001")
@mark.transition("T20")
def test_r4_resume_claims_the_resources_again(home: Home) -> None:
    h = Harness(home)
    for task_id in ("T001", "T002"):
        h.register(task_id)
        h.plan(task_id, resources=["db"])
    w = h.dispatch("T001")
    h.run(
        "task_pause",
        w,
        task_id="T001",
        category="external",
        owner="o",
        next_action="n",
        resume_condition="r",
        worker_stopped=True,
    )
    h.run("resource_release", task_id="T001", resources=["db"], evidence="worker 已停")
    h.run("task_resume", task_id="T001", condition_met=True, note="n")
    assert (
        h.query("SELECT status FROM resource_claims WHERE owner_task = 'T001'")[0]["status"]
        == "held"
    )
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T002", workdir="w", user_turn=True))
        == "g_res"
    )


@mark.use_case("UC-LIFE-001")
@mark.transition("T25")
def test_r5_cancel_marks_the_whole_downstream_chain_stale(h: Harness) -> None:
    h.complete("T001")
    h.complete("T002", ["T001"])
    h.complete("T003", ["T002"])
    h.run(
        "task_cancel",
        task_id="T001",
        worker_stopped=True,
        user_source=h.source("T001", 1, "cancel"),
    )
    assert (h.task("T002")["stale"], h.task("T003")["stale"]) == (1, 1)
    assert (
        reason_of(
            lambda: h.run("user_accept", task_id="T003", user_source=h.source("T003", 1, "accept"))
        )
        == "stale"
    )


@mark.use_case("UC-REVIEW-001")
@mark.transition("G-SUBMIT")
def test_r6_a_blocked_review_does_not_count(home: Home) -> None:
    h, w = started(home)
    r = h.run(
        "review_start",
        w,
        task_id="T001",
        head="h1",
        reviewer={"session": "codex-app-server", "model": "gpt-6-astra"},
    )["events"][0]["data"]["round"]
    h.run(
        "review_complete",
        w,
        task_id="T001",
        round=r,
        result_path="r.json",
        result={
            "verdict": "blocked",
            "summary": "缺测试报告",
            "reviewed_head": "h1",
            "findings": [],
        },
    )
    h.run("review_triage", w, task_id="T001", round=r, items=[])
    assert (
        reason_of(
            lambda: h.run(
                "review_decide", w, task_id="T001", round=r, decision="finalize", head="h1"
            )
        )
        == "blocked_review"
    )
    h.run("self_check_failed", w, task_id="T001", reason="补齐测试报告")
    h.finalize(w, "T001")
    h.submit(w, "T001")
    assert h.status("T001") == "verifying"


@mark.use_case("UC-DISPATCH-001")
def test_r7_requirement_change_while_dispatching_refreshes_the_brief(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", workdir="w", user_turn=True)
    h.run(
        "task_update",
        task_id="T001",
        goal="新目标",
        acceptance=["新验收"],
        user_source=h.source("T001", 1, "change"),
    )
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "新目标" in brief
    assert "revision：2" in brief
    assert h.query("SELECT brief_revision FROM attempts")[0]["brief_revision"] == 2
    started_data = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)[
        "events"
    ][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started_data["generation"],
        outcome="created",
        session_ref="w",
        brief_delivered=True,
    )
    assert h.status("T001") == "in_progress"


@mark.use_case("UC-LIFE-001")
def test_r9_a_new_upstream_change_flags_a_kept_result_again(h: Harness) -> None:
    h.complete("T001")
    h.complete("T002", ["T001"])
    h.run("user_reject", task_id="T001", reason="r", user_source=h.source("T001", 1, "reject"))
    assert h.task("T002")["stale"] == 1
    h.run("stale_keep", task_id="T002", user_source=h.source("T002", 1, "keep"))
    assert h.task("T002")["stale"] == 0
    h.run("sync")
    assert h.task("T002")["stale"] == 0  # the acknowledged change is not flagged again
    h.run(
        "task_update",
        task_id="T001",
        goal="再改",
        acceptance=["x"],
        user_source=h.source("T001", 2, "change"),
    )
    assert h.task("T002")["stale"] == 1


@mark.use_case("UC-REVIEW-001")
def test_r10_budget_exhausted_after_a_self_check(home: Home) -> None:
    h, w = started(home, review={"max_rounds": 1})
    exhaust(h, w)
    h.run("self_check_failed", w, task_id="T001", reason="漏了文档")
    reviewer = {"session": "codex-app-server", "model": "gpt-6-astra"}
    assert (
        reason_of(lambda: h.run("review_start", w, task_id="T001", head="h2", reviewer=reviewer))
        == "budget_exhausted"
    )
    h.run("review_exhausted", w, task_id="T001", suggestion="再给一轮")
    item = h.query("SELECT item_id FROM judgments")[0]["item_id"]
    h.run(
        "judge",
        task_id="T001",
        items=[item],
        decision="extend",
        rounds=1,
        rationale="r",
        basis=["b"],
        confidence="high",
    )
    h.run("review_start", w, task_id="T001", head="h2", reviewer=reviewer)


@mark.use_case("UC-LIFE-001")
@mark.transition("T27")
def test_r11_valid_verification_waits_for_a_slot_instead_of_restarting(home: Home) -> None:
    h, w = started(home, scheduling={"max_workers": 1})
    h.finalize(w, "T001")
    h.submit(w, "T001")
    h.run(
        "task_pause",
        task_id="T001",
        category="integration_blocked",
        owner="o",
        next_action="n",
        resume_condition="r",
    )
    h.register("T002")
    h.plan("T002")
    h.dispatch("T002")
    assert (
        reason_of(lambda: h.run("task_resume", task_id="T001", condition_met=True, note="n"))
        == "g_slot"
    )
    row = h.task("T001")
    assert row["status"] == "paused"
    assert row["submission"] is not None


@mark.use_case("UC-PLAN-001")
def test_r12_expected_version_refuses_stale_decisions(h: Harness) -> None:
    h.register("T001")
    version = int(h.task("T001")["version"])
    h.run("intake_question", task_id="T001", question="q")
    before = h.task("T001")
    assert (
        reason_of(
            lambda: h.run(
                "task_update",
                task_id="T001",
                expected_version=version,
                goal="g2",
                acceptance=["a"],
                user_source=h.source("T001", 1, "change"),
            )
        )
        == "version_conflict"
    )
    assert h.task("T001")["revision"] == before["revision"]
    assert "g2" not in (h.home.root / "todo" / "T001.md").read_text(encoding="utf-8")


@mark.use_case("UC-LIFE-001")
def test_r13_a_worker_cannot_pause_verification(home: Home) -> None:
    h, w = started(home)
    h.finalize(w, "T001")
    h.submit(w, "T001")
    assert (
        reason_of(
            lambda: h.run(
                "task_pause",
                w,
                task_id="T001",
                category="integration_blocked",
                owner="o",
                next_action="n",
                resume_condition="r",
            )
        )
        == "role_denied"
    )


@mark.use_case("UC-LIFE-001")
@mark.transition("T25")
def test_r14_cancel_settles_creations_first_and_late_sessions_are_not_adopted(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    started_data = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)[
        "events"
    ][0]["data"]
    cancel = {
        "task_id": "T001",
        "worker_stopped": True,
        "user_source": h.source("T001", 1, "cancel"),
    }
    assert reason_of(lambda: h.run("task_cancel", **cancel)) == "attempt_unsettled"
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started_data["generation"],
        outcome="created",
        session_ref="w-standby",
    )
    assert reason_of(lambda: h.run("task_cancel", **cancel)) == "attempt_unsettled"
    h.run(
        "attempt_update",
        task_id="T001",
        attempt="a1",
        event="closed",
        evidence="archive_session 回执",
    )
    h.run("task_cancel", **cancel)
    assert h.status("T001") == "cancelled"
    assert (
        reason_of(
            lambda: h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
        )
        == "invalid_state"
    )
