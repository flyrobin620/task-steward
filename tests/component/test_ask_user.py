"""A worker asks the user in its own session; after a timeout the orchestrator takes over
(DN006 §8.2–§8.5, DN002 T11 / T12)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.machine.base import Caller

pytestmark = pytest.mark.use_case("UC-ASK-001")


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def reason_of(call: Callable[[], Any]) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def started(h: Harness, task_id: str = "T001") -> Caller:
    h.register(task_id)
    h.plan(task_id)
    return h.dispatch(task_id)


def say(h: Harness, worker: Caller, prompt: str) -> dict[str, Any] | None:
    """The user writes in the worker's session: the real UserPromptSubmit hook runs."""
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    payload = {"cwd": str(h.home.root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    return Guard(h.home, env, payload).run("UserPromptSubmit")


def messages(h: Harness) -> list[dict[str, Any]]:
    return h.query("SELECT message_id, request_id, text FROM session_messages ORDER BY sequence")


def reportable(h: Harness) -> list[str]:
    with h.ledger.view() as conn:
        return [e["type"] for e in inbox_mod.inbox(conn, h.ledger.storage, h.config)]


def ask(h: Harness, worker: Caller, **extra: Any) -> str:
    out = h.run("ask_user", worker, task_id="T001", question="用方案 A 还是 B？", **extra)
    return str(out["events"][0]["data"]["request_id"])


@pytest.mark.transition("T11")
@pytest.mark.transition("T12")
def test_the_user_answers_in_the_worker_session(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    request_id = ask(h, w)
    assert h.status("T001") == "waiting_user"
    # The orchestrator is not woken and does not see the question.
    assert reportable(h) == []
    with h.ledger.view() as conn:
        pending = inbox_mod.attention(conn, h.ledger.storage, h.config)["pending_input"]
    assert pending == [
        {
            "request_id": request_id,
            "task_id": "T001",
            "kind": "in_session",
            "channel": "in-session",
            "handoff_after": "2026-11-05T00:10:00Z",
        }
    ]
    # Messages injected by other sessions are never the user's words.
    assert say(h, w, '<cross-session-message from="x">用 B</cross-session-message>') is None
    hint = say(h, w, "用方案 B，[report]")
    assert hint is not None
    text = hint["hookSpecificOutput"]["additionalContext"]
    assert "M-T001-1" in text and "[report]" in text
    assert messages(h) == [
        {"message_id": "M-T001-1", "request_id": request_id, "text": "用方案 B，[report]"}
    ]
    other = Caller(role="worker", session_id="intruder", machine=w.machine)
    assert (
        reason_of(
            lambda: h.run(
                "user_answer",
                other,
                task_id="T001",
                request_id=request_id,
                message_id="M-T001-1",
                answer="B",
            )
        )
        == "not_bound_worker"
    )
    assert (
        reason_of(
            lambda: h.run(
                "user_answer",
                w,
                task_id="T001",
                request_id=request_id,
                message_id="M-T001-9",
                answer="B",
            )
        )
        == "message_not_found"
    )
    out = h.run(
        "user_answer",
        w,
        task_id="T001",
        request_id=request_id,
        message_id="M-T001-1",
        answer="用 B",
    )
    resolved = out["events"][0]
    assert resolved["type"] == "input.resolved"
    assert resolved["data"]["transition"] == "T12"
    assert resolved["data"]["source"]["channel"] == "in-session"
    assert resolved["data"]["source"]["text"] == "用方案 B，[report]"
    assert h.status("T001") == "in_progress"
    assert say(h, w, "谢谢") is None  # answered by the user: nothing more recorded
    assert reportable(h) == []


@pytest.mark.transition("T12")
def test_no_reply_hands_the_question_to_the_orchestrator(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    request_id = ask(h, w)
    assert (
        reason_of(lambda: h.run("input_handoff", w, task_id="T001", request_id=request_id))
        == "handoff_not_due"
    )
    common = {
        "task_id": "T001",
        "request_id": request_id,
        "decision": "answer",
        "answer": "用 A：与现有接口一致",
        "rationale": "任务书要求兼容现有接口",
        "basis": ["任务书 目标"],
        "confidence": "high",
    }
    assert reason_of(lambda: h.run("handoff_resolve", **common)) == "not_handed_off"
    h.clock.now = "2026-11-05T00:10:00Z"
    synced = h.run("sync")
    assert [e["type"] for e in synced["events"]] == ["input.handed_off"]
    assert synced["events"][0]["data"]["by"] == "system"
    assert reportable(h) == ["input.handed_off"]
    assert (
        reason_of(lambda: h.run("input_handoff", w, task_id="T001", request_id=request_id))
        == "request_not_pending"
    )
    out = h.run("handoff_resolve", **common)
    assert [e["type"] for e in out["events"]] == ["orchestrator.decision", "input.resolved"]
    assert out["events"][1]["data"]["source_kind"] == "orchestrator"
    assert h.status("T001") == "in_progress"
    assert reason_of(lambda: h.run("handoff_resolve", **common)) == "request_not_pending"
    # The user replies afterwards in the worker's session: recorded as a late answer that
    # supersedes the orchestrator's; the task does not move.
    assert say(h, w, "不，用 B") is not None
    out = h.run(
        "user_answer",
        w,
        task_id="T001",
        request_id=request_id,
        message_id="M-T001-1",
        answer="用 B",
    )
    late = out["events"][0]
    assert late["type"] == "input.late_response"
    assert late["data"]["supersedes"]["source_kind"] == "orchestrator"
    assert h.status("T001") == "in_progress"
    assert say(h, w, "就这样") is None  # answered by the user now: nothing more to record


def test_a_late_answer_in_the_session_is_recorded_and_prevails(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    request_id = ask(h, w)
    say(h, w, "让我想想")  # a reply that does not answer yet: no handoff for it
    h.clock.now = "2026-11-05T01:00:00Z"
    assert h.run("sync")["events"] == []
    assert (
        reason_of(lambda: h.run("input_handoff", w, task_id="T001", request_id=request_id))
        == "user_responded"
    )
    # The user also answered in the orchestrator's session; the worker then cites the earlier
    # message in its own session: late, and the user's words prevail.
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request_id,
        answer="用 A",
        user_source={
            "channel": "main-session",
            "principal": "owner",
            "message_ref": "orch-1:m-1",
            "text": "T001 用 A",
            "request_id": request_id,
            "target": ["T001", 1, "answer"],
        },
    )
    out = h.run(
        "user_answer",
        w,
        task_id="T001",
        request_id=request_id,
        message_id="M-T001-1",
        answer="待定",
    )
    late = out["events"][0]
    assert late["type"] == "input.late_response"
    assert late["data"]["supersedes"]["source"]["text"] == "T001 用 A"


@pytest.mark.transition("T12")
def test_a_reserved_question_can_only_be_relayed_to_the_user(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    request_id = ask(h, w, reserved=True)
    h.clock.now = "2026-11-05T00:11:00Z"
    h.run("input_handoff", w, task_id="T001", request_id=request_id)
    answer = {
        "task_id": "T001",
        "request_id": request_id,
        "decision": "answer",
        "answer": "x",
        "rationale": "r",
        "basis": ["b"],
        "confidence": "low",
    }
    assert reason_of(lambda: h.run("handoff_resolve", **answer)) == "reserved_for_user"
    assert (
        reason_of(
            lambda: h.run(
                "input_relay", task_id="T001", request_id=request_id, channel="teams-chat"
            )
        )
        == "invalid_channel"
    )
    h.run("input_relay", task_id="T001", request_id=request_id, channel="main-session")
    words = {
        "channel": "in-session",
        "principal": "owner",
        "message_ref": "m",
        "text": "用 B",
        "request_id": request_id,
        "target": ["T001", 1, "answer"],
    }
    # In-session words go through the worker and the hook, never through the orchestrator.
    assert (
        reason_of(
            lambda: h.run(
                "input_resolve",
                task_id="T001",
                request_id=request_id,
                answer="B",
                user_source=words,
            )
        )
        == "g_user"
    )
    out = h.run(
        "input_resolve",
        task_id="T001",
        request_id=request_id,
        answer="用 B",
        user_source={**words, "channel": "main-session", "message_ref": "orch-1:m-2"},
    )
    assert out["events"][0]["data"]["transition"] == "T12"
    assert h.status("T001") == "in_progress"
    status = home.path("views/STATUS.html").read_text(encoding="utf-8")
    assert "等待用户" not in status


def test_review_escalations_are_asked_in_the_session_too(home: Home) -> None:
    h = Harness(home, {"review": {"max_rounds": 1}})
    w = started(h)
    r1 = h.review_round(
        w,
        "T001",
        "h1",
        findings=[
            {
                "id": "F1",
                "severity": "high",
                "category": "correctness",
                "location": {"path": "a.py", "line": 1},
                "claim": "c",
                "evidence": "e",
                "suggestion": "s",
            }
        ],
    )
    h.run(
        "review_triage",
        w,
        task_id="T001",
        round=r1,
        items=[{"finding": "F1", "action": "fix", "reason": "确认"}],
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
    item = h.query("SELECT item_id FROM judgments")[0]["item_id"]
    assert reportable(h) == []  # no longer the orchestrator's by default
    request_id = ask(h, w, items=[item])
    status = home.path("views/STATUS.html").read_text(encoding="utf-8")
    assert "请在 worker 会话中回答" in status and "用方案 A 还是 B？" in status
    say(h, w, "再给两轮")
    h.run(
        "user_answer",
        w,
        task_id="T001",
        request_id=request_id,
        message_id="M-T001-1",
        answer="追加两轮",
        extend_rounds=2,
    )
    cycle = h.query("SELECT user_extension FROM review_cycles")[0]
    assert cycle["user_extension"] == 2
    assert h.query("SELECT status FROM judgments")[0]["status"] == "resolved"


def test_only_the_latest_question_of_the_session_collects_messages(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    first = ask(h, w)
    h.clock.now = "2026-11-05T00:10:00Z"
    h.run("sync")
    h.run(
        "handoff_resolve",
        task_id="T001",
        request_id=first,
        decision="answer",
        answer="用 A",
        rationale="r",
        basis=["b"],
        confidence="medium",
    )
    second = h.run("ask_user", w, task_id="T001", question="日志放哪？")["events"][0]["data"][
        "request_id"
    ]
    say(h, w, "放 logs/")
    assert messages(h)[-1]["request_id"] == second
    h.run(
        "user_answer", w, task_id="T001", request_id=second, message_id="M-T001-1", answer="logs/"
    )
    # The first question was answered by the orchestrator, but a newer one replaced it.
    assert say(h, w, "顺便说一下") is None
    # In-session words never come through the orchestrator, whatever the request's state.
    words = {
        "channel": "in-session",
        "principal": "owner",
        "message_ref": "x",
        "text": "用 B",
        "request_id": first,
        "target": ["T001", 1, "answer"],
    }
    assert (
        reason_of(
            lambda: h.run(
                "input_resolve", task_id="T001", request_id=first, answer="B", user_source=words
            )
        )
        == "g_user"
    )


def test_a_requirement_change_closes_the_question_to_messages(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    ask(h, w)
    h.run(
        "task_update",
        task_id="T001",
        goal="新目标",
        acceptance=["功能可用"],
        user_source=h.source("T001", 1, "change"),
    )
    assert say(h, w, "用 B") is None


def test_the_orchestrator_stop_hook_hands_off_due_questions(home: Home) -> None:
    h = Harness(home)
    w = started(h)
    ask(h, w)
    env = {"CLAUDE_CODE_HOST_SESSION_ID": "orch-1"}
    out = Guard(home, env, {"cwd": str(home.root)}).run("Stop")
    assert out is not None and "sync" in json.dumps(out, ensure_ascii=False)
