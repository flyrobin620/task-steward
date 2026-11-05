"""Identity, hooks, inbox, attention and the status page against a real data directory."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import TswError
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
