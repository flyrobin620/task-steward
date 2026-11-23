"""The routine session and its routine board (DN006 §8.7, §8.9), through the real tools, the
projection and the hooks."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tsw import lease
from tsw.config import load_config
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import START_SESSION, Guard
from tsw.host.identity import SESSION_ENV, resolve
from tsw.host.mcp import Server
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


def start_routine(h: Harness, session: str = "routine-1", *, detach: bool = True) -> Caller:
    out = h.run("routine_start", user_turn=True)
    data = out["events"][0]["data"]
    assert (h.home.root / "routine").is_dir()
    h.run("routine_bind", generation=data["generation"], session_id=session)
    if detach:
        assert data["detach"] is True  # the Claude app wakes the starter on every turn
        h.run("routine_detached", generation=data["generation"])
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
    assert data["detach"] is True  # detach it from the orchestrator once bound (CC006 §6.1)
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
    assert [j["job_id"] for j in first["jobs"]] == ["handoff-scan", "session-scan"]
    assert first["orchestrator_session"] == "orch-1"
    # Recent run times stay in the cache (live evidence), at most RECENT_RUNS of them.
    assert cache["runs"] == [h.clock.now]
    longer = {**cache, "runs": [f"r{i}" for i in range(routine_mod.RECENT_RUNS)]}
    with h.ledger.view() as conn:
        kept = routine_mod.board(conn, h.config, h.clock.now, longer, routine.session_id)[1]
    assert len(kept["runs"]) == routine_mod.RECENT_RUNS and kept["runs"][-1] == h.clock.now
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
    # What goes out is rich text (T256): each part in its own paragraph, the task's title
    # escaped, and the worker's link both as the target and the text of a link.
    rich = data["card_html"]
    assert rich.startswith(f"<p><b>{routine_mod.CARD_PREFIX} T001 {request_id}</b></p>")
    assert "<p>用方案 A 还是 B？</p>" in rich and "\n" not in rich
    link = routine_mod.WORKER_LINK
    assert f'<a href="{link}">{link}</a>' in rich
    assert routine_mod.plain_text(rich).startswith(f"{routine_mod.CARD_PREFIX} T001 {request_id}")
    cache: dict[str, Any] = {}
    assert [j["job_id"] for j in board(h, routine, cache)["jobs"]] == [
        "handoff-scan",
        "session-scan",
    ]
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
    teams = jobs[2]
    assert teams["job_id"] == "teams-replies" and teams["input"]["tasks"] == ["T001"]
    assert teams["run"] == {"job_id": "teams-replies", "chat_id": "chat-1"}
    assert teams["fetch"]["tool"] == "mcp__workiq__fetch"
    assert teams["fetch"]["arguments"] == {
        "entityUrls": [
            f"/me/chats/chat-1/messages?$top={routine_mod.TEAMS_READ_TOP}"
            "&$orderby=createdDateTime+desc&$select=id,createdDateTime,lastModifiedDateTime"
        ]
    }
    # Every run starts the chat's read again from the newest page (T286).
    assert cache["teams_steps"] == {"chat-1": routine_mod.first_step("chat-1")}
    # The window starts just before the waiting question's card, however long ago that was.
    assert teams["fetch"]["since"] == "2026-11-05T00:05:00Z"
    fetched = {
        "messages": [
            {
                "id": "card-1",
                "createdDateTime": "2026-11-05T00:10:30Z",
                "from": {"user": {"id": "u1"}},
                "body": {"contentType": "html", "content": rich},
            },
            {
                # The same card sent again by hand (not registered): its text starts with the
                # prefix, so it is still not a reply.
                "id": "card-again",
                "createdDateTime": "2026-11-05T00:11:00Z",
                "from": {"user": {"id": "u1"}},
                "body": {"contentType": "html", "content": rich},
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
    # The step tsw keeps for the chat is what the routine session fetches next, with the row's run.
    run = {"job_id": "teams-replies", "chat_id": "chat-1"}
    assert routine_mod.run_next("chat-1", None, "claude-code") is None
    step = routine_mod.first_step("chat-1")
    assert routine_mod.run_next("chat-1", step, "claude-code") == {
        "tool": "mcp__workiq__fetch",
        "arguments": step["arguments"],
        "run": run,
    }
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
    assert [j["job_id"] for j in board(h, routine, cache)["jobs"]] == [
        "handoff-scan",
        "session-scan",
    ]
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


IN_MAIN = "in-session：在主会话中直接问用户"
BY_TEAMS = "teams-chat：按 teams-chat skill 发卡片"


@pytest.mark.use_case("UC-ROUTINE-001")
@pytest.mark.use_case("UC-ASK-001")
@pytest.mark.parametrize(
    ("config", "told", "not_told"),
    [
        (TEAMS, [IN_MAIN, BY_TEAMS], []),
        ({}, [IN_MAIN], [BY_TEAMS]),
        ({"plugins": {"human_channels": ["teams-chat"]}}, [BY_TEAMS], [IN_MAIN]),
        # input.channels picks the channels a question goes to among the loaded ones.
        ({**TEAMS, "input": {"channels": ["teams-chat"]}}, [BY_TEAMS], [IN_MAIN]),
        ({"plugins": {"human_channels": []}}, ["配置中没有人类渠道"], [IN_MAIN, BY_TEAMS]),
    ],
)
def test_a_handed_off_question_names_the_channels_to_use(
    home: Home, config: dict[str, Any], told: list[str], not_told: list[str]
) -> None:
    # T256: plugins.human_channels decides how the orchestrator passes a question for the user
    # on: in-session asks in its own session, teams-chat goes through the channel's skill.
    h = Harness(home, config)
    routine = start_routine(h)
    asked(h)
    server = Server(h.home, {SESSION_ENV: str(routine.session_id)}, home.root / "routine")
    forward = server.call("routine_run", {"job_id": "handoff-scan"})["forward"]
    assert forward["send"] is True and forward["session_id"] == "orch-1"
    assert "handle-input" in forward["message"]
    assert all(t in forward["message"] for t in told), forward["message"]
    assert not any(t in forward["message"] for t in not_told), forward["message"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_chat_that_cannot_be_read_is_reported_once(home: Home) -> None:
    # T253: 54 runs of HTTP 429 stayed in the detached routine session; after a few failed reads
    # in a row tsw tells the orchestrator, once, and lists it in attention until a read works.
    h = Harness(home, TEAMS)
    routine = start_routine(h)
    _, request_id = asked(h)
    h.clock.now = "2026-11-05T00:10:00Z"
    run_job(h, routine, "handoff-scan")
    h.run("input_relay", task_id="T001", request_id=request_id, channel="teams-chat")
    h.run(
        "channel_sent",
        task_id="T001",
        request_id=request_id,
        chat_id="48:notes",
        message_id="1794241478651",
        sender_id="u1",
    )
    server = Server(h.home, {SESSION_ENV: str(routine.session_id)}, home.root / "routine")
    board_out = server.call("routine_board", {})
    teams = next(j for j in board_out["jobs"] if j["job_id"] == "teams-replies")
    assert teams["fetch"]["tool"] == "mcp__workiq__fetch" and teams["fetch"]["on_error"]
    run = teams["run"]
    throttled = "Error: HTTP 429 Too Many Requests"
    after = routine_mod.TEAMS_FETCH_ALERT_AFTER
    forwards = [
        server.call("routine_run", {**run, "fetched": throttled})["forward"]
        for _ in range(after + 1)
    ]
    assert [f["send"] for f in forwards] == [False] * (after - 1) + [True, False]
    alert = forwards[after - 1]
    assert alert["session_id"] == "orch-1"
    assert "48:notes" in alert["message"] and "HTTP 429" in alert["message"]
    assert f"T001 {request_id}" in alert["message"]
    cache = routine_mod.load_cache(h.home)
    failing = [a for a in attention(h, cache) if a["state"] == "fetch_failing"]
    assert failing == [
        {
            "state": "fetch_failing",
            "job_id": "teams-replies",
            "chat_id": "48:notes",
            "count": after + 1,
            "since": failing[0]["since"],
            "reason": throttled,
        }
    ]
    # A WorkIQ result in error counts the same; a page ends the run of failures.
    error = {"results": [{"data": {"error": {"code": "TooMany"}}, "statusCode": 429}]}
    assert server.call("routine_run", {**run, "fetched": error})["forward"] == {"send": False}
    page = {"results": [{"data": {"value": []}, "statusCode": 200}]}
    assert server.call("routine_run", {**run, "fetched": page})["forward"] == {"send": False}
    cache = routine_mod.load_cache(h.home)
    assert [a for a in attention(h, cache) if a["state"] == "fetch_failing"] == []
    # After it, failures count from one again.
    again = server.call("routine_run", {**run, "fetched": throttled})
    assert again["forward"] == {"send": False}


def teams_id(at: str) -> str:
    """A Teams message ID: the time the message was created, in milliseconds."""
    return str(int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000))


def listed(*times: str) -> dict[str, Any]:
    """A listed page as mcp__workiq__fetch returns it: IDs, creation and last change times."""
    value = [{"id": teams_id(t), "createdDateTime": t, "lastModifiedDateTime": t} for t in times]
    data: dict[str, Any] = {"@odata.count": len(value), "value": value}
    if len(value) >= routine_mod.TEAMS_READ_TOP:
        data["@odata.nextLink"] = "/me/chats/48:notes/messages?$skiptoken=abc"
    return {"note": "Results limited", "results": [{"data": data, "statusCode": 200}]}


def bodies(*messages: tuple[str, str]) -> dict[str, Any]:
    """The bodies read by ID, one result per path."""
    return {
        "results": [
            {
                "data": {
                    "id": mid,
                    "from": {"user": {"id": "u1"}},
                    "body": {"contentType": "html", "content": f"<p>{text}</p>"},
                },
                "statusCode": 200,
            }
            for mid, text in messages
        ]
    }


def fetched_by_hook(h: Harness, routine: Caller, arguments: Any, result: Any) -> Any:
    """The routine session fetched with ``arguments``: its PostToolUse hook keeps the result."""
    return hook(
        h,
        str(routine.session_id),
        "PostToolUse",
        h.home.root / "routine",
        tool_name="mcp__workiq__fetch",
        tool_input=arguments,
        tool_response=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
    )


def teams_waiting(h: Harness) -> tuple[Caller, str, str, Server]:
    """A Teams question waiting in the user's self chat; its card's Teams ID."""
    routine = start_routine(h)
    _, request_id = asked(h)
    h.clock.now = "2026-11-05T00:10:00Z"
    run_job(h, routine, "handoff-scan")
    h.run("input_relay", task_id="T001", request_id=request_id, channel="teams-chat")
    card = teams_id("2026-11-05T00:09:59.500Z")
    h.run(
        "channel_sent",
        task_id="T001",
        request_id=request_id,
        chat_id="48:notes",
        message_id=card,
        sender_id="u1",
    )
    server = Server(h.home, {SESSION_ENV: str(routine.session_id)}, h.home.root / "routine")
    return routine, request_id, card, server


@pytest.mark.use_case("UC-CHANNEL-001")
def test_teams_replies_lists_ids_then_reads_the_bodies_left(home: Home) -> None:
    # T286: whole pages of long cards were copied by the routine session into routine_run and
    # came out cut or changed. Now a page is listed first, tsw's own cards are taken out, only the
    # rest are read in full, every result reaches tsw through the hook, and each text passed on is
    # cut to routine.teams_message_chars.
    h = Harness(home, {**TEAMS, "routine": {"teams_message_chars": 100}})
    routine, request_id, card, server = teams_waiting(h)
    teams = next(
        j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
    )
    run = teams["run"]
    assert run == {"job_id": "teams-replies", "chat_id": "48:notes"}
    assert "不用传 fetched" in teams["fetch"]["then"]
    # Page one, newest first and full: two replies, notes, and tsw's card at the end.
    long_reply = "T001 " + "按方案 B 做，另外" * 30
    notes = [f"2026-11-05T00:1{m}:00.000Z" for m in range(8, 1, -1)]
    times = ["2026-11-05T00:20:00.000Z", "2026-11-05T00:19:00.000Z", *notes]
    page = listed(*times, "2026-11-05T00:09:59.500Z")
    told = fetched_by_hook(h, routine, teams["fetch"]["arguments"], page)
    assert "routine_run" in told["hookSpecificOutput"]["additionalContext"]
    first = server.call("routine_run", run)
    assert first["events"] == [] and first["forward"] == {"send": False}
    step = first["next"]
    assert step["tool"] == "mcp__workiq__fetch" and step["run"] == run
    urls = step["arguments"]["entityUrls"]
    assert len(urls) == 9 and not any(card in u for u in urls)  # the card is never read
    assert urls[0] == f"/me/chats/48:notes/messages/{teams_id(times[0])}?$select=id,from,body"
    texts = [long_reply, "T001 用方案 B", *(f"笔记 {n}" for n in range(7))]
    fetched_by_hook(
        h, routine, step["arguments"], bodies(*zip(map(teams_id, times), texts, strict=True))
    )
    second = server.call("routine_run", run)
    seen = [e["data"] for e in second["events"]]
    assert [(d["message_id"], d["request_id"]) for d in seen] == [
        (teams_id(times[1]), request_id),
        (teams_id(times[0]), request_id),
    ]
    assert seen[1]["text"] == long_reply[:100]
    assert seen[1]["truncated"] is True and seen[1]["length"] == len(long_reply)
    assert "truncated" not in seen[0]
    message = second["forward"]["message"]
    assert second["forward"]["send"] is True and "T001 用方案 B" in message
    assert f"正文已截断，原文 {len(long_reply)} 字" in message
    assert f"/me/chats/48:notes/messages/{teams_id(times[0])}?$select=id,from,body" in message
    # Page one reached back to the card, still inside the window: the page before it is next.
    older = second["next"]["arguments"]["entityUrls"][0]
    assert older.endswith("&$filter=createdDateTime+lt+2026-11-05T00:09:59.500Z")
    fetched_by_hook(h, routine, second["next"]["arguments"], listed("2026-11-05T00:01:00.000Z"))
    last = server.call("routine_run", run)
    assert last["events"] == [] and "next" not in last
    assert routine_mod.load_cache(h.home)["teams_steps"] == {}
    # The next run starts again from the newest page. The replies are not passed on twice, and
    # the notes, read once and not passed on, are not read again (T293): straight to the page
    # before.
    judged = routine_mod.load_cache(h.home)["teams_checked"]["48:notes"]["messages"]
    assert set(judged) == {teams_id(t) for t in notes}
    server.call("routine_board", {})
    fetched_by_hook(h, routine, teams["fetch"]["arguments"], page)
    again = server.call("routine_run", run)
    assert again["events"] == []
    assert again["next"]["arguments"] == second["next"]["arguments"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_teams_read_that_does_not_hold_together_counts_as_failed(home: Home) -> None:
    h = Harness(home, TEAMS)
    routine, _, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    reply_at = "2026-11-05T00:12:00.000Z"
    # Bodies that never match what was asked for: each run lists fine but fails on the bodies.
    # The listing does not end the run of failures, so the orchestrator is still told (T286).
    forwards = []
    for _ in range(routine_mod.TEAMS_FETCH_ALERT_AFTER):
        teams = next(
            j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
        )
        fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(reply_at))
        step = server.call("routine_run", run)["next"]
        fetched_by_hook(h, routine, step["arguments"], bodies(("1794000000000", "T001 好")))
        out = server.call("routine_run", run)
        assert out["events"] == [] and "next" not in out
        forwards.append(out["forward"])
    assert [f["send"] for f in forwards] == [False] * (routine_mod.TEAMS_FETCH_ALERT_AFTER - 1) + [
        True
    ]
    assert "请求的候选" in forwards[-1]["message"]
    # A step that failed leaves nothing judged (T293): the reply is read again next run.
    assert "teams_checked" not in routine_mod.load_cache(h.home)
    # Nothing kept by the hook and nothing passed: a failed read, not an empty page.
    server.call("routine_board", {})
    assert server.call("routine_run", run)["events"] == []
    reason = routine_mod.load_cache(h.home)["fetch_failures"]["48:notes"]["reason"]
    assert "hook 没有记下" in reason
    # A listing whose IDs and times disagree is not used either.
    teams = next(
        j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
    )
    bad = listed(reply_at)
    bad["results"][0]["data"]["value"][0]["createdDateTime"] = "2026-11-05T00:13:00.000Z"
    fetched_by_hook(h, routine, teams["fetch"]["arguments"], bad)
    out = server.call("routine_run", run)
    assert "next" not in out
    assert "不一致" in routine_mod.load_cache(h.home)["fetch_failures"]["48:notes"]["reason"]
    # A session that passes what it fetched (started before T286) is still read when the hook
    # kept nothing; one that passes it although the hook kept it reads the hook's.
    server.call("routine_board", {})
    out = server.call("routine_run", {**run, "fetched": listed(reply_at)})
    assert out["next"]["arguments"]["entityUrls"][0].endswith(
        f"/{teams_id(reply_at)}?$select=id,from,body"
    )
    fetched_by_hook(h, routine, out["next"]["arguments"], bodies((teams_id(reply_at), "T001 好")))
    done = server.call("routine_run", {**run, "fetched": "copied wrong"})
    assert [e["data"]["text"] for e in done["events"]] == ["T001 好"]
    assert "48:notes" not in routine_mod.load_cache(h.home)["fetch_failures"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_fetch_kept_in_a_broken_off_run_is_not_used_later(home: Home) -> None:
    # Review F1 of T286: bodies kept by the hook but not taken (the run broke off) must not stand
    # in for the same fetch in the next run when that one fails without reaching the hook.
    h = Harness(home, TEAMS)
    routine, _, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    reply_at = "2026-11-05T00:12:00.000Z"

    def listed_to_bodies() -> dict[str, Any]:
        teams = next(
            j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
        )
        fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(reply_at))
        return server.call("routine_run", run)["next"]

    step = listed_to_bodies()
    fetched_by_hook(h, routine, step["arguments"], bodies((teams_id(reply_at), "T001 用 A")))
    # The run breaks off here. The next one lists again; the bodies fetch fails this time.
    assert listed_to_bodies()["arguments"] == step["arguments"]
    out = server.call("routine_run", {**run, "fetched": "Error: HTTP 429 Too Many Requests"})
    assert out["events"] == [] and out["forward"] == {"send": False}
    failures = routine_mod.load_cache(h.home)["fetch_failures"]["48:notes"]
    assert failures["count"] == 1 and "429" in failures["reason"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_later_page_that_keeps_failing_is_reported(home: Home) -> None:
    # Review F3 of T286: the first page reads fine every run, the second fails every run. Only a
    # row read to its end ends the run of failures, so the orchestrator is told.
    h = Harness(home, TEAMS)
    routine, _, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    times = [f"2026-11-05T00:2{m}:00.000Z" for m in range(9, -1, -1)]
    throttled = {"results": [{"data": {"error": {"code": "TooMany"}}, "statusCode": 429}]}
    forwards = []
    for n in range(routine_mod.TEAMS_FETCH_ALERT_AFTER):
        teams = next(
            j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
        )
        fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(*times))
        older = server.call("routine_run", run)["next"]
        if n == 0:
            # The first run reads the notes' bodies; later runs go straight to the page before,
            # the notes having been judged (T293). Either way the run of failures goes on.
            notes = bodies(*((teams_id(t), "笔记") for t in times))
            fetched_by_hook(h, routine, older["arguments"], notes)
            older = server.call("routine_run", run)["next"]
        assert older["arguments"]["entityUrls"][0].endswith("lt+2026-11-05T00:20:00.000Z")
        fetched_by_hook(h, routine, older["arguments"], throttled)
        out = server.call("routine_run", run)
        assert "next" not in out
        forwards.append(out["forward"])
    assert [f["send"] for f in forwards] == [False] * (routine_mod.TEAMS_FETCH_ALERT_AFTER - 1) + [
        True
    ]
    assert "HTTP 429" in forwards[-1]["message"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_teams_step_retried_with_its_command_id_returns_the_first_result(home: Home) -> None:
    # Review F5 of T286: the response to the bodies step is lost and the session retries with
    # the same command_id. The fetch the hook kept is gone by then; the first result comes back.
    h = Harness(home, TEAMS)
    routine, request_id, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    reply_at = "2026-11-05T00:12:00.000Z"
    teams = next(
        j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
    )
    fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(reply_at))
    step = server.call("routine_run", run)["next"]
    fetched_by_hook(h, routine, step["arguments"], bodies((teams_id(reply_at), "T001 用 A")))
    first = server.call("routine_run", {**run, "command_id": "bodies-1"})
    assert [e["data"]["request_id"] for e in first["events"]] == [request_id]
    again = server.call("routine_run", {**run, "command_id": "bodies-1"})
    assert again["duplicate"] is True
    assert [e["sequence"] for e in again["events"]] == [e["sequence"] for e in first["events"]]
    assert again.get("next") == first.get("next")
    assert "48:notes" not in routine_mod.load_cache(h.home).get("fetch_failures", {})
    # A listing records nothing in the ledger (review F6): its retry gives the same next step,
    # takes no new fetch and counts nothing.
    teams = next(
        j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
    )
    later = "2026-11-05T00:13:00.000Z"
    fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(later, reply_at))
    listing = server.call("routine_run", {**run, "command_id": "list-1"})
    assert listing["events"] == [] and listing["next"]["arguments"]["entityUrls"] == [
        routine_mod.full_text_path("48:notes", teams_id(later))
    ]
    fetched_by_hook(h, routine, listing["next"]["arguments"], bodies((teams_id(later), "笔记")))
    retried = server.call("routine_run", {**run, "command_id": "list-1"})
    assert retried["events"] == [] and retried["next"] == listing["next"]
    assert "48:notes" not in routine_mod.load_cache(h.home).get("fetch_failures", {})
    # The same command_id with another request is a conflict, not a retry (review F8).
    other = {**run, "command_id": "list-1", "fetched": listed(later)}
    assert reason_of(lambda: server.call("routine_run", other)) == "command_conflict"
    # The bodies the hook kept are still there for the step itself.
    assert server.call("routine_run", run)["events"] == []
    assert "48:notes" not in routine_mod.load_cache(h.home).get("fetch_failures", {})


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_failed_teams_step_retried_stays_failed(home: Home) -> None:
    # Review F7 of T286: a bodies step that failed its check (one candidate's body missing)
    # must not record the reply it did hold when it is retried with its command_id.
    h = Harness(home, TEAMS)
    routine, _, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    times = ["2026-11-05T00:13:00.000Z", "2026-11-05T00:12:00.000Z"]
    teams = next(
        j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
    )
    fetched_by_hook(h, routine, teams["fetch"]["arguments"], listed(*times))
    server.call("routine_run", run)
    partial = bodies((teams_id(times[1]), "T001 yes"))
    attempt = {**run, "command_id": "bodies-x", "fetched": partial}
    first = server.call("routine_run", attempt)
    assert first["events"] == [] and "next" not in first
    count = routine_mod.load_cache(h.home)["fetch_failures"]["48:notes"]["count"]
    again = server.call("routine_run", attempt)
    assert again["events"] == [] and "next" not in again
    assert routine_mod.load_cache(h.home)["fetch_failures"]["48:notes"]["count"] == count


@pytest.mark.use_case("UC-ROUTINE-001")
def test_only_the_routine_sessions_fetches_are_kept(home: Home) -> None:
    h = Harness(home, TEAMS)
    routine = start_routine(h, "r1")
    arguments = {"entityUrls": ["/me/chats/48:notes/messages?$top=1"]}
    response = [{"type": "text", "text": '{"results": []}'}]
    fields = {"tool_name": "mcp__workiq__fetch", "tool_input": arguments, "tool_response": response}
    # The orchestrator reading a message in full, or another tool: nothing kept, nothing said.
    assert hook(h, "orch-1", "PostToolUse", h.home.root, **fields) is None
    other = {**fields, "tool_name": "mcp__workiq__retrieve"}
    assert hook(h, "r1", "PostToolUse", h.home.root / "routine", **other) is None
    assert routine_mod.take_fetch(h.home, arguments) == (False, None)
    told = hook(h, "r1", "PostToolUse", h.home.root / "routine", **fields)
    assert "不用传 fetched" in told["hookSpecificOutput"]["additionalContext"]
    # Taken once; the same paths spelled with %2c are the same fetch.
    spelled = {"entityUrls": ["/me/chats/48%3anotes/messages?$top=1"]}
    assert routine_mod.take_fetch(h.home, spelled) == (True, '{"results": []}')
    assert routine_mod.take_fetch(h.home, arguments) == (False, None)
    # Only the latest few are kept.
    for n in range(routine_mod.CAPTURES_KEPT + 2):
        args = {"entityUrls": [f"/p/{n}"]}
        assert routine_mod.record_fetch(h.home, args, response, h.clock.now)
    assert routine_mod.take_fetch(h.home, {"entityUrls": ["/p/0"]}) == (False, None)
    assert routine_mod.take_fetch(h.home, {"entityUrls": ["/p/9"]})[0] is True
    assert routine_mod.record_fetch(h.home, {"entityUrls": []}, response, h.clock.now) is False
    del routine


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
    assert hook(h, "r1", "PreToolUse", cwd, tool_name="mcp__workiq__fetch") is None
    # A routine turn ends with nothing in the ledger and nothing to remind; before the first
    # board read there is no cache to add to.
    assert hook(h, "r1", "Stop", cwd) is None
    cache_file = home.cache_dir / "routine.json"
    assert not cache_file.exists()
    # Once the board has run, the end of each turn joins the run times (live evidence, T250).
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"generation": 1, "runs": ["t0"]}), encoding="utf-8")
    assert hook(h, "r1", "Stop", cwd) is None
    ends = json.loads(cache_file.read_text(encoding="utf-8"))["ends"]
    assert len(ends) == 1 and ends[0] > "2026"
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


@pytest.mark.use_case("UC-ROUTINE-001")
def test_an_attached_routine_session_is_detached(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    # Bound but not detached (it was started before T250, or the detach was not recorded): every
    # turn it ends wakes the orchestrator, so the orchestrator detaches it first.
    routine = start_routine(h, detach=False)
    cache: dict[str, Any] = {}
    board(h, routine, cache)
    assert attention(h, cache) == [
        {
            "state": "attached",
            "generation": 1,
            "session_name": "routine-g1",
            "session_id": "routine-1",
            "armed_at": None,
            "last_run": h.clock.now,
        }
    ]
    # The reminder comes in any turn, not only the user's: detaching creates no session.
    injected = '<cross-session-message from="w">x</cross-session-message>'
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt=injected)
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block"
    assert "detach_session" in stop["reason"] and "routine_detached" in stop["reason"]
    assert "routine-1" in stop["reason"] and "generation: 1" in stop["reason"]
    # Only the generation in charge, and only once.
    assert reason_of(lambda: h.run("routine_detached", generation=2)) == "invalid_generation"
    h.run("routine_detached", generation=1, note="已由主会话在 T250 实测中分离")
    assert h.query("SELECT generation, note FROM routine_detached") == [
        {"generation": 1, "note": "已由主会话在 T250 实测中分离"}
    ]
    assert reason_of(lambda: h.run("routine_detached", generation=1)) == "already_detached"
    assert attention(h, cache)[0]["state"] == "unarmed"
    # Only the orchestrator records it.
    assert reason_of(lambda: h.run("routine_detached", routine, generation=1)) == "role_denied"
    # A new generation starts attached again until it is detached in turn.
    start_routine(h, "routine-2", detach=False)
    assert attention(h, {})[0]["state"] == "attached"
    h.run("routine_detached", generation=2)
    assert attention(h, {})[0]["state"] != "attached"


@pytest.mark.use_case("UC-ROUTINE-001")
def test_no_detach_where_the_host_does_not_wake_the_starter(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = routine_mod.SESSIONS["claude-app"]
    monkeypatch.setitem(routine_mod.SESSIONS, "claude-app", dataclasses.replace(caps, detach=False))
    h = Harness(home)
    out = h.run("routine_start", user_turn=True)
    assert out["events"][0]["data"]["detach"] is False
    h.run("routine_bind", generation=1, session_id="routine-1")
    with h.ledger.view() as conn:
        assert routine_mod.status(conn, h.config, h.clock.now, {})["state"] == "unarmed"


def listed_changed(*messages: tuple[str, str]) -> dict[str, Any]:
    """A listed page whose messages carry their last change: (created, last modified)."""
    page = listed(*(t for t, _ in messages))
    for item, (_, modified) in zip(page["results"][0]["data"]["value"], messages, strict=True):
        item["lastModifiedDateTime"] = modified
    return page


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_note_is_read_once_while_a_question_waits(home: Home) -> None:
    # T293: the read window does not move while a question waits, and every note the user wrote
    # in the self chat was read in full in every run. Read once, a note is not read again until
    # it changes or a new question could make it an answer; the window moving on drops it.
    h = Harness(home, TEAMS)
    routine, request_id, _, server = teams_waiting(h)
    run = {"job_id": "teams-replies", "chat_id": "48:notes"}
    note_a, note_b, reply_at = (
        "2026-11-05T00:11:00.000Z",
        "2026-11-05T00:12:00.000Z",
        "2026-11-05T00:13:00.000Z",
    )
    card_at = "2026-11-05T00:09:59.500Z"

    def read(page: dict[str, Any], texts: dict[str, str] | None = None) -> dict[str, Any]:
        teams = next(
            j for j in server.call("routine_board", {})["jobs"] if j["job_id"] == "teams-replies"
        )
        fetched_by_hook(h, routine, teams["fetch"]["arguments"], page)
        out = server.call("routine_run", run)
        if texts is None:
            return out
        urls = out["next"]["arguments"]["entityUrls"]
        asked_for = [u.split("/messages/")[1].split("?")[0] for u in urls]
        assert sorted(asked_for) == sorted(texts)
        fetched_by_hook(h, routine, out["next"]["arguments"], bodies(*texts.items()))
        return server.call("routine_run", run)

    unchanged = [(t, t) for t in (reply_at, note_b, note_a, card_at)]
    first = read(
        listed_changed(*unchanged),
        {teams_id(reply_at): "T001 用 B", teams_id(note_b): "笔记 b", teams_id(note_a): "笔记 a"},
    )
    assert [e["data"]["message_id"] for e in first["events"]] == [teams_id(reply_at)]
    assert "next" not in first
    cache = routine_mod.load_cache(h.home)
    assert cache["teams_checked"]["48:notes"]["messages"] == {
        teams_id(note_a): {"at": "2026-11-05T00:11:00Z", "modified": note_a},
        teams_id(note_b): {"at": "2026-11-05T00:12:00Z", "modified": note_b},
    }
    assert "48:notes" not in cache.get("fetch_failures", {})
    # The next run reads no body: the reply was passed on, the notes were judged.
    again = read(listed_changed(*unchanged))
    assert again["events"] == [] and "next" not in again
    # The user edits note a into an answer: read again and passed on.
    edited = [
        (reply_at, reply_at),
        (note_b, note_b),
        (note_a, "2026-11-05T00:30:00.000Z"),
        unchanged[3],
    ]
    out = read(listed_changed(*edited), {teams_id(note_a): "T001 改用 C"})
    assert [e["data"]["text"] for e in out["events"]] == ["T001 改用 C"]
    judged = routine_mod.load_cache(h.home)["teams_checked"]["48:notes"]["messages"]
    assert set(judged) == {teams_id(note_b)}  # passed on now: left to the ledger (PR #55)
    # A second task asks in the same chat: note b could answer it now, so it is read once more.
    h.clock.now = "2026-11-05T00:40:00Z"
    _, second = asked(h, "T002")
    h.clock.now = "2026-11-05T01:00:00Z"
    run_job(h, routine, "handoff-scan")
    h.run("input_relay", task_id="T002", request_id=second, channel="teams-chat")
    card2_at = "2026-11-05T00:59:59.500Z"
    h.run(
        "channel_sent",
        task_id="T002",
        request_id=second,
        chat_id="48:notes",
        message_id=teams_id(card2_at),
        sender_id="u1",
    )
    out = read(listed_changed((card2_at, card2_at), *edited), {teams_id(note_b): "笔记 b"})
    assert out["events"] == []
    basis = routine_mod.load_cache(h.home)["teams_checked"]["48:notes"]["basis"]
    assert basis == {"requests": sorted([request_id, second]), "sender_id": "u1"}
    # T001's question is answered: the window starts at T002's card, and what came before it is
    # dropped at the next run's start.
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request_id,
        answer="用 C",
        user_source={
            "channel": "teams-chat",
            "principal": "teams",
            "message_ref": teams_id(note_a),
            "text": "T001 改用 C",
            "request_id": request_id,
            "target": ["T001", 1, "answer"],
        },
    )
    server.call("routine_board", {})
    assert routine_mod.load_cache(h.home)["teams_checked"]["48:notes"]["messages"] == {}


def switch_off(h: Harness) -> None:
    """The user switches the routine session off (routine.enabled=false)."""
    path = h.home.path("config/task-steward.json")
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["routine"] = {**settings.get("routine", {}), "enabled": False}
    path.write_text(json.dumps(settings), encoding="utf-8")
    h.config = load_config(h.home).require_valid()


@pytest.mark.use_case("UC-ROUTINE-002")
def test_a_switched_off_routine_session_deletes_its_timer_then_is_archived(home: Home) -> None:
    # T345: the Claude app refuses to archive a session that still holds a recurring CronCreate
    # task ("still has live work"), and lets it through once the task is deleted.
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    h.dispatch("T001")
    routine = start_routine(h)
    cache: dict[str, Any] = {}
    board(h, routine, cache)
    h.run("routine_armed", routine, job_ref="job-1", cron="*/10 * * * *", interval_minutes=10)
    switch_off(h)
    retiring = {
        "state": "retiring",
        "step": "disarm",
        "generation": 1,
        "session_name": "routine-g1",
        "session_id": "routine-1",
    }
    assert attention(h, cache) == [retiring]
    # The orchestrator is told in any turn to wake it: messaging creates no session.
    injected = '<cross-session-message from="w">x</cross-session-message>'
    hook(h, "orch-1", "UserPromptSubmit", home.root, prompt=injected)
    stop = hook(h, "orch-1", "Stop", home.root)
    assert stop["decision"] == "block" and "运行 routine_board" in stop["reason"]
    assert "routine-1" in stop["reason"]
    # The board lists no jobs, only the timer to delete, with how to do it.
    out = board(h, routine, cache)
    assert out["enabled"] is False and out["jobs"] == []
    assert out["orchestrator_session"] == "orch-1"
    [disarm] = out["maintenance"]
    assert disarm["action"] == "disarm" and disarm["job_ref"] == "job-1"
    assert "CronDelete" in disarm["do"] and "routine_disarmed" in disarm["do"]
    # Only the timer tsw recorded, only by the routine session in charge.
    assert reason_of(lambda: h.run("routine_disarmed", routine, job_ref="job-9")) == "job_mismatch"
    assert reason_of(lambda: h.run("routine_disarmed", job_ref="job-1")) == "role_denied"
    server = Server(h.home, {SESSION_ENV: "routine-1"}, home.root / "routine")
    done = server.call("routine_disarmed", {"job_ref": "job-1", "result": "Cancelled job job-1."})
    assert done["events"][0]["type"] == "routine.disarmed"
    assert done["events"][0]["data"] == {
        "generation": 1,
        "job_ref": "job-1",
        "result": "Cancelled job job-1.",
    }
    forward = done["forward"]
    assert forward["send"] is True and forward["session_id"] == "orch-1"
    assert "archive_session(session_id=routine-1)" in forward["message"]
    assert "routine_archived" in forward["message"] and "generation: 1" in forward["message"]
    assert h.query("SELECT armed_ref, armed_at FROM routine_sessions") == [
        {"armed_ref": None, "armed_at": None}
    ]
    assert reason_of(lambda: h.run("routine_disarmed", routine, job_ref="job-1")) == "not_armed"
    # Nothing left on the board; the orchestrator archives it next.
    again = board(h, routine, cache)
    assert again["maintenance"] == [] and again["jobs"] == []
    assert attention(h, cache) == [{**retiring, "step": "archive"}]
    stop = hook(h, "orch-1", "Stop", home.root)
    assert "archive_session(session_id=routine-1" in stop["reason"]
    assert "routine_archived" in stop["reason"]
    assert reason_of(lambda: h.run("routine_archived", generation=2)) == "invalid_generation"
    assert reason_of(lambda: h.run("routine_archived", routine, generation=1)) == "role_denied"
    archived = h.run("routine_archived", generation=1, result="Archived session routine-1")
    assert archived["events"][0]["data"] == {
        "generation": 1,
        "session_ref": "routine-1",
        "result": "Archived session routine-1",
    }
    assert attention(h, cache) == []
    assert hook(h, "orch-1", "Stop", home.root) is None
    with h.ledger.view() as conn:
        who = resolve(conn, home, {SESSION_ENV: "routine-1"}, home.root / "routine")
    assert who.role == "revoked" and who.phase == "archived"
    # Switched on again: no routine session in charge, so the next user turn starts one.
    settings_path = h.home.path("config/task-steward.json")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["routine"]["enabled"] = True
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    h.config = load_config(h.home).require_valid()
    assert attention(h, cache) == [{"state": "missing"}]


@pytest.mark.use_case("UC-ROUTINE-002")
def test_switched_on_again_before_it_was_archived_it_arms_again(home: Home) -> None:
    h = Harness(home)
    routine = start_routine(h)
    cache: dict[str, Any] = {}
    board(h, routine, cache)
    h.run("routine_armed", routine, job_ref="job-1", cron="*/10 * * * *", interval_minutes=10)
    switch_off(h)
    board(h, routine, cache)
    h.run("routine_disarmed", routine, job_ref="job-1", result="No job with id job-1")
    path = h.home.path("config/task-steward.json")
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["routine"]["enabled"] = True
    path.write_text(json.dumps(settings), encoding="utf-8")
    h.config = load_config(h.home).require_valid()
    out = board(h, routine, cache)
    assert [m["action"] for m in out["maintenance"]] == ["arm"]
    assert out["maintenance"][0]["replace"] is None
    assert [j["job_id"] for j in out["jobs"]] == ["handoff-scan", "session-scan"]


@pytest.mark.use_case("UC-ROUTINE-002")
def test_switched_off_without_a_timer_or_a_session(home: Home) -> None:
    h = Harness(home)
    routine = start_routine(h)
    switch_off(h)
    # Bound but never armed: nothing to delete, archive it straight away.
    assert attention(h)[0]["step"] == "archive"
    out = board(h, routine, {})
    assert out["maintenance"] == [] and out["jobs"] == []
    # The user archived it from the sidebar: recorded the same way, with a note.
    h.run("routine_archived", generation=1, note="用户在侧栏归档，get_session 读回 isArchived")
    assert attention(h) == []  # nothing bound any more: nothing to retire
    with h.ledger.view() as conn:
        assert routine_mod.status(conn, h.config, h.clock.now, {}) == {"state": "disabled"}


@pytest.mark.use_case("UC-ROUTINE-002")
def test_the_routine_session_is_told_how_to_disarm(home: Home) -> None:
    h = Harness(home)
    start_routine(h, "r1")
    start = hook(h, "r1", "SessionStart", home.root / "routine")
    text = start["hookSpecificOutput"]["additionalContext"]
    assert "disarm" in text and "routine_disarmed" in text
