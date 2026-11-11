"""A requirement change the user makes in the worker's session, through the real hook process and
the worker's MCP server over stdio (DN002 §5.1a)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import REPO_ROOT, child_env, run_json

MACHINE = socket.gethostname()  # the hook and MCP processes record on this machine
ORCH = ["--role", "orchestrator", "--session", "orch-1", "--machine", MACHINE]
PLUGIN = REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"

pytestmark = pytest.mark.use_case("UC-CHANGE-001")


def ok(name: str, caller: list[str], **inp: Any) -> dict[str, Any]:
    code, out = run_json(
        "tool", "run", name, "--input", json.dumps(inp, ensure_ascii=False), *caller
    )
    assert code == 0, out
    return out


def hook(session: str, home: Path, prompt: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PLUGIN), "guard", "UserPromptSubmit"],
        input=json.dumps({"cwd": str(home), "prompt": prompt}, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def mcp(cwd: Path, session: str, *messages: Any) -> dict[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PLUGIN), "mcp"],
        input="".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        cwd=cwd,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return {int(r["id"]): r for r in replies if "id" in r}


def call(i: int, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18"},
}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def started(tmp_path: Path) -> tuple[Path, Path]:
    """T001 in progress with worker session w-1 in its own directory."""
    code, out = run_json("init")
    assert code == 0
    home = Path(out["home"])
    (home / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    assert run_json("lease", "acquire", "--session", "orch-1", "--machine", MACHINE)[0] == 0
    workdir = tmp_path / "wt"
    workdir.mkdir()
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
    ok("task_dispatch", ORCH, task_id="T001", workdir=str(workdir), user_turn=True)
    begun = ok("attempt_start", ORCH, task_id="T001", purpose="dispatch", user_turn=True)
    ok(
        "attempt_reconcile",
        ORCH,
        task_id="T001",
        attempt="a1",
        generation=begun["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    return home, workdir


def test_the_hook_records_the_users_words_and_the_worker_cites_them(
    started: tuple[Path, Path],
) -> None:
    home, workdir = started
    out = hook("w-1", home, "<system-reminder>r</system-reminder>再加一个导出功能")
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "M-T001-1" in context and "requirement_change" in context
    # Another session's message reaches the worker the same way, but is never recorded.
    hook("w-1", home, '<cross-session-message from="orch-1">也加导入</cross-session-message>')

    replies = mcp(
        workdir,
        "w-1",
        INIT,
        LIST,
        call(
            2,
            "requirement_change",
            task_id="T001",
            message_ids=["M-T001-2"],
            goal="g，并支持导入",
            acceptance=["a", "可以导入"],
        ),
        call(
            3,
            "requirement_change",
            task_id="T001",
            message_ids=["M-T001-1"],
            goal="g，并支持导出",
            acceptance=["a", "可以导出"],
        ),
    )
    tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
    assert "message_ids" in tools["requirement_change"]["inputSchema"]["required"]
    refused = json.loads(replies[2]["result"]["content"][0]["text"])
    assert replies[2]["result"]["isError"] is True
    assert refused["details"]["reason"] == "g_change"
    done = json.loads(replies[3]["result"]["content"][0]["text"])
    event = done["events"][0]
    assert event["type"] == "task.requirement_changed"
    assert event["data"]["user_source"]["text"] == "再加一个导出功能"
    # The worker wakes the orchestrator holding the lease.
    assert done["wake"]["send"] is True and done["wake"]["session_id"] == "orch-1"
    assert "task.requirement_changed" in done["wake"]["message"]
    code, shown = run_json("task", "show", "T001")
    assert code == 0 and shown["task"]["revision"] == 2
