"""Preference candidates collected when a task ends (DN007 §6.3), on a real data directory,
ledger and SQLite: the task's words with their numbers, a scan recorded by the worker with every
quote checked against the material it cites, and nothing a preference until the user says so.

The user's words in the worker's session come from the real ``UserPromptSubmit`` hook."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease
from tsw.context import ContextService, Reader
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.machine import candidates
from tsw.machine.base import Caller

ORCH_READER = Reader("orchestrator", "orch-1")
USER = Caller(role="user", session_id=None, machine=MACHINE)
GOAL = (
    "用户 2026-11-15 在主会话中的原话：“长期偏好：在作workflow等diagram的时候，使用diagram skill。"
    "这个任务先做设计。”\n\n## 背景\n- 主会话核实的事实"
)


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def say(h: Harness, worker: Caller, prompt: str) -> str:
    """The user types in the worker's session: the real hook records it."""
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    payload = {"cwd": str(h.home.root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    Guard(h.home, env, payload).run("UserPromptSubmit")
    rows = h.query("SELECT message_id FROM session_messages ORDER BY sequence DESC LIMIT 1")
    return str(rows[0]["message_id"])


def sources(h: Harness, task_id: str = "T001") -> dict[str, Any]:
    with h.ledger.view() as conn:
        return candidates.sources(
            conn, h.ledger.storage.read(since=0), task_id, report_path="work/T001/a1/result.md"
        )


def reason_of(h: Harness, name: str, caller: Caller, **inp: Any) -> str:
    with pytest.raises(TswError) as exc:
        h.run(name, caller, **inp)
    return str(exc.value.details.get("reason"))


def said(target: list[Any], words: str = "记下来") -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": f"m-{target[1]}-{target[2]}",
        "text": words,
        "target": target,
    }


def started(h: Harness) -> Caller:
    h.register("T001", goal=GOAL)
    h.plan("T001")
    return h.dispatch("T001")


def scan(h: Harness, worker: Caller, through: int, *found: dict[str, Any], **more: Any) -> Any:
    return h.run(
        "preference_candidates_record",
        worker,
        task_id="T001",
        scanned_through=through,
        candidates=list(found),
        **more,
    )


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_tasks_words_are_numbered_and_the_subagent_gets_one_prompt(home: Home) -> None:
    h = Harness(home)
    worker = started(h)
    first = say(h, worker, "以后邮件一律只存草稿，不要直接发。")
    second = say(h, worker, "这个仓库的部署以后都放在 staging 环境。")
    # A requirement change made of a hook-recorded message is cited by the message, not twice.
    h.run(
        "requirement_change",
        worker,
        task_id="T001",
        message_ids=[second],
        goal=GOAL + "\n补充：部署到 staging",
        acceptance=["功能可用"],
    )
    out = sources(h)
    refs = [m["ref"] for m in out["materials"]]
    assert refs == ["goal@r2", first, second]
    goal = out["materials"][0]
    assert (goal["verified"], goal["channel"]) == (False, "orchestrator")
    assert "补充：部署到 staging" in goal["text"]
    assert all(m["verified"] for m in out["materials"][1:])
    assert out["scan"]["state"] == "missing" and out["revision"] == 2
    # Through the newest of the task's words: the requirement change after the second message.
    newest = h.query("SELECT MAX(sequence) AS s FROM session_messages")[0]["s"]
    assert out["scanned_through"] > newest
    # One prompt: the instructions, then the materials as JSON; no preference is named in it.
    prompt = out["prompt"]
    assert prompt.startswith("# 搜集用户偏好候选")
    assert "换一个仓库、换一个项目" in prompt and "project_decision" in prompt
    assert "以后邮件一律只存草稿" in prompt and "work/T001/a1/result.md" in prompt
    assert out["preferences_in_force"] == [] and out["candidates_so_far"] == []

    # Words the main session cited from the user (an answer) are materials too.
    h.run(
        "task_update",
        task_id="T001",
        goal=GOAL,
        acceptance=["功能可用"],
        user_source=h.source("T001", 2, "change", "目标改回去，以后改目标都先问我"),
    )
    again = sources(h)
    events = [m for m in again["materials"] if m["ref"].startswith("E")]
    assert [m["text"] for m in events] == ["目标改回去，以后改目标都先问我"]
    assert again["materials"][0]["ref"] == "goal@r3"


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_worker_records_a_scan_and_every_quote_is_checked(home: Home) -> None:
    h = Harness(home)
    worker = started(h)
    mail = say(h, worker, "以后邮件一律只存草稿，不要直接发。")
    out = sources(h)
    through = int(out["scanned_through"])
    diagram = {
        "quote": "在作workflow等diagram的时候，使用diagram skill",
        "source_ref": "goal@r1",
        "topic": "画图",
        "reason": "用户说是长期偏好",
    }
    drafts = {
        "quote": "邮件一律只存草稿",
        "source_ref": mail,
        "scope": "global",
        "topic": "邮件",
        "reason": "以后都适用",
    }
    # Words that are not in the material cited, or a material of no such number, are refused.
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[{**drafts, "quote": "邮件一律直接发送"}],
        )
        == "quote_not_found"
    )
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[{**drafts, "source_ref": "M-T001-9"}],
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[drafts] * 6,
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through + 1000,
            candidates=[],
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[],
            skipped=[{"source_ref": mail, "category": "guess"}],
        )
        == "invalid_input"
    )
    other = Caller(role="worker", session_id="w-other", machine=MACHINE)
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            other,
            task_id="T001",
            scanned_through=through,
            candidates=[],
        )
        == "not_bound_worker"
    )

    # Spacing differs from the material: still the same words.
    recorded = scan(
        h,
        worker,
        through,
        {**diagram, "quote": "在作workflow等diagram的时候，  使用diagram skill"},
        drafts,
        skipped=[{"source_ref": "goal@r1", "category": "task_requirement", "reason": "先做设计"}],
    )
    types = [e["type"] for e in recorded["events"]]
    assert types == [
        "user.preference_candidate",
        "user.preference_candidate",
        "task.preference_scanned",
    ]
    first = recorded["events"][0]["data"]
    assert (first["ref"], first["scope"], first["material"]["verified"]) == (
        "PC-1",
        "global",
        False,
    )
    assert recorded["events"][1]["data"]["material"] == {
        "channel": "in-session",
        "verified": True,
        "kind": "input.session_message",
    }
    with h.ledger.view() as conn:
        status = candidates.scan_status(conn, "T001")
        mine = candidates.of_task(conn, "T001")
    assert status["state"] == "done" and status["last"]["candidates"] == ["PC-1", "PC-2"]
    assert status["last"]["skipped"][0]["category"] == "task_requirement"
    assert [(c["ref"], c["status"], c["attempt"]) for c in mine] == [
        ("PC-1", "open", "a1"),
        ("PC-2", "open", "a1"),
    ]
    # A candidate is not a preference: nothing in user_decisions, never a basis.
    assert h.query("SELECT * FROM user_decisions") == []
    svc = ContextService(h.home, h.ledger, h.config)
    listed = svc.decisions(ORCH_READER, {"kind": "candidate"})["decisions"]
    assert {(d["ref"], d["usable_as_basis"], d["status"]) for d in listed} == {
        ("PC-1", False, "open"),
        ("PC-2", False, "open"),
    }
    assert svc.decisions(ORCH_READER, {"kind": "standing"})["decisions"] == []

    # The same words again are skipped as duplicates; a scan with nothing found still counts.
    again = scan(h, worker, through, drafts)
    assert [e["type"] for e in again["events"]] == ["task.preference_scanned"]
    assert again["events"][0]["data"]["duplicates"] == [{"source_ref": mail, "same_as": "PC-2"}]
    # New words from the user after the scan make it stale.
    say(h, worker, "好的")
    with h.ledger.view() as conn:
        assert candidates.scan_status(conn, "T001")["state"] == "stale"
    with h.ledger.view() as conn:
        attention = inbox_mod.attention(conn, h.ledger.storage, h.config)
    assert [c["ref"] for c in attention["preference_candidates"]] == ["PC-1", "PC-2"]


