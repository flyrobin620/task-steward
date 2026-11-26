"""The spare-session pool (DN002 §5.9, T363) through the real tools, projection, hooks, MCP
server and Dashboard app.

The host's session registry and inboxes are fakes in the test's own ``CLAUDE_CONFIG_DIR``
(tests/fixtures/fake_inbox.py); git is real, in temporary repositories.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.component.test_workspace import checkout, git
from tests.fixtures.fake_inbox import FakeInbox
from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host import session_inbox
from tsw.host.guard import Guard
from tsw.host.identity import SESSION_ENV, resolve
from tsw.host.mcp import Server
from tsw.host.session_inbox import key_file
from tsw.host.session_labels import all_labels
from tsw.host.status_app import StatusApp
from tsw.machine import spares as spares_mod
from tsw.machine.base import Caller

POOL = "UC-SPARE-001"
TASK = "UC-SPARE-002"
CHAT = "UC-SPARE-003"
# The Dashboard reads the real clock, the harness's is in the past: no spare is too old for it.
YOUNG = {"scheduling": {"spare": {"enabled": True, "count": 2, "max_age_hours": 1_000_000}}}


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


@pytest.fixture
def inboxes() -> Iterator[Callable[[str], FakeInbox]]:
    """Fake running sessions in the registry the host would publish (CC001 §3.1)."""
    made: list[FakeInbox] = []

    def make(session: str) -> FakeInbox:
        found = FakeInbox(session_inbox.sessions_dir(), session, pid=5000 + len(made))
        made.append(found)
        return found

    yield make
    for found in made:
        found.close()


def reason_of(call: Callable[[], Any]) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def spare(h: Harness, session: str) -> str:
    """Make one spare session in the user's turn, as the orchestrator does."""
    out = h.run("spare_start", user_turn=True)
    sid = str(out["events"][0]["data"]["spare_id"])
    h.run("spare_reconcile", spare_id=sid, outcome="created", session_ref=session)
    return sid


def hook(h: Harness, session: str, event: str, cwd: Path, **fields: Any) -> Any:
    env = {SESSION_ENV: session}
    return Guard(h.home, env, {"cwd": str(cwd), "hook_event_name": event, **fields}).run(event)


def who(h: Harness, session: str, cwd: Path) -> Any:
    with h.ledger.view() as conn:
        return resolve(conn, h.home, {SESSION_ENV: session}, cwd)


def pool(h: Harness) -> dict[str, Any]:
    with h.ledger.view() as conn:
        found = inbox_mod.attention(conn, h.ledger.storage, h.config, None, h.clock.now)["spares"]
    return found[0] if found else {}


# ---------------------------------------------------------------------- the pool


@pytest.mark.use_case(POOL)
def test_spare_start_only_in_the_users_turn_and_up_to_the_target(home: Home) -> None:
    h = Harness(home)
    assert reason_of(lambda: h.run("spare_start", user_turn=False)) == "g_window"
    out = h.run("spare_start", user_turn=True)
    data = out["events"][0]["data"]
    assert data["spare_id"] == "S1" and data["workdir"] == "spares/S1"
    assert out["session_name"] == "spare-s1" and out["title"] == "[备用] S1"
    assert out["permission_mode"] == "auto" and "备用会话就绪" in out["prompt"]
    assert (home.root / "spares" / "S1").is_dir()
    # Being made: the new session is listed as the spare it is about to be.
    making = who(h, "s-1", home.root / "spares" / "S1")
    assert making.pending and making.pending_role == "spare" and making.spare_id == "S1"
    h.run("spare_reconcile", spare_id="S1", outcome="created", session_ref="s-1")
    assert who(h, "s-1", home.root / "spares" / "S1").role == "spare"
    spare(h, "s-2")
    # The default target is 2 (scheduling.spare.count).
    assert reason_of(lambda: h.run("spare_start", user_turn=True)) == "spare_pool_full"
    # A session already bound elsewhere cannot be a spare.
    h3 = Harness(home, {"scheduling": {"spare": {"enabled": True, "count": 3}}})
    sid = h3.run("spare_start", user_turn=True)["events"][0]["data"]["spare_id"]
    assert (
        reason_of(
            lambda: h3.run("spare_reconcile", spare_id=sid, outcome="created", session_ref="s-1")
        )
        == "session_in_use"
    )
    h3.run("spare_reconcile", spare_id=sid, outcome="not_created")
    assert not (home.root / "spares" / sid).exists()  # its empty folder is removed


