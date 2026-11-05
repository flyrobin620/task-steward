"""Identity, hooks, inbox, attention and the status page against a real data directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.host.identity import SESSION_ENV, resolve
from tsw.ledger import Ledger
from tsw.machine.base import Caller

mark = pytest.mark


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def guard(h: Harness, session: str, cwd: Path, **payload: Any) -> Guard:
    g = Guard(h.home, {SESSION_ENV: session}, {"cwd": str(cwd), **payload})
    g.machine = "m1"
    g.ledger = Ledger(h.home, clock=h.clock, machine="m1")
    return g


def worker_task(h: Harness, tmp_path: Path) -> tuple[str, Path]:
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)["events"][
        0
    ]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    return "w-1", workdir


@mark.use_case("UC-HOST-001")
def test_identity_by_session_and_directory(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    with h.ledger.view() as conn:
        worker = resolve(conn, h.home, {SESSION_ENV: session}, workdir)
        assert (worker.role, worker.task_id, worker.attempt, worker.cwd_consistent) == (
            "worker",
            "T001",
            "a1",
            True,
        )
        elsewhere = resolve(conn, h.home, {SESSION_ENV: session}, tmp_path)
        assert elsewhere.role == "worker" and not elsewhere.cwd_consistent
        assert resolve(conn, h.home, {SESSION_ENV: "orch-x"}, h.home.root).role == "orchestrator"
        assert resolve(conn, h.home, {SESSION_ENV: "orch-x"}, tmp_path).role == "none"
        assert resolve(conn, h.home, {}, h.home.root).role == "none"


@mark.use_case("UC-HOST-002")
def test_worker_writes_are_confined_to_its_roots(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    inside = guard(
        h, session, workdir, tool_name="Write", tool_input={"file_path": str(workdir / "a.py")}
    )
    assert inside.run("PreToolUse") is None
    draft = h.home.root / "work" / "T001" / "a1" / "result.md"
    assert (
        guard(h, session, workdir, tool_name="Edit", tool_input={"file_path": str(draft)}).run(
            "PreToolUse"
        )
        is None
    )
    outside = guard(
        h, session, workdir, tool_name="Write", tool_input={"file_path": str(tmp_path / "x.py")}
    )
    decision = outside.run("PreToolUse")
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    push = guard(h, session, workdir, tool_name="Bash", tool_input={"command": "git push"})
    assert push.run("PreToolUse") is not None  # no delivery authorisation configured


@mark.use_case("UC-HOST-002")
def test_orchestrator_cannot_edit_tsw_owned_files_or_start_unrecorded_sessions(h: Harness) -> None:
    ledger_file = h.home.root / "ledger" / "events" / "x.jsonl"
    edit = guard(
        h, "orch-1", h.home.root, tool_name="Write", tool_input={"file_path": str(ledger_file)}
    )
    assert edit.run("PreToolUse") is not None
    claude_md = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="Write",
        tool_input={"file_path": str(h.home.root / "notes.md")},
    )
    assert claude_md.run("PreToolUse") is None
    start = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="mcp__ccd_session__start_session",
        tool_input={"name": "t001-a1-g1"},
    )
    assert start.run("PreToolUse") is not None
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", workdir="w", user_turn=True)
    h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    assert start.run("PreToolUse") is None


@mark.use_case("UC-HOST-002")
def test_session_start_context_and_lease(h: Harness, tmp_path: Path) -> None:
    out = guard(h, "orch-1", h.home.root).run("SessionStart")
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "已持有调度租约" in text
    other = guard(h, "orch-2", h.home.root).run("SessionStart")
    assert other is not None and "只读运行" in other["hookSpecificOutput"]["additionalContext"]
    session, workdir = worker_task(h, tmp_path)
    worker = guard(h, session, workdir).run("SessionStart")
    assert worker is not None and "T001" in worker["hookSpecificOutput"]["additionalContext"]
    assert guard(h, "someone", tmp_path).run("SessionStart") is None


@mark.use_case("UC-HOST-002")
def test_stop_blocks_on_unhandled_reports_and_unfinished_reviews(
    h: Harness, tmp_path: Path
) -> None:
    session, workdir = worker_task(h, tmp_path)
    w = Caller("worker", session, "m1")
    h.run("question", w, task_id="T001", text="q")
    stop = guard(h, "orch-1", h.home.root).run("Stop")
    assert stop is not None and stop["decision"] == "block"
    assert guard(h, "orch-1", h.home.root, stop_hook_active=True).run("Stop") is None
    h.run(
        "review_start",
        w,
        task_id="T001",
        head="h1",
        reviewer={"session": "codex-app-server", "model": "m"},
    )
    worker_stop = guard(h, session, workdir).run("Stop")
    assert worker_stop is not None and worker_stop["decision"] == "block"
    types = [e.type for e in h.ledger.storage.read()]
    assert "worker.turn_ended" in types
    report = guard(h, session, workdir, prompt="请回传 [report] 结论").run("UserPromptSubmit")
    assert (
        report is not None and "progress_note" in report["hookSpecificOutput"]["additionalContext"]
    )


@mark.use_case("UC-HOST-003")
def test_inbox_ack_and_attention(h: Harness, tmp_path: Path) -> None:
    session, _ = worker_task(h, tmp_path)
    h.run("question", Caller("worker", session, "m1"), task_id="T001", text="q")
    with h.ledger.view() as conn:
        pending = inbox_mod.inbox(conn, h.ledger.storage, h.config)
        items = inbox_mod.attention(conn, h.ledger.storage, h.config)
    assert [p["type"] for p in pending] == ["worker.question"]
    assert items["judgments"][0]["task_id"] == "T001"
    assert items["inbox"] == [{"count": 1}]
    h.ledger.commit(
        inbox_mod.ack([pending[0]["sequence"]], actor={"role": "orchestrator"}, config=h.config)
    )
    with h.ledger.view() as conn:
        assert inbox_mod.inbox(conn, h.ledger.storage, h.config) == []
    with pytest.raises(TswError):
        h.ledger.commit(inbox_mod.ack([1], actor={"role": "orchestrator"}, config=h.config))
    h.ledger.rebuild()
    with h.ledger.view() as conn:
        assert inbox_mod.inbox(conn, h.ledger.storage, h.config) == []


@mark.use_case("UC-HOST-003")
def test_status_page_follows_every_commit(h: Harness) -> None:
    h.register("T001", title="导出 <CSV>")
    h.register("T002")
    h.plan("T001")
    page = (h.home.root / "views" / "STATUS.html").read_text(encoding="utf-8")
    assert "导出 &lt;CSV&gt;" in page
    assert "待启动" in page
    assert "orch-1" in page
    h.run("task_cancel", task_id="T002", user_source=h.source("T002", 1, "cancel"))
    page = (h.home.root / "views" / "STATUS.html").read_text(encoding="utf-8")
    assert "已取消" in page