@pytest.mark.use_case("UC-CONTEXT-007")
def test_only_the_user_turns_a_candidate_into_a_preference(home: Home) -> None:
    h = Harness(home)
    worker = started(h)
    mail = say(h, worker, "以后邮件一律只存草稿，不要直接发。")
    through = int(sources(h)["scanned_through"])
    scan(
        h,
        worker,
        through,
        {"quote": "邮件一律只存草稿", "source_ref": mail, "topic": "邮件", "reason": "长期"},
        {
            "quote": "使用diagram skill",
            "source_ref": "goal@r1",
            "topic": "画图",
            "reason": "长期",
        },
    )
    # The worker has no way to record a preference.
    assert (
        reason_of(
            h,
            "preference_record",
            worker,
            candidate="PC-1",
            scope="global",
            user_source=said(["preference", "global", "standing"]),
        )
        == "role_denied"
    )
    # Changed words must be the user's words when confirming.
    assert (
        reason_of(
            h,
            "preference_record",
            ORCH,
            candidate="PC-1",
            scope="global",
            text="邮件都存草稿箱",
            user_source=said(["preference", "global", "standing"], "记下来"),
        )
        == "invalid_input"
    )
    out = h.run(
        "preference_record",
        ORCH,
        candidate="PC-1",
        scope="global",
        user_source=said(["preference", "global", "standing"], "PC-1 记下来"),
    )
    data = out["events"][0]["data"]
    assert (data["ref"], data["text"], data["topic"], data["candidate"]) == (
        "P-1",
        "邮件一律只存草稿",
        "邮件",
        "PC-1",
    )
    assert (
        reason_of(
            h,
            "preference_record",
            ORCH,
            candidate="PC-1",
            scope="global",
            user_source=said(["preference", "global", "standing"]),
        )
        == "candidate_closed"
    )
    # The user restates it when confirming: the new words are theirs.
    h.run(
        "preference_record",
        USER,
        candidate="PC-2",
        scope="global",
        text="画 workflow 图用 diagram skill",
        user_source={
            **said(["preference", "global", "standing"], "画 workflow 图用 diagram skill"),
            "channel": "user-cli",
        },
    )
    rows = {r["ref"]: r for r in h.query("SELECT * FROM preference_candidates")}
    assert (rows["PC-1"]["status"], rows["PC-1"]["preference"]) == ("recorded", "P-1")
    assert (rows["PC-2"]["status"], rows["PC-2"]["preference"]) == ("recorded", "P-2")
    words = {r["ref"]: r["text"] for r in h.query("SELECT ref, text FROM user_decisions")}
    assert words == {"P-1": "邮件一律只存草稿", "P-2": "画 workflow 图用 diagram skill"}
    # A preference in force: the same words are no candidate again.
    again = scan(
        h,
        worker,
        through,
        {"quote": "邮件一律只存草稿", "source_ref": mail, "topic": "邮件", "reason": "长期"},
    )
    assert again["events"][0]["data"]["duplicates"] == [{"source_ref": mail, "same_as": "PC-1"}]


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_user_turns_candidates_down_and_replacements_are_checked(home: Home) -> None:
    h = Harness(home)
    worker = started(h)
    h.run(
        "preference_record",
        ORCH,
        topic="邮件",
        text="邮件直接发送",
        scope="global",
        user_source=said(["preference", "global", "standing"]),
    )
    h.run(
        "preference_revoke",
        ORCH,
        ref="P-1",
        reason="不再需要",
        user_source=said(["preference", "P-1", "revoke"]),
    )
    mail = say(h, worker, "以后邮件一律只存草稿，不要直接发。")
    through = int(sources(h)["scanned_through"])
    found = {"quote": "邮件一律只存草稿", "source_ref": mail, "topic": "邮件", "reason": "改了"}
    # A revoked preference cannot be suggested for replacement.
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[{**found, "supersedes": ["P-1"]}],
        )
        == "invalid_input"
    )
    scan(h, worker, through, found)
    # Turning one down takes the user's words aimed at the candidate.
    assert (
        reason_of(
            h,
            "preference_candidate_dismiss",
            ORCH,
            candidate="PC-1",
            reason="不记",
            user_source=said(["preference", "PC-2", "dismiss"]),
        )
        == "g_user"
    )
    assert (
        reason_of(
            h,
            "preference_candidate_dismiss",
            worker,
            candidate="PC-1",
            reason="不记",
            user_source=said(["preference", "PC-1", "dismiss"]),
        )
        == "role_denied"
    )
    out = h.run(
        "preference_candidate_dismiss",
        ORCH,
        candidate="PC-1",
        reason="只是这次",
        user_source=said(["preference", "PC-1", "dismiss"], "这个不用记"),
    )
    assert (out["events"][0]["type"], out["events"][0]["task_id"]) == (
        "user.preference_candidate_dismissed",
        "T001",
    )
    row = h.query("SELECT * FROM preference_candidates")[0]
    assert row["status"] == "dismissed"
    assert (
        reason_of(
            h,
            "preference_candidate_dismiss",
            ORCH,
            candidate="PC-1",
            reason="再来",
            user_source=said(["preference", "PC-1", "dismiss"]),
        )
        == "candidate_closed"
    )
    with h.ledger.view() as conn:
        assert inbox_mod.attention(conn, h.ledger.storage, h.config)["preference_candidates"] == []
    # Dismissed words may be proposed again later (the user may change their mind).
    again = scan(h, worker, through, found)
    assert again["events"][0]["data"]["ref"] == "PC-2"