@pytest.mark.use_case(POOL)
def test_spare_pool_off(home: Home) -> None:
    h = Harness(home, {"scheduling": {"spare": {"enabled": False, "count": 2}}})
    assert reason_of(lambda: h.run("spare_start", user_turn=True)) == "spare_disabled"
    assert pool(h) == {}


@pytest.mark.use_case(POOL)
def test_attention_needs_and_retire(home: Home, inboxes: Callable[[str], FakeInbox]) -> None:
    h = Harness(home)
    assert pool(h)["needs"] == 2
    spare(h, "s-1")
    spare(h, "s-2")
    inboxes("s-2")  # only s-2 runs
    found = pool(h)
    assert [a["spare_id"] for a in found["available"]] == ["S2"]
    assert found["retire"] == [
        {
            "spare_id": "S1",
            "session_id": "s-1",
            "created_at": found["retire"][0]["created_at"],
            "running": False,
            "reason": "not_running",
        }
    ]
    assert found["needs"] == 1
    # Archived: closed, its folder removed, out of the pool.
    out = h.run("spare_close", spare_id="S1", outcome="archived", reason="not_running")
    assert out["events"][0]["data"]["folder_removed"] is True
    assert not (home.root / "spares" / "S1").exists()
    assert [r["spare_id"] for r in pool(h)["available"]] == ["S2"]
    # Too old (scheduling.spare.max_age_hours, 24 by default).
    h.clock.now = "2026-11-06T01:00:00Z"
    assert pool(h)["retire"][0]["reason"] == "age"
    # The host refused to archive it: no longer in the pool, not reminded again.
    h.run("spare_close", spare_id="S2", outcome="failed", result="拒绝：会话正显示在屏幕上")
    assert pool(h)["needs"] == 2 and pool(h)["retire"] == []
    assert (
        h.query("SELECT status FROM spares WHERE spare_id = 'S2'")[0]["status"] == "retire_failed"
    )


@pytest.mark.use_case(POOL)
def test_stop_hook_reminds_in_the_users_turn(
    home: Home, inboxes: Callable[[str], FakeInbox]
) -> None:
    h = Harness(home)
    prompt = {"prompt": "补充备用会话", "session_id": "orch-1"}
    # A fresh install where nothing has asked for a session yet: no reminder.
    assert hook(h, "orch-1", "UserPromptSubmit", home.root, **prompt) is None
    assert hook(h, "orch-1", "Stop", home.root) is None
    # Once tsw is in use (here: a task has a session), the user's turn fills the pool.
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    hook(h, "orch-1", "UserPromptSubmit", home.root, **prompt)
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block" and "spare_start" in stop["reason"]
    # A turn begun by another session: no reminder to add (no session can be made then).
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt='<cross-session-message from="x">hi')
    assert hook(h, "orch-1", "Stop", home.root) is None
    # Retiring archives: reminded in any turn.
    spare(h, "s-1")
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt='<cross-session-message from="x">hi')
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block" and "spare_close" in stop["reason"]
    assert "不在运行" in stop["reason"] and "spare_start" not in stop["reason"]
    del inboxes


# ---------------------------------------------------------------------- claimed for a task


def repo_harness(home: Home, tmp_path: Path) -> tuple[Harness, Path]:
    repo = checkout(tmp_path)
    return Harness(home, {"repositories": {"demo": {"path": str(repo), "aliases": ["d"]}}}), repo


def claim(h: Harness, task_id: str = "T001") -> dict[str, Any]:
    h.register(task_id)
    h.plan(task_id)
    h.run("task_dispatch", task_id=task_id, user_turn=False, spare=True)
    return h.run(
        "attempt_start", task_id=task_id, purpose="dispatch", user_turn=False, spare="auto"
    )


