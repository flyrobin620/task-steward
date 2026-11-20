"""Reading Teams replies in two steps through the real hook and MCP server processes (T286):
the routine session's PostToolUse hook keeps what it fetched, ``routine_run`` takes it from there.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tests.support import child_env
from tests.surface.test_host_surface import INIT, NOTE, PLUGIN, call, launch, mcp, payload
from tsw import lease
from tsw.home import Home
from tsw.machine.base import Caller

ROUTINE = "r-1"


def teams_id(at: str) -> str:
    return str(int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000))


def hook(cwd: Path, session: str, event: str, **fields: Any) -> Any:
    proc = launch(
        PLUGIN / "bin" / "tsw_launch.py",
        "guard",
        event,
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        cwd=cwd,
        stdin=json.dumps({"cwd": str(cwd), "hook_event_name": event, **fields}),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def routine_call(cwd: Path, name: str, **arguments: Any) -> Any:
    replies = mcp(cwd.parent, cwd, ROUTINE, INIT, NOTE, call(1, name, **arguments))
    reply = next(r for r in replies if r.get("id") == 1)
    assert "error" not in reply, reply
    return payload(reply)


def fetched(cwd: Path, arguments: Any, result: Any) -> Any:
    """What the host sends the hook after the routine session's fetch: content blocks."""
    text = json.dumps(result, ensure_ascii=False)
    return hook(
        cwd,
        ROUTINE,
        "PostToolUse",
        tool_name="mcp__workiq__fetch",
        tool_input=arguments,
        tool_response=[{"type": "text", "text": text}],
    )


@pytest.fixture
def waiting(home: Home) -> tuple[Harness, str, str]:
    """A question relayed to the user's self chat in Teams, and a bound routine session."""
    h = Harness(
        home,
        {
            "plugins": {"human_channels": ["in-session", "teams-chat"]},
            "routine": {"teams_message_chars": 100},
        },
    )
    out = h.run("routine_start", user_turn=True)
    generation = out["events"][0]["data"]["generation"]
    h.run("routine_bind", generation=generation, session_id=ROUTINE)
    h.run("routine_detached", generation=generation)
    h.register("T001")
    h.plan("T001")
    worker = h.dispatch("T001")
    asked = h.run("ask_user", worker, task_id="T001", question="用方案 A 还是 B？")
    request_id = asked["events"][0]["data"]["request_id"]
    h.clock.now = "2026-11-05T00:10:00Z"
    h.run(
        "routine_run",
        Caller(role="routine", session_id=ROUTINE, machine=MACHINE),
        job_id="handoff-scan",
    )
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
    # The server and hook processes run on this machine, the harness's lease on its own.
    h.ledger.commit(lease.release("orch-1", machine=MACHINE, actor=ORCH.actor()))
    here = lease.this_machine()
    h.ledger.commit(
        lease.acquire("orch-1", machine=here, lease_minutes=1_000_000, actor=ORCH.actor())
    )
    return h, request_id, card


@pytest.mark.use_case("UC-CHANNEL-001")
def test_the_hook_hands_each_fetch_to_routine_run(waiting: tuple[Harness, str, str]) -> None:
    h, request_id, card = waiting
    cwd = h.home.root / "routine"
    board = routine_call(cwd, "routine_board")
    teams = next(j for j in board["jobs"] if j["job_id"] == "teams-replies")
    run = teams["run"]
    reply_at, long_at = "2026-11-05T00:12:00.000Z", "2026-11-05T00:13:00.000Z"
    note_at = "2026-11-05T00:11:00.000Z"
    page = {
        "results": [
            {
                "data": {
                    "@odata.count": 4,
                    "value": [
                        {"id": teams_id(t), "createdDateTime": t, "lastModifiedDateTime": t}
                        for t in (long_at, reply_at, note_at, "2026-11-05T00:09:59.500Z")
                    ],
                },
                "statusCode": 200,
            }
        ]
    }
    told = fetched(cwd, teams["fetch"]["arguments"], page)
    assert "不用传 fetched" in told["hookSpecificOutput"]["additionalContext"]
    first = routine_call(cwd, "routine_run", **run)
    urls = first["next"]["arguments"]["entityUrls"]
    assert urls == [
        f"/me/chats/48:notes/messages/{teams_id(t)}?$select=id,from,body"
        for t in (long_at, reply_at, note_at)
    ]
    assert card not in "".join(urls)
    long_reply = "T001 " + "第二个方案更好，" * 40
    bodies = {
        "results": [
            {
                "data": {
                    "id": teams_id(t),
                    "from": {"user": {"id": "u1"}},
                    "body": {"contentType": "html", "content": f"<p>{text}</p>"},
                },
                "statusCode": 200,
            }
            for t, text in ((long_at, long_reply), (reply_at, "T001 用 B"), (note_at, "笔记"))
        ]
    }
    fetched(cwd, first["next"]["arguments"], bodies)
    second = routine_call(cwd, "routine_run", **run)
    assert "next" not in second  # fewer than a page: the chat has nothing older
    seen = {e["data"]["message_id"]: e["data"] for e in second["events"]}
    assert seen[teams_id(reply_at)]["text"] == "T001 用 B"
    assert seen[teams_id(long_at)]["text"] == long_reply[:100]
    assert seen[teams_id(long_at)]["truncated"] is True
    assert seen[teams_id(long_at)]["request_id"] == request_id
    forward = second["forward"]
    assert forward["send"] is True and "正文已截断" in forward["message"]
    assert teams_id(note_at) not in seen
    # The next run lists the same page and reads no body: the replies were passed on, the note
    # was judged not to be one (T293).
    board = routine_call(cwd, "routine_board")
    teams = next(j for j in board["jobs"] if j["job_id"] == "teams-replies")
    fetched(cwd, teams["fetch"]["arguments"], page)
    third = routine_call(cwd, "routine_run", **run)
    assert third["events"] == [] and "next" not in third
    assert third["forward"] == {"send": False}