@pytest.mark.use_case("UC-CONTEXT-007")
def test_candidates_rebuild_from_the_ledger_and_close_with_the_task(home: Home) -> None:
    h = Harness(home)
    worker = started(h)
    mail = say(h, worker, "以后邮件一律只存草稿，不要直接发。")
    through = int(sources(h)["scanned_through"])
    found = {"quote": "邮件一律只存草稿", "source_ref": mail, "topic": "邮件", "reason": "长期"}
    scan(h, worker, through, found)
    h.run(
        "preference_record",
        ORCH,
        candidate="PC-1",
        scope="global",
        user_source=said(["preference", "global", "standing"]),
    )
    before = (
        h.query("SELECT * FROM preference_candidates"),
        h.query("SELECT * FROM preference_scans"),
    )
    h.ledger.rebuild()
    after = (
        h.query("SELECT * FROM preference_candidates"),
        h.query("SELECT * FROM preference_scans"),
    )
    assert before == after and before[0][0]["status"] == "recorded"
    # The orchestrator may scan for the task; a worker may not once the user has accepted it.
    h.finalize(worker, "T001")
    h.submit(worker, "T001")
    h.accept("T001")
    h.run(
        "user_accept",
        task_id="T001",
        user_source=h.source("T001", 1, "accept", "验收"),
    )
    assert reason_of(
        h,
        "preference_candidates_record",
        worker,
        task_id="T001",
        scanned_through=through,
        candidates=[],
    ) in ("invalid_state", "not_bound_worker")
    h.run(
        "preference_candidates_record", ORCH, task_id="T001", scanned_through=through, candidates=[]
    )