@pytest.mark.use_case(TASK)
@pytest.mark.transition("T07")
@pytest.mark.transition("T08")
def test_a_spare_takes_a_repository_task(
    home: Home, tmp_path: Path, inboxes: Callable[[str], FakeInbox]
) -> None:
    h, repo = repo_harness(home, tmp_path)
    h.register("T001")
    h.plan("T001")
    # No spare yet: a turn that is not the user's cannot dispatch at all.
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T001", user_turn=False, spare=True))
        == "no_spare"
    )
    spare(h, "s-1")
    # In the user's own turn the orchestrator creates the session instead.
    assert (
        reason_of(lambda: h.run("task_dispatch", task_id="T001", user_turn=True, spare=True))
        == "spare_in_user_turn"
    )
    h.run("task_dispatch", task_id="T001", user_turn=False, spare=True)
    assert (
        reason_of(
            lambda: h.run(
                "attempt_start", task_id="T001", purpose="dispatch", user_turn=True, spare="auto"
            )
        )
        == "spare_in_user_turn"
    )
    out = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=False, spare="auto")
    started, claimed = out["events"]
    assert started["type"] == "attempt.start_requested" and claimed["type"] == "spare.claimed"
    worktree = home.root / "work" / "T001" / "a1" / "demo"
    assert started["data"]["spare"] == "S1" and started["data"]["session_name"] == "spare-s1"
    assert started["data"]["spare_target"] == str(worktree)
    assert started["data"]["session_title"] == "[备用] S1"
    assert started["data"]["worktree"]["by_session"] is True
    assert claimed["data"] == {
        "spare_id": "S1",
        "session_ref": "s-1",
        "as": "task",
        "task_id": "T001",
        "attempt": "a1",
        "via": "orchestrator",
        "target": str(worktree),
        "target_branch": "tsw/t001-a1",
    }
    # tsw planned the worktree but did not make it: the session makes it, so it may move there.
    assert not worktree.exists()
    assert out["session_id"] == "s-1" and "[TaskSteward 派发]" in out["claim_message"]
    assert (
        "git -C" in out["claim_message"] and "worktree add -b tsw/t001-a1" in out["claim_message"]
    )
    brief = (home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "由备用会话 S1 认领" in brief
    # Bound to the spare session, no other.
    common = {"task_id": "T001", "attempt": "a1", "generation": 1, "brief_delivered": True}

    def bind(ref: str) -> None:
        h.run("attempt_reconcile", outcome="created", session_ref=ref, **common)

    assert reason_of(lambda: bind("other")) == "spare_session_mismatch"
    bind("s-1")
    assert h.status("T001") == "in_progress"
    spare_dir = home.root / "spares" / "S1"
    worker = who(h, "s-1", spare_dir)
    # Still in the spare's folder: consistent, but only the task's paths are writable.
    assert worker.role == "worker" and worker.cwd_consistent and worker.spare_id == "S1"
    assert not any(str(r).startswith(str(spare_dir)) for r in worker.writable_roots)
    # The session runs the command it was given; then it records the move (§5.9 step 3).
    caller = Caller(role="worker", session_id="s-1", machine=MACHINE)
    assert (
        reason_of(lambda: h.run("spare_moved", caller, task_id="T001", outcome="moved"))
        == "spare_target_missing"
    )
    command = out["claim_message"].split("`")[1]
    subprocess.run(command, shell=True, check=True, capture_output=True)
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tsw/t001-a1"
    del repo
    moved = Server(home, {SESSION_ENV: "s-1"}, spare_dir).call("spare_moved", {"outcome": "moved"})
    assert moved["events"][0]["type"] == "spare.moved"
    assert moved["wake"]["send"] is True and moved["wake"]["session_id"] == "orch-1"
    assert "[TaskSteward 开始]" in moved["wake"]["message"] and "s-1" in moved["wake"]["message"]
    # The start message came before the move took effect: told to end the turn and ask again.
    start = moved["start_message"]
    early = hook(h, "s-1", "UserPromptSubmit", spare_dir, prompt=f"<cross-session-message>{start}")
    assert "目录还没移到" in early["hookSpecificOutput"]["additionalContext"]
    later = hook(h, "s-1", "UserPromptSubmit", worktree, prompt=f"<cross-session-message>{start}")
    assert later is None or "目录还没移到" not in json.dumps(later, ensure_ascii=False)
    # The session's title is renamed from the spare's to the task's (DN002 §5.8).
    titles = h.query("SELECT base, known FROM session_titles WHERE task_id = 'T001'")
    assert titles == [{"base": "T001 任务 T001", "known": "[备用] S1"}]


@pytest.mark.use_case(TASK)
def test_a_spare_takes_a_task_without_a_checkout(home: Home) -> None:
    h = Harness(home)  # the harness's repository has no checkout on this machine
    spare(h, "s-1")
    out = claim(h)
    target = home.root / "work" / "T001" / "a1" / "S1"
    assert out["target"] == str(target) and "mkdir -p" in out["claim_message"]
    assert not target.exists()


@pytest.mark.use_case(TASK)
@pytest.mark.transition("T09")
def test_a_spare_not_reached_leaves_the_pool(home: Home) -> None:
    h = Harness(home)
    spare(h, "s-1")
    claim(h)
    out = h.run(
        "attempt_reconcile", task_id="T001", attempt="a1", generation=1, outcome="not_created"
    )
    assert [e["type"] for e in out["events"]][:2] == ["spare.lost", "attempt.not_created"]
    assert h.status("T001") == "ready"
    assert h.query("SELECT status FROM spares WHERE spare_id = 'S1'")[0]["status"] == "lost"


@pytest.mark.use_case(TASK)
def test_a_taken_over_worktree_cannot_take_a_spare(home: Home, tmp_path: Path) -> None:
    h, _ = repo_harness(home, tmp_path)
    h.register("T001")
    h.plan("T001")
    worker = h.dispatch("T001")  # a1 made its worktree in the user's turn
    del worker
    h.run("attempt_update", task_id="T001", attempt="a1", event="lost", evidence="会话不在了")
    spare(h, "s-1")
    h.run("task_dispatch", task_id="T001", user_turn=False, spare=True, workdir="~/wt/T001")
    assert reason_of(
        lambda: h.run(
            "attempt_start", task_id="T001", purpose="dispatch", user_turn=False, spare="S1"
        )
    ) in ("spare_dir_exists", "spare_dir_mismatch")


@pytest.mark.use_case(TASK)
def test_auto_picks_a_running_spare(home: Home, inboxes: Callable[[str], FakeInbox]) -> None:
    h = Harness(home)
    spare(h, "s-1")
    spare(h, "s-2")
    inboxes("s-1")  # the newer S2 is not running
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", user_turn=False, spare=True)
    server = Server(home, {SESSION_ENV: "orch-1"}, home.root)
    out = server.call(
        "attempt_start",
        {"task_id": "T001", "purpose": "dispatch", "user_turn": False, "spare": "auto"},
    )
    assert out["spare_id"] == "S1" and out["session_id"] == "s-1"


# ---------------------------------------------------------------------- the user's conversation


@pytest.mark.use_case(CHAT)
def test_a_message_from_the_user_claims_a_spare(home: Home) -> None:
    h = Harness(home)
    spare(h, "s-1")
    folder = home.root / "spares" / "S1"
    start = hook(h, "s-1", "SessionStart", folder)
    assert "备用会话 S1" in start["hookSpecificOutput"]["additionalContext"]
    # CC-B10: nothing is done before it is claimed.
    denied = hook(h, "s-1", "PreToolUse", folder, tool_name="Bash", tool_input={"command": "ls"})
    assert "CC-B10" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    # Its own first prompt (the app puts the background before it) claims nothing.
    first = (
        "Background (from the session that started this one): …\n\n你是 TaskSteward 的备用会话 S1"
    )
    assert hook(h, "s-1", "UserPromptSubmit", folder, prompt=first) is None
    assert h.query("SELECT status FROM spares")[0]["status"] == "available"
    note = hook(h, "s-1", "UserPromptSubmit", folder, prompt="帮我查一下 workdeck 的日志")
    assert "set_session_title" in note["hookSpecificOutput"]["additionalContext"]
    row = h.query("SELECT status, claimed_as, claim_via FROM spares")[0]
    assert row == {"status": "claimed", "claimed_as": "chat", "claim_via": "direct"}
    now = who(h, "s-1", folder)
    assert now.role == "none" and now.claimed_as == "chat"
    # No longer held back: the user's conversation is the user's.
    assert (
        hook(h, "s-1", "PreToolUse", folder, tool_name="Bash", tool_input={"command": "ls"}) is None
    )
    with h.ledger.view() as conn:
        assert all_labels(conn)["s-1"][0]["text"] == "chat S1"


@pytest.mark.use_case(CHAT)
def test_dashboard_new_session(home: Home, inboxes: Callable[[str], FakeInbox]) -> None:
    h = Harness(home, YOUNG)
    app = StatusApp(home, token="t" * 32)
    app.sessions_dir = session_inbox.sessions_dir()
    status, out = app.new_session({"text": "你好", "command_id": "new-session-0001"})
    assert status == 409 and out["code"] == "no_spare" and "补充备用会话" in out["hint"]
    spare(h, "s-1")
    spare(h, "s-2")
    with h.ledger.view() as conn:
        labels = all_labels(conn)
    assert labels["s-1"][0]["text"] == "spare S1" and labels["s-2"][0]["text"] == "spare S2"
    running = inboxes("s-1")  # S2, the newest, is not running: it is lost, S1 is used
    assert [a["spare_id"] for a in app.spares()["available"]] == ["S1"]
    status, out = app.new_session({"text": "帮我看看日志", "command_id": "new-session-0002"})
    assert status == 200 and out["state"] == "sent" and out["spare_id"] == "S1"
    assert out["href"] == "claude://claude.ai/epitaxy/s-1"
    rows = {r["spare_id"]: r for r in h.query("SELECT spare_id, status, claimed_as FROM spares")}
    assert rows["S1"]["status"] == "claimed" and rows["S1"]["claimed_as"] == "chat"
    assert rows["S2"]["status"] in ("available", "lost")
    lines = [json.loads(x) for x in b"".join(running.received).decode("utf-8").splitlines()]
    assert lines[1]["from"] == "tsw-dashboard"
    assert lines[1]["message"]["content"] == "帮我看看日志"
    # The same submission again: answered from memory, nothing written twice.
    again_status, again = app.new_session(
        {"text": "帮我看看日志", "command_id": "new-session-0002"}
    )
    assert again_status == 200 and again["duplicate"] is True
    assert len(running.received) == 1
    # Too long, or no command_id: refused before anything is claimed.
    assert app.new_session({"text": "x" * 5000, "command_id": "new-session-0003"})[0] == 400
    assert app.new_session({"text": "x"})[0] == 400


@pytest.mark.use_case(CHAT)
def test_dashboard_skips_a_spare_that_is_gone(
    home: Home, inboxes: Callable[[str], FakeInbox]
) -> None:
    h = Harness(home, YOUNG)
    spare(h, "s-1")
    spare(h, "s-2")
    # S2 is still in the registry, but its inbox no longer opens (the session went away).
    folder = session_inbox.sessions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dead = r"\\.\pipe\tsw-test-gone" if os.name == "nt" else str(folder / "gone.sock")
    entry = {"pid": 6001, "hostSessionId": "s-2", "messagingSocketPath": dead, "updatedAt": 2}
    (folder / "6001.json").write_text(json.dumps(entry), encoding="utf-8")
    key_file(folder, 6001, dead).write_text(json.dumps({"peerToken": "x"}), encoding="utf-8")
    alive = inboxes("s-1")
    app = StatusApp(home, token="t" * 32)
    app.sessions_dir = session_inbox.sessions_dir()
    status, out = app.new_session({"text": "开始", "command_id": "new-session-0004"})
    assert status == 200 and out["spare_id"] == "S1"
    statuses = {r["spare_id"]: r["status"] for r in h.query("SELECT spare_id, status FROM spares")}
    assert statuses == {"S1": "claimed", "S2": "lost"}
    assert alive.received


@pytest.mark.use_case(CHAT)
def test_spare_claim_tool_is_the_users(home: Home) -> None:
    h = Harness(home)
    spare(h, "s-1")
    source = {
        "channel": "status-app",
        "principal": "me",
        "message_ref": "status-app:x:1",
        "text": "hi",
        "target": ["spare", "S1", "chat"],
    }
    with pytest.raises(TswError):
        h.run("spare_claim", ORCH, spare_id="S1", user_source=source)
    user = Caller(role="user", session_id=None, machine=MACHINE)
    out = h.run("spare_claim", user, spare_id="S1", user_source=source)
    assert out["events"][0]["data"]["via"] == "status-app"
    assert reason_of(lambda: h.run("spare_claim", user, spare_id="S1", user_source=source)) == (
        "spare_unavailable"
    )
    with h.ledger.view() as conn:
        assert spares_mod.available(conn) == []
