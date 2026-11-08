"""Identity, hooks, inbox, attention and the status page against a real data directory."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import ORCH, Harness
from tsw.cli import main as cli_main
from tsw.commands import Command
from tsw.errors import TswError
from tsw.events import EventDraft
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.host.identity import SESSION_ENV, resolve
from tsw.host.mcp import Server, serve
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


@mark.use_case("UC-HOST-001")
def test_a_replaced_session_is_revoked_not_the_orchestrator(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    h.run("attempt_update", task_id="T001", attempt="a1", event="closed", evidence="closed")
    with h.ledger.view() as conn:
        for cwd in (workdir, h.home.root):
            who = resolve(conn, h.home, {SESSION_ENV: session}, cwd)
            assert (who.role, who.task_id, who.phase) == ("revoked", "T001", "closed")
    write = guard(
        h, session, workdir, tool_name="Write", tool_input={"file_path": str(workdir / "a.py")}
    )
    assert write.run("PreToolUse") is not None
    bash = guard(h, session, h.home.root, tool_name="Bash", tool_input={"command": "ls"})
    assert bash.run("PreToolUse") is not None
    read = guard(h, session, workdir, tool_name="Read", tool_input={"file_path": "a.py"})
    assert read.run("PreToolUse") is None
    assert guard(h, session, workdir).run("Stop") is None


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
@mark.parametrize(
    ("command", "allowed"),
    [
        ("echo x > notes.md", True),
        ("cd sub && echo x > notes.md", True),
        ("echo x > ../escape.md", False),
        ("cd .. && touch escape.md", False),
        ("cp a.py ../../b.py", False),
        ("tee -a {outside}/log.txt", False),
        (r'powershell -Command "Set-Content -Path ..\x.txt -Value 1"', False),
        ("bash -c 'rm -rf ../other'", False),
        ("echo x > {draft}", True),
        ("python -m tsw tool run task_dispatch --role orchestrator --input {{}}", False),
        ("tsw task list", False),
        ("tsw config validate", True),
        (
            "env -u CLAUDE_CODE_HOST_SESSION_ID python {launcher} tool run x --role system "
            "--input {{}}",
            False,
        ),
        ("python -c 'from tsw.cli import main'", False),
        ("python - <<'EOF'\nimport tsw.cli\nEOF", False),
        ("rg 'import tsw' tsw", True),
        ("git commit -m 'Fix import tsw behavior'", True),
        ("tsw host render", False),
        ("mv ../other/file.txt local.txt", False),
        ("cp -t ../other local.txt", False),
        ("git -C .. clone https://example.invalid/repo.git local", False),
        ("sed -i -e '/^#/d' notes.txt", True),
        ("powershell -Command \"Set-Content paths.txt 'C:/sdk/bin'\"", True),
        ("python -X utf8 - <<'PY'\nimport tsw.cli\nPY", False),
        ("python -X utf8 tools/check.py", True),
        ("cat <<\\EOF\nhello\nEOF\ngit push origin HEAD", False),
        ("echo hello # <<EOF\ngit push origin HEAD", False),
        ('cmd.exe /d /c "echo # Build step & git push origin HEAD"', False),
        ('powershell -Command "Copy-Item -Path local.txt ../outside.txt"', False),
        ("git status", True),
    ],
)
def test_worker_shell_writes_are_confined_too(
    h: Harness, tmp_path: Path, command: str, allowed: bool
) -> None:
    session, workdir = worker_task(h, tmp_path)
    draft = h.home.root / "work" / "T001" / "a1" / "r.md"
    # Unquoted backslashes are escapes in bash, so sessions write paths with forward slashes.
    text = command.format(
        outside=(tmp_path / "elsewhere").as_posix(),
        draft=draft.as_posix(),
        launcher="plugin/bin/tsw_launch.py",
    )
    decision = guard(h, session, workdir, tool_name="Bash", tool_input={"command": text}).run(
        "PreToolUse"
    )
    assert (decision is None) is allowed, decision


@mark.use_case("UC-HOST-002")
def test_a_worker_away_from_its_workdir_may_only_cd_back(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    for tool, tool_input in (
        ("Write", {"file_path": str(workdir / "a.py")}),
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": f'cd "{tmp_path}"'}),
    ):
        decision = guard(h, session, away, tool_name=tool, tool_input=tool_input).run("PreToolUse")
        assert (
            decision is not None
            and "cd" in decision["hookSpecificOutput"]["permissionDecisionReason"]
        )
    back = guard(h, session, away, tool_name="Bash", tool_input={"command": f'cd "{workdir}"'})
    assert back.run("PreToolUse") is None


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


def prompt_then_stop(h: Harness, prompt: str | None, session: str = "orch-1") -> Any:
    """One orchestrator turn: the prompt that starts it (``None``: a turn the hook did not see)
    and the ``Stop`` hook that ends it."""
    if prompt is not None:
        assert guard(h, session, h.home.root, prompt=prompt).run("UserPromptSubmit") is None
    return guard(h, session, h.home.root).run("Stop")


@mark.use_case("UC-HOST-002")
@mark.use_case("UC-HOST-003")
def test_a_user_turn_does_not_end_while_tasks_wait_for_a_session(h: Harness) -> None:
    h.register("T001")
    h.register("T002")
    h.plan("T001")
    h.plan("T002", deps=["T001"])
    with h.ledger.view() as conn:
        listed = inbox_mod.attention(conn, h.ledger.storage, h.config)["needs_prestart"]
    assert listed == [
        {"task_id": "T001", "status": "ready"},
        {"task_id": "T002", "status": "waiting_dependency", "waiting_on": ["T001"]},
    ]
    stop = prompt_then_stop(h, "执行 T001 和 T002")
    assert stop is not None and stop["decision"] == "block"
    assert "T001、T002" in stop["reason"] and "attempt_start" in stop["reason"]
    # Blocked once: the stop that follows in the same turn goes through.
    assert guard(h, "orch-1", h.home.root, stop_hook_active=True).run("Stop") is None
    # The app puts its own reminders in front of the user's words: still the user's turn.
    reminded = prompt_then_stop(h, "<system-reminder>linked sessions</system-reminder>\n继续")
    assert reminded is not None and reminded["decision"] == "block"
    # Turns that cannot create sessions are not reminded.
    for injected in (
        '<cross-session-message from="w">done</cross-session-message>',
        "<side-sessions-event>t001 ended</side-sessions-event>",
        "<scheduled-task>check</scheduled-task>",
        "<system-reminder>only a reminder</system-reminder>",
    ):
        assert prompt_then_stop(h, injected) is None
    assert prompt_then_stop(h, None) is None  # the user's mark was used up by the last stop
    # Another session reads while this one holds the lease: it could not prestart either.
    assert prompt_then_stop(h, "执行", session="orch-2") is None
    # Once both have a session, the user's turn ends normally.
    for task_id in ("T001", "T002"):
        started = h.run("attempt_start", task_id=task_id, purpose="prestart", user_turn=True)
        assert (h.home.root / "work" / task_id / "a1").is_dir()
        h.run(
            "attempt_reconcile",
            task_id=task_id,
            attempt="a1",
            generation=started["events"][0]["data"]["generation"],
            outcome="created",
            session_ref=f"w-{task_id}",
        )
    assert prompt_then_stop(h, "执行") is None


@mark.use_case("UC-HOST-003")
def test_needs_prestart_respects_the_standby_limit(home: Home) -> None:
    h = Harness(home, {"scheduling": {"prestart": {"enabled": True, "max_standby": 2}}})
    for task_id in ("T001", "T002", "T003"):
        h.register(task_id)
        h.plan(task_id)
    h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)

    def listed() -> list[str]:
        with h.ledger.view() as conn:
            items = inbox_mod.attention(conn, h.ledger.storage, h.config)["needs_prestart"]
        return [item["task_id"] for item in items]

    assert listed() == ["T002"]  # one place left; T001 is being created
    h.run("attempt_start", task_id="T002", purpose="prestart", user_turn=True)
    assert listed() == []
    assert prompt_then_stop(h, "执行") is None  # nothing can be prestarted now


@mark.use_case("UC-DISPATCH-001")
def test_attempt_start_creates_the_execution_directory_anywhere(h: Harness, tmp_path: Path) -> None:
    h.register("T001")
    h.plan("T001")
    h.register("T002")
    h.plan("T002")
    elsewhere = tmp_path / "worktrees" / "T001"
    h.run(
        "attempt_start", task_id="T001", purpose="prestart", user_turn=True, workdir=str(elsewhere)
    )
    assert elsewhere.is_dir()
    dispatched = tmp_path / "worktrees" / "T002"
    h.run("task_dispatch", task_id="T002", workdir=str(dispatched), user_turn=True)
    assert not dispatched.exists()
    h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    assert dispatched.is_dir()
    # A directory that cannot be created is refused before anything is recorded.
    h.register("T003")
    h.plan("T003")
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(TswError) as refused:
        h.run(
            "attempt_start",
            task_id="T003",
            purpose="prestart",
            user_turn=True,
            workdir=str(blocker / "T003"),
        )
    assert refused.value.details["reason"] == "workdir_unavailable"
    assert h.query("SELECT * FROM attempts WHERE task_id = 'T003'") == []


def change(h: Harness, task_id: str, revision: int, goal: str) -> dict[str, Any]:
    out = h.run(
        "task_update",
        task_id=task_id,
        goal=goal,
        acceptance=["验收"],
        user_source=h.source(task_id, revision, "change"),
    )
    return dict(out["events"][0]["data"])


@mark.use_case("UC-DISPATCH-001")
def test_a_prestarted_session_has_a_brief_that_follows_the_requirement(
    h: Harness, tmp_path: Path
) -> None:
    h.register("T001")
    h.register("T002", goal="第一版目标")
    h.plan("T001")
    h.plan("T002", deps=["T001"])
    started = h.run("attempt_start", task_id="T002", purpose="prestart", user_turn=True)
    data = started["events"][0]["data"]
    assert (data["brief_path"], data["brief_revision"], data["read_set"]) == (
        "runs/T002/a1/brief.md",
        1,
        ["T001", "T002"],
    )
    brief = h.home.root / "runs" / "T002" / "a1" / "brief.md"
    assert "第一版目标" in brief.read_text(encoding="utf-8")
    workdir = h.home.root / "work" / "T002" / "a1"
    h.run(
        "attempt_reconcile",
        task_id="T002",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-2",
    )
    server = Server(h.home, {SESSION_ENV: "w-2"}, workdir)
    who = server.call("whoami", {})
    assert (who["phase"], who["brief_path"], who["brief_revision"]) == (
        "standby",
        "runs/T002/a1/brief.md",
        1,
    )
    standby = guard(h, "w-2", workdir).run("SessionStart")
    assert standby is not None and "待命中" in standby["hookSpecificOutput"]["additionalContext"]
    # A later decision is written back at once: the standby session's brief follows.
    updated = change(h, "T002", 1, "第二版目标")
    assert updated["brief"] == {
        "attempt": "a1",
        "revision": 2,
        "path": "runs/T002/a1/brief.md",
        "session_ref": "w-2",
    }
    text = brief.read_text(encoding="utf-8")
    assert "第二版目标" in text and "revision：2" in text
    assert server.call("whoami", {})["brief_revision"] == 2
    # Dispatch renders it again from the latest revision and delivers it.
    upstream = h.dispatch("T001")
    h.finalize(upstream, "T001")
    h.submit(upstream, "T001")
    h.accept("T001")
    h.run("sync")
    assert h.status("T002") == "ready"
    h.run("task_dispatch", task_id="T002", workdir=str(workdir))
    h.run("attempt_deliver", task_id="T002")
    assert h.status("T002") == "in_progress"
    assert "第二版目标" in brief.read_text(encoding="utf-8")
    # At work: the brief follows as well and the worker is named for the orchestrator to tell.
    at_work = change(h, "T002", 2, "第三版目标")
    assert at_work["brief"]["session_ref"] == "w-2" and at_work["brief"]["revision"] == 3
    assert "第三版目标" in brief.read_text(encoding="utf-8")
    assert server.call("whoami", {})["brief_revision"] == 3


@mark.use_case("UC-DISPATCH-001")
def test_a_session_prestarted_before_briefs_gets_one_on_the_next_change(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    # attempt.start_requested as written before prestart wrote briefs: no brief fields.
    h.ledger.commit(
        Command(
            actor=ORCH.actor(),
            input={"op": "old_prestart"},
            build=lambda _ctx: [
                EventDraft(
                    type="attempt.start_requested",
                    task_id="T001",
                    revision=1,
                    attempt="a1",
                    data={
                        "attempt": "a1",
                        "generation": 1,
                        "session_name": "t001-a1-g1",
                        "purpose": "prestart",
                        "controller": "orch-1",
                        "adapter": "claude-app",
                        "workdir": "work/T001/a1",
                    },
                )
            ],
        )
    )
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=1,
        outcome="created",
        session_ref="w-1",
    )
    assert h.query("SELECT brief_path FROM attempts")[0]["brief_path"] is None
    updated = change(h, "T001", 1, "补写的目标")
    assert updated["brief"] == {
        "attempt": "a1",
        "revision": 2,
        "path": "runs/T001/a1/brief.md",
        "read_set": ["T001"],
        "session_ref": "w-1",
    }
    row = h.query("SELECT brief_path, brief_revision, read_set FROM attempts")[0]
    assert row == {
        "brief_path": "runs/T001/a1/brief.md",
        "brief_revision": 2,
        "read_set": '["T001"]',
    }
    brief = h.home.root / "runs" / "T001" / "a1" / "brief.md"
    assert "补写的目标" in brief.read_text(encoding="utf-8")


def ack(h: Harness, sequences: list[int], session: str = "orch-1") -> Any:
    return inbox_mod.ack(
        sequences,
        actor={"role": "orchestrator", "session_id": session},
        config=h.config,
        session_id=session,
        machine="m1",
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
    first = [pending[0]["sequence"]]
    with pytest.raises(TswError) as no_lease:
        h.ledger.commit(ack(h, first, session="orch-2"))
    assert no_lease.value.details["reason"] == "lease_held"
    h.ledger.commit(ack(h, first))
    with h.ledger.view() as conn:
        assert inbox_mod.inbox(conn, h.ledger.storage, h.config) == []
    with pytest.raises(TswError):
        h.ledger.commit(ack(h, [1]))
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


@mark.use_case("UC-HOST-001")
def test_the_server_announces_new_tools_once_a_session_is_bound(h: Harness, tmp_path: Path) -> None:
    workdir = tmp_path / "wt" / "T001"  # created when the task is dispatched below
    server = Server(h.home, {SESSION_ENV: "w-1"}, workdir)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in listed[0]["result"]["tools"]] == ["whoami"]
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == [
        {"jsonrpc": "2.0", "id": 2, "result": {}}
    ]
    worker_task(h, tmp_path)  # the orchestrator binds this session to T001/a1
    replies = server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert replies[1] == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    names = {
        t["name"]
        for t in server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})[0]["result"][
            "tools"
        ]
    }
    assert {"submit", "review_start"} <= names
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


@mark.use_case("UC-HOST-001")
def test_a_session_starting_in_an_execution_directory_is_listed_the_worker_tools(
    h: Harness, tmp_path: Path
) -> None:
    # Clients may keep their first tool list and ignore list_changed, and the worker session is
    # created before the orchestrator can bind it: list the worker tools already, grant nothing.
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    server = Server(h.home, {SESSION_ENV: "w-1"}, workdir / "sub")
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed[0]["result"]["tools"]}
    assert {"whoami", "task_read", "review_run", "submit", "review_triage"} <= names
    assert "task_list" not in names
    with pytest.raises(TswError) as denied:
        server.call("task_read", {"task_id": "T001"})
    assert denied.value.to_dict()["details"]["reason"] == "not_bound"
    elsewhere = Server(h.home, {SESSION_ENV: "x-1"}, tmp_path / "elsewhere")
    other = elsewhere.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in other[0]["result"]["tools"]] == ["whoami"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    assert server.call("task_read", {"task_id": "T001"})["task_id"] == "T001"


@mark.use_case("UC-HOST-001")
def test_an_idle_client_hears_about_its_new_tools(h: Harness, tmp_path: Path) -> None:
    workdir = tmp_path / "wt" / "T001"
    lines: list[dict[str, Any]] = []
    listed = threading.Event()
    done = threading.Event()

    class Out:
        def write(self, text: str) -> None:
            lines.append(json.loads(text))
            if lines[-1].get("id") == 1:
                listed.set()

        def flush(self) -> None:
            pass

    def incoming() -> Iterator[str]:
        yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        done.wait(30)  # the client sends nothing more until it is told the list changed

    thread = threading.Thread(
        target=serve,
        args=(h.home.root,),
        kwargs={
            "stdin": incoming(),
            "stdout": Out(),
            "env": {SESSION_ENV: "w-1"},
            "cwd": workdir,
            "opener": lambda _root: h.home,  # pyright: ignore[reportUnknownLambdaType]
            "poll_seconds": 0.05,
        },
        daemon=True,
    )
    thread.start()
    assert listed.wait(30)
    worker_task(h, tmp_path)  # bound while the client is idle
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(lines) < 2:
        time.sleep(0.05)
    done.set()
    thread.join(30)
    assert lines[1:] == [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]


@mark.use_case("UC-HOST-001")
def test_idle_polls_do_not_read_the_ledger_until_it_changes(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server(h.home, {SESSION_ENV: "w-1"}, tmp_path / "wt" / "T001")
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    reads: list[int] = []
    resolve_identity = Server.identity

    def counted(self: Server) -> Any:
        reads.append(1)
        return resolve_identity(self)

    monkeypatch.setattr(Server, "identity", counted)
    # tools/list already noted the ledger's state, so idle polls do not read it again.
    assert server.list_changes() == [] and server.list_changes() == []
    assert reads == []
    worker_task(h, tmp_path)
    assert server.list_changes() == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    ]
    assert len(reads) == 1


@mark.use_case("UC-HOST-001")
def test_a_listing_and_its_ledger_marker_are_one_snapshot(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server(h.home, {SESSION_ENV: "w-1"}, tmp_path / "wt" / "T001")
    resolve_identity = Server.identity
    bound: list[bool] = []

    def binding_midway(self: Server) -> Any:
        if not bound:  # the orchestrator binds the session while the list is being built
            bound.append(True)
            worker_task(h, tmp_path)
        return resolve_identity(self)

    monkeypatch.setattr(Server, "identity", binding_midway)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "submit" in {t["name"] for t in listed[0]["result"]["tools"]}
    assert server.list_changes() == []  # the newer ledger is re-read once, same identity
    assert server.list_changes() == []


@mark.use_case("UC-HOST-001")
def test_a_failed_listing_does_not_swallow_the_next_change(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server(h.home, {SESSION_ENV: "w-1"}, tmp_path / "wt" / "T001")
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    worker_task(h, tmp_path)
    resolve_identity = Server.identity

    def broken(self: Server) -> Any:
        raise OSError("ledger busy")

    monkeypatch.setattr(Server, "identity", broken)
    failed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert failed[0]["error"]["code"] == -32603
    monkeypatch.setattr(Server, "identity", resolve_identity)
    assert server.list_changes() == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    ]


@mark.use_case("UC-HOST-001")
def test_a_session_created_for_an_attempt_lists_worker_tools_before_binding(
    h: Harness, tmp_path: Path
) -> None:
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    # The app has created the session; the orchestrator has not bound it yet.
    server = Server(h.home, {SESSION_ENV: "w-1"}, workdir)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed[0]["result"]["tools"]}
    assert {"whoami", "review_run", "review_triage", "review_decide", "submit"} <= names
    assert "task_dispatch" not in names
    early = server.call("whoami", {})
    assert (early["role"], early["pending"], early["task_id"]) == ("none", True, "T001")
    with pytest.raises(TswError) as waiting:
        server.call("progress_note", {"text": "x"})
    assert waiting.value.details["reason"] == "not_bound"
    context = guard(h, "w-1", workdir).run("SessionStart")
    assert context is not None and "whoami" in context["hookSpecificOutput"]["additionalContext"]
    # Bound as a prestart, the session waits: the context says standby is not a go.
    assert "standby 时是预启动的待命会话" in context["hookSpecificOutput"]["additionalContext"]
    elsewhere = Server(h.home, {SESSION_ENV: "x-1"}, tmp_path)
    assert [t["name"] for t in elsewhere.tools(elsewhere.identity())] == ["whoami"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    assert server.call("whoami", {})["role"] == "worker"
    # Another session opened in the same directory after the binding is not a candidate.
    late = Server(h.home, {SESSION_ENV: "w-2"}, workdir)
    assert [t["name"] for t in late.tools(late.identity())] == ["whoami"]


@mark.use_case("UC-HOST-002")
def test_a_pending_session_can_only_read_until_it_is_bound(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    for tool, tool_input in (
        ("Write", {"file_path": str(workdir / "a.py")}),
        ("Bash", {"command": "git status"}),
        ("mcp__ccd_session__start_session", {"name": "x"}),
    ):
        decision = guard(h, "w-1", workdir, tool_name=tool, tool_input=tool_input).run("PreToolUse")
        assert (
            decision is not None
            and "绑定" in decision["hookSpecificOutput"]["permissionDecisionReason"]
        )
    read = guard(h, "w-1", workdir, tool_name="Read", tool_input={"file_path": "a.py"})
    assert read.run("PreToolUse") is None
    monkeypatch.setenv(SESSION_ENV, "w-1")
    monkeypatch.chdir(workdir)
    assert cli_main(["--home", str(h.home.root), "task", "show", "T001", "--json"]) == 3
    assert "host_session_cli" in capsys.readouterr().err


@mark.use_case("UC-HOST-001")
def test_a_prestarted_session_is_a_worker_from_its_first_listing(
    h: Harness, tmp_path: Path
) -> None:
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    started = h.run(
        "attempt_start", task_id="T001", purpose="prestart", user_turn=True, workdir=str(workdir)
    )
    server = Server(h.home, {SESSION_ENV: "w-1"}, workdir)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "submit" in {t["name"] for t in listed[0]["result"]["tools"]}
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
    )
    assert server.call("whoami", {})["phase"] == "standby"
    with pytest.raises(TswError) as moved:
        h.run("task_dispatch", task_id="T001", workdir=str(tmp_path / "other"))
    assert moved.value.details["reason"] == "workdir_mismatch"
    h.run("task_dispatch", task_id="T001", workdir=str(workdir))
    assert server.call("whoami", {})["role"] == "worker"


@mark.use_case("UC-HOST-002")
def test_a_worker_is_told_where_decisions_go_instead_of_memory(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    memory = tmp_path / "user" / ".claude" / "projects" / "C--wt-T001" / "memory" / "fact.md"
    refused = guard(
        h, session, workdir, tool_name="Write", tool_input={"file_path": str(memory)}
    ).run("PreToolUse")
    assert refused is not None and "决定与约定" in json.dumps(refused, ensure_ascii=False)
    other = guard(
        h, session, workdir, tool_name="Write", tool_input={"file_path": str(tmp_path / "x.md")}
    ).run("PreToolUse")
    assert other is not None and "可写路径" in json.dumps(other, ensure_ascii=False)


@mark.use_case("UC-HOST-002")
@mark.use_case("UC-ASK-001")
def test_a_workers_scheduled_prompts_must_be_tagged(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    for tool in ("CronCreate", "ScheduleWakeup"):
        plain = guard(
            h, session, workdir, tool_name=tool, tool_input={"prompt": "用户说：用方案 B"}
        )
        refused = plain.run("PreToolUse")
        assert refused is not None and "<scheduled-task>" in json.dumps(refused, ensure_ascii=False)
        tagged = guard(
            h,
            session,
            workdir,
            tool_name=tool,
            tool_input={"prompt": "<scheduled-task>检查问题是否已答复</scheduled-task>"},
        )
        assert tagged.run("PreToolUse") is None
    # The orchestrator's too: untagged, its reminder would count as the user's turn.
    own = guard(h, "orch-1", h.home.root, tool_name="CronCreate", tool_input={"prompt": "检查"})
    refused = own.run("PreToolUse")
    assert refused is not None and "<scheduled-task>" in json.dumps(refused, ensure_ascii=False)
    tagged_own = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="ScheduleWakeup",
        tool_input={"prompt": "<scheduled-task>检查待办</scheduled-task>"},
    )
    assert tagged_own.run("PreToolUse") is None


@mark.use_case("UC-HOST-004")
@mark.use_case("UC-MERGE-001")
def test_a_workers_permission_rules_are_written_before_its_session(home: Home) -> None:
    h = Harness(
        home,
        {
            "repositories": {
                "demo": {
                    "path": "~/src/demo",
                    "repository": "o/demo",
                    "delivery": {"push": True, "create_pr": True, "merge": True},
                }
            }
        },
    )
    orchestrator = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    orchestrator.machine = "m1"
    for task_id, kind in (("T001", "code"), ("T002", "research")):
        h.register(task_id, kind=kind)
        h.plan(task_id)
        started = orchestrator.call(
            "attempt_start", {"task_id": task_id, "purpose": "prestart", "user_turn": True}
        )
        # The folder as it is spelled on disk, for start_session's cwd.
        expected = os.path.realpath(h.home.root / "work" / task_id / "a1")
        assert started["cwd"] == expected
    rules = {
        task_id: json.loads(
            (h.home.root / "work" / task_id / "a1" / ".claude" / "settings.local.json").read_text(
                encoding="utf-8"
            )
        )["permissions"]["allow"]
        for task_id in ("T001", "T002")
    }
    # Only the development task in a repository that lets the worker merge may merge.
    assert "Bash(gh pr merge:*)" in rules["T001"]
    assert "Bash(gh pr merge:*)" not in rules["T002"]
    assert all("mcp__plugin_task-steward_tsw" in r for r in rules.values())
    assert not any("start_session" in rule for r in rules.values() for rule in r)
    # No session may edit the rules tsw writes: not the worker, not the orchestrator.
    workdir = h.home.root / "work" / "T001" / "a1"
    for target in (
        workdir / ".claude" / "settings.local.json",
        workdir / ".claude" / "Settings.json",
        h.home.root / ".claude" / "settings.json",
    ):
        edit = guard(
            h, "orch-1", h.home.root, tool_name="Write", tool_input={"file_path": str(target)}
        )
        refused = edit.run("PreToolUse")
        assert refused is not None and "settings" in json.dumps(refused, ensure_ascii=False)
    shell = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="Bash",
        tool_input={"command": f'echo {{}} > "{workdir / ".claude" / "settings.local.json"}"'},
    )
    assert shell.run("PreToolUse") is not None
    elsewhere = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="Write",
        tool_input={"file_path": str(workdir / "src" / ".claude" / "notes.md")},
    )
    assert elsewhere.run("PreToolUse") is None


@mark.use_case("UC-HOST-001")
def test_default_and_home_relative_execution_directories_are_recognised(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    h.register("T002")
    h.plan("T002")
    # A prestart without a directory creates the session in work/<task>/<attempt>.
    h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    default = h.home.root / "work" / "T001" / "a1"
    assert default.is_dir()  # attempt_start created it
    assert Server(h.home, {SESSION_ENV: "w-1"}, default).identity().pending
    # A dispatch may record its directory relative to the data directory.
    h.run("task_dispatch", task_id="T002", workdir="work/T002-wt", user_turn=True)
    h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    relative = h.home.root / "work" / "T002-wt"
    assert relative.is_dir()
    who = Server(h.home, {SESSION_ENV: "w-2"}, relative).identity()
    assert (who.pending, who.task_id) == (True, "T002")


@mark.use_case("UC-HOST-001")
def test_a_prestart_retry_without_a_directory_uses_the_default(h: Harness, tmp_path: Path) -> None:
    h.register("T001")
    h.plan("T001")
    first = h.run(
        "attempt_start",
        task_id="T001",
        purpose="prestart",
        user_turn=True,
        workdir=str(tmp_path / "custom"),
    )
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=first["events"][0]["data"]["generation"],
        outcome="not_created",
    )
    again = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    default = h.home.root / "work" / "T001" / "a1"
    assert default.is_dir()
    assert Server(h.home, {SESSION_ENV: "w-1"}, default).identity().pending
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=again["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
    )
    h.run("task_dispatch", task_id="T001", workdir=str(default))  # same place, absolute form
    assert Server(h.home, {SESSION_ENV: "w-1"}, default).identity().role == "worker"


@mark.use_case("UC-HOST-001")
def test_relative_and_absolute_forms_name_the_same_directory(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    started = h.run(
        "attempt_start", task_id="T001", purpose="prestart", user_turn=True, workdir="work/wt"
    )
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
    )
    h.run("task_dispatch", task_id="T001", workdir=str(h.home.root / "work" / "wt"))