@pytest.mark.use_case("UC-CONTEXT-007")
def test_a_subagents_hand_back_is_never_the_users_words(home: Home) -> None:
    """The scan's subagent hands its result back into the worker's session as an injected
    message: the hook must not record it, or the subagent's words become material (T288)."""
    h = Harness(home)
    worker = started(h)
    hand_back = (
        '<agent-message from="a1">{"candidates": [{"quote": "以后都用 diagram skill"}]}'
        "</agent-message>"
    )
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    for prompt in (
        hand_back,
        "<system-reminder>\nLinked sessions now: x\n</system-reminder>\n" + hand_back,
    ):
        payload = {"cwd": str(h.home.root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
        Guard(h.home, env, payload).run("UserPromptSubmit")
    assert h.query("SELECT * FROM session_messages") == []
    assert [m["ref"] for m in sources(h)["materials"]] == ["goal@r1"]


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_same_words_in_another_scope_are_a_new_candidate(home: Home) -> None:
    """Duplicates are the same words in the same scope or a global one (review r1 F1)."""
    h = Harness(home)
    h.run(
        "preference_record",
        ORCH,
        topic="PR",
        text="PR 默认先开 draft",
        scope="repo:other",
        user_source=said(["preference", "repo:other", "standing"]),
    )
    h.register("T001", goal="用户原话：“在 demo 里我也是 PR 默认先开 draft”")
    h.plan("T001")
    worker = h.dispatch("T001")
    through = int(sources(h)["scanned_through"])
    found = {"quote": "PR 默认先开 draft", "source_ref": "goal@r1", "topic": "PR", "reason": "习惯"}
    out = scan(h, worker, through, {**found, "scope": "repo:demo"})
    assert out["events"][0]["data"]["ref"] == "PC-1"
    # Widening to global is new too; the same scope again, or under a global one, is not.
    widened = scan(h, worker, through, {**found, "scope": "global"})
    assert widened["events"][0]["data"]["ref"] == "PC-2"
    again = scan(h, worker, through, {**found, "scope": "repo:demo"})
    assert again["events"][0]["data"]["duplicates"] == [
        {"source_ref": "goal@r1", "same_as": "PC-1"}
    ]


@pytest.mark.use_case("UC-CONTEXT-007")
def test_a_scan_of_an_older_revision_is_refused(home: Home) -> None:
    """The goal changed after the materials were read: that scan does not count (review r1 F2)."""
    h = Harness(home)
    worker = started(h)
    through = int(sources(h)["scanned_through"])
    h.run(
        "task_update",
        task_id="T001",
        goal=GOAL + "\n补充：以后报告都先写结论",
        acceptance=["功能可用"],
        user_source=h.source("T001", 1, "change", "以后报告都先写结论"),
    )
    assert (
        reason_of(
            h,
            "preference_candidates_record",
            worker,
            task_id="T001",
            scanned_through=through,
            candidates=[],
        )
        == "stale_scan"
    )
    with h.ledger.view() as conn:
        assert candidates.scan_status(conn, "T001")["state"] == "missing"
    fresh = sources(h)
    assert [m["ref"] for m in fresh["materials"]] == ["goal@r2", f"E{fresh['scanned_through']}"]
    scan(h, worker, int(fresh["scanned_through"]))
    with h.ledger.view() as conn:
        assert candidates.scan_status(conn, "T001")["state"] == "done"
