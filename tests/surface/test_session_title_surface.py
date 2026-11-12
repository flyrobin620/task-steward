"""Worker session titles through the real surfaces (DN002 §5.8): ``tsw tool run`` hands out the
title, the orchestrator's MCP server over stdio lists and records what is due, and the Stop hook
process reminds of it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json
from tests.surface.test_follow_up_surface import mcp, result
from tests.surface.test_requirement_change_surface import INIT, LIST, MACHINE, ORCH, PLUGIN, ok

pytestmark = pytest.mark.use_case("UC-SESSION-001")
BASE = "T001 短标题"


def call(i: int, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def stop(home: Path) -> dict[str, Any] | None:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PLUGIN), "guard", "Stop"],
        input=json.dumps({"cwd": str(home), "hook_event_name": "Stop"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-1"),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


@pytest.fixture
def delivered(tmp_path: Path) -> Path:
    """T001 prestarted with a short title, then delivered: its standby mark is due to go."""
    code, out = run_json("init")
    assert code == 0
    home = Path(out["home"])
    (home / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    assert run_json("lease", "acquire", "--session", "orch-1", "--machine", MACHINE)[0] == 0
    ok(
        "task_register",
        ORCH,
        task_id="T001",
        title="t",
        repo="demo",
        kind="code",
        goal="g",
        acceptance=["a"],
    )
    ok("task_plan", ORCH, task_id="T001", reason="无依赖")
    begun = ok(
        "attempt_start",
        ORCH,
        task_id="T001",
        purpose="prestart",
        user_turn=True,
        session_title=BASE,
    )
    assert begun["session_title"] == f"{BASE}（待命）"
    ok(
        "attempt_reconcile",
        ORCH,
        task_id="T001",
        attempt="a1",
        generation=begun["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
    )
    ok("task_dispatch", ORCH, task_id="T001", workdir="work/T001/a1")
    ok("attempt_deliver", ORCH, task_id="T001")
    return home


def test_the_orchestrator_renames_through_its_mcp_server(delivered: Path) -> None:
    home = delivered
    reminder = stop(home)
    assert reminder is not None and reminder["decision"] == "block"
    assert (
        "attention 的 sessions" in reminder["reason"] and "T001 a1（改标题）" in reminder["reason"]
    )
    session = {"sessionId": "w-1", "title": f"{BASE}（待命）", "isArchived": False}
    replies = mcp(
        home,
        "orch-1",
        INIT,
        LIST,
        call(2, "attention"),
        call(3, "session_check", task_id="T001", attempt="a1", observed=json.dumps(session)),
    )
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"session_check", "session_record"} <= names
    [item] = result(replies[2])["sessions"]
    assert (item["action"], item["title"], item["state"]) == ("title", BASE, "due")
    check = result(replies[3])
    assert (check["action"], check["session_id"], check["title"]) == ("rename", "w-1", BASE)
    record = mcp(
        home,
        "orch-1",
        INIT,
        call(
            1,
            "session_record",
            task_id="T001",
            attempt="a1",
            action="title",
            title=BASE,
            ok=True,
            result=f'Renamed session w-1 to "{BASE}" (was "{BASE}（待命）").',
        ),
        call(2, "attention"),
    )
    event = result(record[1])["events"][0]
    assert (event["type"], event["data"]["outcome"]) == ("attempt.session_updated", "renamed")
    assert result(record[2])["sessions"] == []
    assert stop(home) is None


def test_a_user_title_is_recorded_and_left_alone(delivered: Path) -> None:
    home = delivered
    session = {"sessionId": "w-1", "title": "用户自己起的名字", "isArchived": False}
    replies = mcp(
        home,
        "orch-1",
        INIT,
        call(1, "session_check", task_id="T001", attempt="a1", observed=session),
        call(2, "attention"),
    )
    check = result(replies[1])
    assert (check["action"], check["outcome"]) == ("none", "user_title")
    assert result(replies[2])["sessions"] == []
    assert stop(home) is None
