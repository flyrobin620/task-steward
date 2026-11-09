"""The routine session and its routine board (DN006 §8.7, §8.9), through the real tools, the
projection and the hooks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import START_SESSION, Guard
from tsw.host.identity import resolve
from tsw.machine import routine as routine_mod
from tsw.machine.base import Caller

TEAMS = {"plugins": {"human_channels": ["in-session", "teams-chat"]}}


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def reason_of(call: Callable[[], Any]) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def start_routine(h: Harness, session: str = "routine-1") -> Caller:
    out = h.run("routine_start", user_turn=True)
    data = out["events"][0]["data"]
    assert (h.home.root / "routine").is_dir()
    h.run("routine_bind", generation=data["generation"], session_id=session)
    return Caller(role="routine", session_id=session, machine=MACHINE)


def board(h: Harness, routine: Caller, cache: dict[str, Any]) -> dict[str, Any]:
    with h.ledger.view() as conn:
        result, kept = routine_mod.board(conn, h.config, h.clock.now, cache, routine.session_id)
    cache.clear()
    cache.update(kept)
    return result


def run_job(h: Harness, routine: Caller, job_id: str, **extra: Any) -> tuple[list, str]:
    out = h.run("routine_run", routine, job_id=job_id, **extra)
    with h.ledger.view() as conn:
        message = routine_mod.forward_text(conn, job_id, out["events"])
    return out["events"], message


def hook(h: Harness, session: str, event: str, cwd: Path, **fields: Any) -> Any:
    env = {"CLAUDE_CODE_HOST_SESSION_ID": session}
    return Guard(h.home, env, {"cwd": str(cwd), "hook_event_name": event, **fields}).run(event)


def attention(h: Harness, cache: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with h.ledger.view() as conn:
        return inbox_mod.attention(conn, h.ledger.storage, h.config, cache, h.clock.now)["routine"]


def asked(h: Harness, task_id: str = "T001", **extra: Any) -> tuple[Caller, str]:
    h.register(task_id)
    h.plan(task_id)
    worker = h.dispatch(task_id)
    out = h.run("ask_user", worker, task_id=task_id, question="用方案 A 还是 B？", **extra)
    return worker, str(out["events"][0]["data"]["request_id"])


@pytest.mark.use_case("UC-ROUTINE-001")
def test_start_bind_and_role(home: Home) -> None:
    h = Harness(home)
    # Like a worker, the routine session is created only in the user's own turn.
    assert reason_of(lambda: h.run("routine_start", user_turn=False)) == "g_window"
    out = h.run("routine_start", user_turn=True)
    data = out["events"][0]["data"]
    assert data["session_name"] == "routine-g1" and data["model"] == "claude-sonnet-5-5"
    assert data["effort"] == "low" and data["permission_mode"] == "auto"
    assert data["host_permission_mode"] == "auto"  # the Claude app's name for it
    # Between creation and binding the session is listed as the routine session it will be.
    with h.ledger.view() as conn:
        pending = resolve(conn, home, {"CLAUDE_CODE_HOST_SESSION_ID": "r1"}, home.root / "routine")
    assert pending.pending and pending.pending_role == "routine"
    assert h.query("SELECT status FROM routine_sessions") == [{"status": "starting"}]
    with h.ledger.view() as conn:
        assert routine_mod.status(conn, h.config, h.clock.now, {})["state"] == "starting"
    # The host's start_session is let through only for the recorded name (CC-B9).
    allowed = hook(
        h,
        "orch-1",
        "PreToolUse",
        home.root,
        tool_name=START_SESSION,
        tool_input={"name": "routine-g1"},
    )
    assert allowed is None
    denied = hook(
        h,
        "orch-1",
        "PreToolUse",
        home.root,
        tool_name=START_SESSION,
        tool_input={"name": "routine-g7"},
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    h.run("routine_bind", generation=1, session_id="r1")
    assert reason_of(lambda: h.run("routine_bind", generation=1, session_id="r1")) in (
        "already_bound",
    )
    with h.ledger.view() as conn:
        who = resolve(conn, home, {"CLAUDE_CODE_HOST_SESSION_ID": "r1"}, home.root / "routine")
    assert who.role == "routine" and who.attempt == "routine-g1"
    # A second generation replaces the first: the old session is revoked.
    h.run("routine_start", user_turn=True)
    h.run("routine_bind", generation=2, session_id="r2")
    with h.ledger.view() as conn:
        old = resolve(conn, home, {"CLAUDE_CODE_HOST_SESSION_ID": "r1"}, home.root / "routine")
    assert old.role == "revoked"
    stale = Caller(role="routine", session_id="r1", machine=MACHINE)
    assert reason_of(lambda: h.run("routine_run", stale, job_id="handoff-scan")) == (
        "not_bound_routine"
    )
    # A session already bound as a worker cannot become the routine session.
    worker = asked(h)[0]
    h.run("routine_start", user_turn=True)
    assert (
        reason_of(lambda: h.run("routine_bind", generation=3, session_id=str(worker.session_id)))
        == "session_in_use"
    )


@pytest.mark.use_case("UC-ROUTINE-001")
def test_board_arms_renews_clears_and_recovers(home: Home) -> None:
    h = Harness(home)
    routine = start_routine(h)
    cache: dict[str, Any] = {}
    first = board(h, routine, cache)
    assert [m["action"] for m in first["maintenance"]] == ["arm"]
    arm = first["maintenance"][0]
    assert arm["cron"] == "*/10 * * * *" and arm["replace"] is None
    assert arm["prompt"].startswith("<scheduled-task>")
    assert [j["job_id"] for j in first["jobs"]] == ["handoff-scan"]
    assert first["orchestrator_session"] == "orch-1"
    assert attention(h, cache) == [
        {
            "state": "unarmed",
            "session_name": "routine-g1",
            "session_id": "routine-1",
            "armed_at": None,
            "last_run": h.clock.now,
        }
    ]
    # Bound but never armed (it read the board, then stopped before arming): stale once the
    # window has passed since that run, whether or not the run was cached.
    h.clock.now = "2026-11-05T00:29:00Z"
    assert attention(h, cache)[0]["state"] == "unarmed"
    h.clock.now = "2026-11-05T00:31:00Z"
    assert attention(h, cache)[0]["state"] == "stale"
    assert attention(h, {})[0]["state"] == "stale"
    h.clock.now = "2026-11-05T00:00:00Z"
    h.run("routine_armed", routine, job_ref="job-1", cron=arm["cron"], interval_minutes=10)
    assert attention(h, cache) == []
    h.clock.now = "2026-11-05T00:10:00Z"
    assert board(h, routine, cache)["maintenance"] == []
    # No run for three intervals (the host restarted, the timer is gone): stale, then re-armed.
    h.clock.now = "2026-11-05T01:00:00Z"
    assert attention(h, cache)[0]["state"] == "stale"
    again = board(h, routine, cache)
    assert [m["action"] for m in again["maintenance"]] == ["arm"]
    assert again["maintenance"][0]["replace"] == "job-1"
    h.run("routine_armed", routine, job_ref="job-2", cron="*/10 * * * *", interval_minutes=10)
    assert attention(h, cache) == []
    # A day after binding: clear the context once.
    h.clock.now = "2026-11-06T00:00:01Z"
    cache["last_run"] = "2026-11-05T23:55:00Z"
    actions = [m["action"] for m in board(h, routine, cache)["maintenance"]]
    assert actions == ["clear_context"]
    h.clock.now = "2026-11-06T00:10:00Z"
    assert board(h, routine, cache)["maintenance"] == []
    # Six days after arming: renew before the host's seven-day expiry.
    h.clock.now = "2026-11-11T01:00:01Z"
    cache["last_run"] = "2026-11-11T00:55:00Z"
    renew = [m for m in board(h, routine, cache)["maintenance"] if m["action"] == "arm"]
    assert renew and renew[0]["replace"] == "job-2"
    # Only the routine session in charge reads the board.
    with h.ledger.view() as conn, pytest.raises(TswError):
        routine_mod.board(conn, h.config, h.clock.now, {}, "someone-else")


@pytest.mark.use_case("UC-ROUTINE-001")
@pytest.mark.use_case("UC-ASK-001")
def test_handoff_scan_hands_off_and_forwards(home: Home) -> None:
    h = Harness(home)
    routine = start_routine(h)
    worker, request_id = asked(h)
    assert run_job(h, routine, "handoff-scan") == ([], "")
    h.clock.now = "2026-11-05T00:10:00Z"
    events, message = run_job(h, routine, "handoff-scan")
    assert [(e["type"], e["data"]["by"]) for e in events] == [("input.handed_off", "system")]
    assert request_id in message and "用方案 A 还是 B？" in message
    assert str(worker.session_id) in message and "handle-input" in message
    assert run_job(h, routine, "handoff-scan") == ([], "")  # handed off once only
    with h.ledger.view() as conn:
        items = inbox_mod.inbox(conn, h.ledger.storage, h.config)
    assert [i["type"] for i in items] == ["input.handed_off"]
    assert reason_of(lambda: h.run("routine_run", routine, job_id="nope")) == "unknown_job"
    assert reason_of(lambda: h.run("routine_run", job_id="handoff-scan")) == "role_denied"
    # The orchestrator sat idle past its lease: it is still the one to wake (it takes the lease
    # again before it writes).
    with h.ledger.view() as conn:
        assert routine_mod.orchestrator_session(conn, "2099-01-01T00:00:00Z") == "orch-1"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_teams_round_trip(home: Home) -> None:
    h = Harness(home, TEAMS)
    routine = start_routine(h)
    _, request_id = asked(h)
    h.clock.now = "2026-11-05T00:10:00Z"
    run_job(h, routine, "handoff-scan")
    assert (
        reason_of(
            lambda: h.run(
                "channel_sent", task_id="T001", request_id=request_id, chat_id="c1", message_id="m1"
            )
        )
        == "not_relayed"
    )
    relayed = h.run("input_relay", task_id="T001", request_id=request_id, channel="teams-chat")
    data = relayed["events"][0]["data"]
    assert data["card"].startswith(f"{routine_mod.CARD_PREFIX} T001 {request_id}")
    assert routine_mod.WORKER_LINK in data["card"] and data["chat"] == "self"
    cache: dict[str, Any] = {}
    assert [j["job_id"] for j in board(h, routine, cache)["jobs"]] == ["handoff-scan"]
    sent = h.run(
        "channel_sent",
        task_id="T001",
        request_id=request_id,
        chat_id="chat-1",
        message_id="card-1",
        sender_id="u1",
    )
    assert [e["type"] for e in sent["events"]] == ["channel.message_sent", "routine.job_added"]
    jobs = board(h, routine, cache)["jobs"]
    teams = jobs[1]
    assert teams["job_id"] == "teams-replies" and teams["input"]["tasks"] == ["T001"]
    assert teams["run"] == {"job_id": "teams-replies", "chat_id": "chat-1"}
    assert teams["fetch"]["tool"] == "mcp__teams__ListChatMessages"
    assert teams["fetch"]["arguments"] == {"chatId": "chat-1", "top": routine_mod.TEAMS_READ_TOP}
    # The window starts just before the waiting question's card, however long ago that was.
    assert teams["fetch"]["since"] == "2026-11-05T00:05:00Z"
    fetched = {
        "messages": [
            {
                "id": "card-1",
                "createdDateTime": "2026-11-05T00:10:30Z",
                "from": {"user": {"id": "u1"}},
                "body": {"content": data["card"]},
            },
            {
                "id": "r1",
                "createdDateTime": "2026-11-05T00:12:00Z",
                "from": {"user": {"id": "u1"}},
                "body": {"content": "<p>T001 用方案 B</p>"},
            },
        ]
    }
    assert reason_of(lambda: h.run("routine_run", routine, job_id="teams-replies")) == (
        "unknown_job"
    )
    events, message = run_job(h, routine, "teams-replies", chat_id="chat-1", fetched=fetched)
    assert [(e["type"], e["data"]["message_id"]) for e in events] == [("channel.reply_seen", "r1")]
    assert "T001 用方案 B" in message and request_id in message
    again = run_job(h, routine, "teams-replies", chat_id="chat-1", fetched=fetched)
    assert again == ([], "")  # passed on once
    # One page at a time: the script asks for the next page until it reaches back past the card.
    run = {"job_id": "teams-replies", "chat_id": "chat-1"}
    with h.ledger.view() as conn:
        assert routine_mod.run_next(conn, "teams-replies", run, "claude-code") is None
        follow = routine_mod.run_next(
            conn, "teams-replies", {**run, "fetched": {**fetched, "nextLink": "p2"}}, "claude-code"
        )
        assert follow == {
            "tool": "mcp__teams__ListChatMessages",
            "arguments": {"chatId": "chat-1", "nextLink": "p2"},
            "run": run,
        }
        older = {
            "messages": [{"id": "x", "createdDateTime": "2026-11-05T00:04:59Z"}],
            "nextLink": "p3",
        }
        assert routine_mod.run_next(conn, "teams-replies", {**run, "fetched": older}, "x") is None
        assert routine_mod.run_next(conn, "handoff-scan", {"fetched": fetched}, "x") is None
    # The orchestrator decides it answers the question and records it with the Teams source.
    out = h.run(
        "input_resolve",
        task_id="T001",
        request_id=request_id,
        answer="用 B",
        user_source={
            "channel": "teams-chat",
            "principal": "teams",
            "message_ref": "r1",
            "text": "T001 用方案 B",
            "request_id": request_id,
            "target": ["T001", 1, "answer"],
        },
    )
    assert out["events"][0]["data"]["transition"] == "T12"
    assert h.status("T001") == "in_progress"
    # Answered: the task leaves the Teams job's list, and the job is no longer run.
    assert [j["job_id"] for j in board(h, routine, cache)["jobs"]] == ["handoff-scan"]
    # Questions waiting in two chats (the configured chat changed in between): one row each, and
    # a reply is read only in the chat its question went to.
    _, second = asked(h, "T002")
    _, third = asked(h, "T003")
    h.clock.now = "2026-11-05T00:30:00Z"
    run_job(h, routine, "handoff-scan")
    for task_id, request, chat, card in (
        ("T002", second, "chat-1", "card-2"),
        ("T003", third, "chat-2", "card-3"),
    ):
        h.run("input_relay", task_id=task_id, request_id=request, channel="teams-chat")
        out = h.run(
            "channel_sent", task_id=task_id, request_id=request, chat_id=chat, message_id=card
        )
        assert [e["type"] for e in out["events"]] == ["channel.message_sent"]
    rows = [j for j in board(h, routine, cache)["jobs"] if j["job_id"] == "teams-replies"]
    assert [(j["input"]["chat_id"], j["input"]["tasks"]) for j in rows] == [
        ("chat-1", ["T002"]),
        ("chat-2", ["T003"]),
    ]
    # Teams message IDs are unique within a chat only: r1 was already passed on from chat-1.
    reply = {"id": "r1", "createdDateTime": "2026-11-05T00:40:00Z", "body": {"content": "T003 好"}}
    assert run_job(h, routine, "teams-replies", chat_id="chat-1", fetched=[reply]) == ([], "")
    events, _ = run_job(h, routine, "teams-replies", chat_id="chat-2", fetched=[reply])
    assert [e["data"]["request_id"] for e in events] == [third]


@pytest.mark.use_case("UC-ROUTINE-001")
def test_hooks_for_the_routine_session(home: Home) -> None:
    h = Harness(home)
    start_routine(h, "r1")
    cwd = home.root / "routine"
    start = hook(h, "r1", "SessionStart", cwd)
    assert "routine_board" in start["hookSpecificOutput"]["additionalContext"]
    for tool, tool_input in (
        ("Write", {"file_path": str(cwd / "x.txt")}),
        ("Bash", {"command": "echo hi"}),
        (START_SESSION, {"name": "routine-g1"}),
    ):
        denied = hook(h, "r1", "PreToolUse", cwd, tool_name=tool, tool_input=tool_input)
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason.startswith("CC-B11"), tool
    untagged = hook(
        h, "r1", "PreToolUse", cwd, tool_name="CronCreate", tool_input={"prompt": "routine_board"}
    )
    assert untagged["hookSpecificOutput"]["permissionDecision"] == "deny"
    tagged = hook(
        h,
        "r1",
        "PreToolUse",
        cwd,
        tool_name="CronCreate",
        tool_input={"prompt": routine_mod.SCHEDULED_PROMPT},
    )
    assert tagged is None
    assert hook(h, "r1", "PreToolUse", cwd, tool_name="mcp__teams__ListChatMessages") is None
    # A routine turn ends with nothing to record and nothing to remind.
    assert hook(h, "r1", "Stop", cwd) is None
    assert hook(h, "r1", "UserPromptSubmit", cwd, prompt=routine_mod.SCHEDULED_PROMPT) is None
    assert h.query("SELECT COUNT(*) AS n FROM session_messages") == [{"n": 0}]


@pytest.mark.use_case("UC-ROUTINE-001")
def test_the_orchestrator_is_reminded(home: Home) -> None:
    h = Harness(home)
    # Nothing is being worked on: no reminder about the routine session.
    assert attention(h) == []
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    assert attention(h) == [{"state": "missing"}]
    # The reminder comes at the end of the user's turn, not in other turns.
    assert hook(h, "orch-1", "UserPromptSubmit", home.root, prompt="执行 T001") is None
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block" and "routine_start" in stop["reason"]
    injected = '<cross-session-message from="w">x</cross-session-message>'
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt=injected)
    assert hook(h, "orch-1", "Stop", home.root) is None
    routine = start_routine(h)
    cache: dict[str, Any] = {}
    board(h, routine, cache)
    h.run("routine_armed", routine, job_ref="j1", cron="*/10 * * * *", interval_minutes=10)
    # Hooks read the real clock, well past the harness's: no run since arming, so the routine
    # session is stale, and the orchestrator is told to wake it in any turn.
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt=injected)
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block" and "routine_board" in stop["reason"]
    assert "routine-1" in stop["reason"]
