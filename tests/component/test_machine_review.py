"""The worker's self-review loop, the orchestrator's judgment, submission and acceptance."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tsw.errors import TswError
from tsw.home import Home
from tsw.machine.base import Caller

mark = pytest.mark
HIGH = {
    "id": "F1",
    "severity": "high",
    "category": "correctness",
    "claim": "c",
    "evidence": "e",
    "suggestion": "s",
}
LOW = {**HIGH, "id": "F2", "severity": "low"}


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as info:
        call()
    return str(info.value.details.get("reason"))


def started(home: Home, **config: Any) -> tuple[Harness, Caller]:
    h = Harness(home, config or None)
    h.register("T001")
    h.plan("T001")
    return h, h.dispatch("T001")


def judgments(h: Harness) -> list[dict[str, Any]]:
    return h.query("SELECT item_id, trigger, status FROM judgments ORDER BY item_id")


@mark.use_case("UC-REVIEW-001")
@mark.transition("E01")
@mark.transition("E03")
@mark.transition("E04")
@mark.transition("E05")
def test_iterate_then_finalize(home: Home) -> None:
    h, w = started(home)
    r1 = h.review_round(w, "T001", "h1", [HIGH, LOW])
    assert h.task("T001")["substate"] == "triaging"
    assert (
        reason_of(
            lambda: h.run(
                "review_triage",
                w,
                task_id="T001",
                round=r1,
                items=[{"finding": "F1", "action": "defer", "reason": "以后"}],
            )
        )
        == "invalid_action"
    )
    assert (
        reason_of(
            lambda: h.run(
                "review_triage",
                w,
                task_id="T001",
                round=r1,
                items=[{"finding": "F1", "action": "fix"}],
            )
        )
        == "triage_incomplete"
    )
    h.run(
        "review_triage",
        w,
        task_id="T001",
        round=r1,
        items=[
            {"finding": "F1", "action": "fix"},
            {"finding": "F2", "action": "defer", "reason": "超出范围"},
        ],
    )
    assert (
        reason_of(
            lambda: h.run(
                "review_decide", w, task_id="T001", round=r1, decision="finalize", head="h1"
            )
        )
        == "blocking_unresolved"
    )
    h.run("review_decide", w, task_id="T001", round=r1, decision="iterate", head="h1")
    assert h.task("T001")["substate"] == "revising"
    r2 = h.review_round(w, "T001", "h2")
    assert r2 == r1 + 1
    h.run("review_triage", w, task_id="T001", round=r2, items=[])
    # Round 2 reviewed the fix of F1 incrementally, so F1 counts as verified.
    h.run("review_decide", w, task_id="T001", round=r2, decision="finalize", head="h2")
    h.submit(w, "T001", head="h2")
    assert h.status("T001") == "verifying"


@mark.use_case("UC-REVIEW-001")
def test_fix_with_verification_finalizes_and_submits(home: Home) -> None:
    h, w = started(home)
    r1 = h.review_round(w, "T001", "h1", [HIGH])
    h.run(
        "review_triage",
        w,
        task_id="T001",
        round=r1,
        items=[{"finding": "F1", "action": "fix", "verification": "test_x 通过"}],
    )
    h.run("review_decide", w, task_id="T001", round=r1, decision="iterate", head="h1")
    r2 = h.review_round(w, "T001", "h2")
    h.run("review_triage", w, task_id="T001", round=r2, items=[])
    h.run("review_decide", w, task_id="T001", round=r2, decision="finalize", head="h2")
    h.submit(w, "T001", head="h2")
    submission = h.task("T001")["submission"]
    assert '"rounds": [1, 2]' in submission
    assert (h.home.root / "runs" / "T001" / "a1" / "result.html").is_file()


@mark.use_case("UC-REVIEW-001")
@mark.use_case("UC-JUDGE-001")
def test_budget_exhausted_is_judged_by_the_orchestrator(home: Home) -> None:
    h, w = started(home, review={"max_rounds": 1, "orchestrator_extension_limit": 1})
    r1 = h.review_round(w, "T001", "h1", [HIGH])
    h.run(
        "review_triage",
        w,
        task_id="T001",
        round=r1,
        items=[{"finding": "F1", "action": "fix", "verification": "v"}],
    )
    assert (
        reason_of(
            lambda: h.run(
                "review_decide", w, task_id="T001", round=r1, decision="iterate", head="h1"
            )
        )
        == "budget_exhausted"
    )
    h.run(
        "review_decide",
        w,
        task_id="T001",
        round=r1,
        decision="escalate",
        head="h1",
        escalate_kind="budget_exhausted",
        suggestion="再给一轮",
    )
    item = judgments(h)[0]
    assert item["trigger"] == "review.budget_exhausted"
    assert h.status("T001") == "in_progress"
    common = {
        "task_id": "T001",
        "items": [item["item_id"]],
        "rationale": "在收敛",
        "basis": ["round 1"],
        "confidence": "high",
    }
    assert (
        reason_of(lambda: h.run("judge", decision="extend", rounds=2, **common))
        == "extension_limit"
    )
    h.run("judge", decision="extend", rounds=1, **common)
    assert judgments(h)[0]["status"] == "resolved"
    h.run("review_decide", w, task_id="T001", round=r1, decision="iterate", head="h1")
    r2 = h.review_round(w, "T001", "h2")
    h.run("review_triage", w, task_id="T001", round=r2, items=[])
    h.run("review_decide", w, task_id="T001", round=r2, decision="finalize", head="h2")
    decisions = h.query("SELECT decision, trigger FROM decisions")
    assert decisions == [{"decision": "extend", "trigger": "review.budget_exhausted"}]


@mark.use_case("UC-JUDGE-001")
def test_defer_request_approved_by_the_orchestrator(home: Home) -> None:
    h, w = started(home)
    r1 = h.review_round(w, "T001", "h1", [HIGH])
    h.run("review_triage", w, task_id="T001", round=r1, items=[{"finding": "F1", "action": "fix"}])
    h.run(
        "review_decide",
        w,
        task_id="T001",
        round=r1,
        decision="escalate",
        head="h1",
        escalate_kind="defer_requested",
        suggestion="依赖外部团队",
    )
    item = judgments(h)[0]["item_id"]
    h.run(
        "judge",
        task_id="T001",
        items=[item],
        decision="defer",
        rationale="外部依赖",
        basis=["issue"],
        confidence="medium",
        findings=[{"round": r1, "finding": "F1"}],
    )
    h.run("review_decide", w, task_id="T001", round=r1, decision="finalize", head="h1")
    h.submit(w, "T001")
    assert h.status("T001") == "verifying"
    assert h.query("SELECT disposition FROM findings")[0]["disposition"] == "deferred_approved"


@mark.use_case("UC-JUDGE-001")
def test_overrule_and_answer(home: Home) -> None:
    h, w = started(home)
    h.run("question", w, task_id="T001", text="验收第 2 条指什么？")
    question = judgments(h)[0]["item_id"]
    common = {
        "task_id": "T001",
        "rationale": "任务书写明",
        "basis": ["todo/T001.md"],
        "confidence": "high",
    }
    assert (
        reason_of(lambda: h.run("judge", decision="answer", items=[question], **common))
        == "invalid_input"
    )
    h.run("judge", decision="answer", items=[question], answer="指单元测试", **common)
    r1 = h.review_round(w, "T001", "h1", [HIGH])
    h.run("review_triage", w, task_id="T001", round=r1, items=[{"finding": "F1", "action": "fix"}])
    h.run(
        "review_decide",
        w,
        task_id="T001",
        round=r1,
        decision="escalate",
        head="h1",
        escalate_kind="defer_requested",
        suggestion="意见不成立",
    )
    item = next(j for j in judgments(h) if j["status"] == "open")["item_id"]
    assert (
        reason_of(
            lambda: h.run(
                "judge",
                decision="overrule",
                items=[item],
                findings=[{"round": r1, "finding": "F9"}],
                **common,
            )
        )
        == "finding_not_found"
    )
    h.run(
        "judge",
        decision="overrule",
        items=[item],
        findings=[{"round": r1, "finding": "F1"}],
        **common,
    )
    h.run("review_decide", w, task_id="T001", round=r1, decision="finalize", head="h1")
    assert [d["decision"] for d in h.query("SELECT decision FROM decisions ORDER BY sequence")] == [
        "answer",
        "overrule",
    ]


@mark.use_case("UC-JUDGE-001")
@mark.transition("T11")
@mark.transition("T12")
def test_escalation_to_the_user_and_back(home: Home) -> None:
    h, w = started(home)
    r1 = h.review_round(w, "T001", "h1")
    h.run("question", w, task_id="T001", text="要不要改接口？")
    item = judgments(h)[0]["item_id"]
    result = h.run(
        "judge",
        task_id="T001",
        items=[item],
        decision="escalate",
        question="是否允许改公开接口？",
        rationale="影响其他任务",
        basis=["DN006 §8.2"],
        confidence="low",
    )
    request = result["events"][1]["data"]["request_id"]
    assert h.status("T001") == "waiting_user"
    assert (
        reason_of(lambda: h.run("review_triage", w, task_id="T001", round=r1, items=[]))
        == "invalid_state"
    )
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request,
        answer="允许",
        user_source=h.source("T001", 1, "answer", request_id=request),
    )
    row = h.task("T001")
    assert (row["status"], row["substate"]) == ("in_progress", "triaging")
    late = h.run(
        "input_resolve",
        task_id="T001",
        request_id=request,
        answer="改主意了",
        user_source=h.source("T001", 1, "answer", request_id=request),
    )
    assert late["events"][0]["type"] == "input.late_response"


@mark.use_case("UC-REVIEW-001")
@mark.transition("E02")
def test_review_failures_retry_then_become_unavailable(home: Home) -> None:
    h, w = started(home, review={"retry_limit": 1})
    started_round = h.run(
        "review_start",
        w,
        task_id="T001",
        head="h1",
        reviewer={"session": "codex-app-server", "model": "gpt-6-astra"},
    )
    r1 = started_round["events"][0]["data"]["round"]
    assert (
        h.run("review_fail", w, task_id="T001", round=r1, error="timeout")["events"][0]["type"]
        == "review.failed"
    )
    assert (
        h.run("review_fail", w, task_id="T001", round=r1, error="timeout")["events"][0]["type"]
        == "review.unavailable"
    )
    assert judgments(h)[0]["trigger"] == "review.unavailable"
    assert (
        reason_of(
            lambda: h.run(
                "review_start", w, task_id="T001", head="h1", reviewer={"session": "claude-app"}
            )
        )
        == "invalid_substate"
    )


@mark.use_case("UC-REVIEW-001")
@mark.transition("E06")
def test_head_changed_after_review_needs_another_round(home: Home) -> None:
    h, w = started(home)
    r1 = h.review_round(w, "T001", "h1")
    h.run("review_triage", w, task_id="T001", round=r1, items=[])
    assert (
        reason_of(
            lambda: h.run(
                "review_decide", w, task_id="T001", round=r1, decision="finalize", head="h1b"
            )
        )
        == "head_not_reviewed"
    )
    h.run("self_check_failed", w, task_id="T001", reason="修正了报告链接")
    assert h.task("T001")["substate"] == "executing"
    h.finalize(w, "T001", head="h1b")
    h.submit(w, "T001", head="h1b")
    assert h.status("T001") == "verifying"


@mark.use_case("UC-REVIEW-001")
@mark.transition("E07")
def test_requirement_change_restarts_the_cycle(home: Home) -> None:
    h, w = started(home)
    h.finalize(w, "T001")
    h.run(
        "task_update",
        task_id="T001",
        goal="新目标",
        acceptance=["新验收"],
        user_source=h.source("T001", 1, "change"),
    )
    row = h.task("T001")
    assert (row["revision"], row["cycle"], row["substate"]) == (2, 2, "executing")
    h.finalize(w, "T001", head="h3")
    h.submit(w, "T001", head="h3")
    assert h.task("T001")["status"] == "verifying"


@mark.use_case("UC-VERIFY-001")
@mark.transition("T13")
@mark.transition("G-SUBMIT")
def test_submit_guards(home: Home) -> None:
    h, w = started(home)
    other = Caller("worker", "w-other", MACHINE)
    h.finalize(w, "T001")
    assert reason_of(lambda: h.submit(other, "T001")) == "not_bound_worker"
    assert reason_of(lambda: h.submit(w, "T001", head="h9")) == "g_submit"
    assert (
        reason_of(
            lambda: h.run(
                "submit",
                w,
                task_id="T001",
                head="h1",
                report_md="work/T001/a1/none.md",
                report_html="work/T001/a1/none.html",
                worktree_clean=True,
            )
        )
        == "g_submit"
    )
    assert (
        reason_of(
            lambda: h.run(
                "submit",
                w,
                task_id="T001",
                head="h1",
                report_md="../x.md",
                report_html="x.html",
                worktree_clean=True,
            )
        )
        == "invalid_report_path"
    )
    assert (
        reason_of(
            lambda: h.run(
                "submit",
                w,
                task_id="T001",
                head="h1",
                report_md="work/T001/a1/r.md",
                report_html="work/T001/a1/r.html",
                worktree_clean=False,
            )
        )
        == "g_submit"
    )
    h.submit(w, "T001")
    assert h.status("T001") == "verifying"


@mark.use_case("UC-VERIFY-001")
@mark.transition("T17")
@mark.transition("T18")
@mark.transition("G-ACCEPT")
def test_verification_feedback_and_acceptance_guards(home: Home) -> None:
    h, w = started(home)
    h.finalize(w, "T001")
    h.submit(w, "T001")
    assert (
        reason_of(
            lambda: h.run(
                "task_accept", task_id="T001", verified_head="h1", summary="s", acceptance=[]
            )
        )
        == "g_accept"
    )
    h.run("feedback", task_id="T001", items=["补一个边界测试"])
    row = h.task("T001")
    assert (row["status"], row["cycle"]) == ("in_progress", 2)
    h.finalize(w, "T001", head="h2")
    h.submit(w, "T001", head="h2")
    full = [
        {"item": "功能可用", "status": "done", "evidence": "e"},
        {"item": "测试通过", "status": "done", "evidence": "e"},
    ]
    assert (
        reason_of(
            lambda: h.run(
                "task_accept", task_id="T001", verified_head="h1", summary="s", acceptance=full
            )
        )
        == "g_accept"
    )
    assert (
        reason_of(
            lambda: h.run(
                "task_accept", task_id="T001", verified_head="h2", summary="s", acceptance=full
            )
        )
        == "g_accept"
    )  # no output
    h.run("integrate", task_id="T001", repo="demo", status="started")
    h.run(
        "output_link",
        task_id="T001",
        output_id="pr-1",
        type="pr",
        ref="u",
        evidence=["e"],
        verified_at="t",
    )
    assert (
        reason_of(
            lambda: h.run(
                "task_accept", task_id="T001", verified_head="h2", summary="s", acceptance=full
            )
        )
        == "g_accept"
    )  # integration open
    h.run("integrate", task_id="T001", repo="demo", status="completed")
    h.run(
        "task_accept", task_id="T001", verified_head="h2", summary="技术验收通过", acceptance=full
    )
    assert h.status("T001") == "done"
    review = (h.home.root / "runs" / "T001" / "a1" / "review.md").read_text(encoding="utf-8")
    assert "技术验收通过" in review


@mark.use_case("UC-LIFE-001")
@mark.transition("T19")
@mark.transition("T27")
def test_blocked_integration_pauses_and_resumes_to_verifying(home: Home) -> None:
    h, w = started(home)
    h.finalize(w, "T001")
    h.submit(w, "T001")
    pause = {"owner": "CI", "next_action": "等 CI 恢复", "resume_condition": "CI 正常"}
    assert (
        reason_of(lambda: h.run("task_pause", task_id="T001", category="external", **pause))
        == "invalid_state"
    )
    h.run("task_pause", task_id="T001", category="integration_blocked", **pause)
    assert h.status("T001") == "paused"
    h.run("task_resume", task_id="T001", condition_met=True, note="CI 恢复")
    assert h.status("T001") == "verifying"


@mark.use_case("UC-LIFE-001")
def test_paused_verification_after_requirement_change_cannot_return_to_verifying(
    home: Home,
) -> None:
    h, w = started(home)
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
    h.run(
        "task_update",
        task_id="T001",
        goal="g2",
        acceptance=["a2"],
        user_source=h.source("T001", 1, "change"),
    )
    h.run("task_resume", task_id="T001", condition_met=True, note="n")
    row = h.task("T001")
    # The frozen submission no longer belongs to the revision: T20 back to work, not T27.
    assert (row["status"], row["cycle"], row["submission"]) == ("in_progress", 2, None)


def test_worker_tools_require_the_bound_session(home: Home) -> None:
    h, _ = started(home)
    stranger = Caller("worker", "w-x", MACHINE)
    assert (
        reason_of(lambda: h.run("progress_note", stranger, task_id="T001", text="x"))
        == "not_bound_worker"
    )
