"""The routine session through the real MCP server and hook processes (DN006 §8.9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json
from tests.surface.test_host_surface import INIT, LIST, NOTE, PLUGIN, call, launch, mcp, payload


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
        json.dumps(
            {
                "version": 1,
                "repositories": {"demo": {"path": "~/src/demo"}},
                "plugins": {"human_channels": ["in-session", "teams-chat"]},
            }
        ),
        encoding="utf-8",
    )
    return root


def by_id(replies: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {r.get("id"): r for r in replies}


def hook(home: Path, session: str, event: str, cwd: Path, **fields: Any) -> Any:
    proc = launch(
        PLUGIN / "bin" / "tsw_launch.py",
        "guard",
        event,
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        cwd=cwd,
        stdin=json.dumps({"cwd": str(cwd), **fields}),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


@pytest.mark.use_case("UC-ROUTINE-001")
@pytest.mark.use_case("UC-CHANNEL-001")
def test_the_routine_session_end_to_end(home: Path) -> None:
    replies = by_id(
        mcp(
            home,
            home,
            "orch-1",
            INIT,
            NOTE,
            call(1, "lease_acquire"),
            call(2, "routine_start", user_turn=True),
        )
    )
    started = payload(replies[2])
    cwd = Path(started["cwd"])
    assert cwd.is_dir() and cwd.name == "routine"
    data = started["events"][0]["data"]
    assert data["session_name"] == "routine-g1" and data["detach"] is True
    # Before the binding the new session already sees the routine tools.
    pending = by_id(mcp(home, cwd, "r-1", INIT, NOTE, LIST, call(2, "routine_board")))
    names = {t["name"] for t in pending[1]["result"]["tools"]}
    assert {"routine_board", "routine_run", "routine_armed"} <= names
    assert "task_register" not in names and "routine_start" not in names
    assert "not_bound" in pending[2]["result"]["content"][0]["text"]
    bound = by_id(
        mcp(
            home,
            home,
            "orch-1",
            INIT,
            NOTE,
            call(3, "routine_bind", generation=1, session_id="r-1"),
            call(4, "routine_detached", generation=1),
        )
    )
    assert "error" not in bound[3]
    assert payload(bound[4])["events"][0]["type"] == "routine.detached"
    run = by_id(
        mcp(
            home,
            cwd,
            "r-1",
            INIT,
            NOTE,
            call(1, "whoami"),
            call(2, "routine_board"),
            call(3, "routine_armed", job_ref="job-1", cron="*/10 * * * *", interval_minutes=10),
            call(4, "routine_run", job_id="handoff-scan"),
            call(5, "routine_board"),
        )
    )
    assert payload(run[1])["role"] == "routine"
    first = payload(run[2])
    assert [m["action"] for m in first["maintenance"]] == ["arm"]
    assert first["orchestrator_session"] == "orch-1"
    assert payload(run[4])["forward"] == {"send": False}
    assert payload(run[5])["maintenance"] == []
    assert json.loads((home / "cache" / "routine.json").read_text(encoding="utf-8"))["generation"]
    # The hooks keep the routine session to its board (CC-B11) and its timer tagged (CC-B8).
    denied = hook(home, "r-1", "PreToolUse", cwd, tool_name="Bash", tool_input={"command": "ls"})
    assert denied["hookSpecificOutput"]["permissionDecisionReason"].startswith("CC-B11")
    start = hook(home, "r-1", "SessionStart", cwd)
    assert "routine_board" in start["hookSpecificOutput"]["additionalContext"]
    # A Teams card can only be registered for a question relayed to Teams.
    sent = by_id(
        mcp(
            home,
            home,
            "orch-1",
            INIT,
            NOTE,
            call(
                1,
                "channel_sent",
                task_id="T001",
                request_id="Q-T001-1",
                chat_id="c",
                message_id="m",
            ),
        )
    )
    assert "request_not_found" in sent[1]["result"]["content"][0]["text"]


@pytest.mark.use_case("UC-ROUTINE-002")
def test_a_switched_off_routine_session_is_disarmed_then_archived(home: Path) -> None:
    started = payload(
        by_id(
            mcp(
                home,
                home,
                "orch-1",
                INIT,
                NOTE,
                call(1, "lease_acquire"),
                call(2, "routine_start", user_turn=True),
                call(3, "routine_bind", generation=1, session_id="r-1"),
                call(4, "routine_detached", generation=1),
            )
        )[2]
    )
    cwd = Path(started["cwd"])
    armed = by_id(
        mcp(
            home,
            cwd,
            "r-1",
            INIT,
            NOTE,
            LIST,
            call(2, "routine_armed", job_ref="job-1", cron="*/10 * * * *", interval_minutes=10),
        )
    )
    assert "routine_disarmed" in {t["name"] for t in armed[1]["result"]["tools"]}
    config = home / "config" / "task-steward.json"
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["routine"] = {"enabled": False}
    config.write_text(json.dumps(settings), encoding="utf-8")
    # The orchestrator's Stop hook says to wake it, in any turn.
    stop = hook(home, "orch-1", "Stop", home)
    assert stop["decision"] == "block" and "运行 routine_board" in stop["reason"]
    run = by_id(
        mcp(
            home,
            cwd,
            "r-1",
            INIT,
            NOTE,
            call(1, "routine_board"),
            call(2, "routine_disarmed", job_ref="job-1", result="Cancelled job job-1."),
        )
    )
    out = payload(run[1])
    assert out["jobs"] == [] and [m["action"] for m in out["maintenance"]] == ["disarm"]
    forward = payload(run[2])["forward"]
    assert forward["send"] is True and forward["session_id"] == "orch-1"
    assert "archive_session(session_id=r-1)" in forward["message"]
    done = by_id(
        mcp(
            home,
            home,
            "orch-1",
            INIT,
            NOTE,
            LIST,
            call(2, "attention"),
            call(3, "routine_archived", generation=1, result="Archived session r-1"),
            call(4, "attention"),
        )
    )
    assert "routine_archived" in {t["name"] for t in done[1]["result"]["tools"]}
    assert payload(done[2])["routine"][0]["step"] == "archive"
    assert payload(done[3])["events"][0]["type"] == "routine.archived"
    assert payload(done[4])["routine"] == []
