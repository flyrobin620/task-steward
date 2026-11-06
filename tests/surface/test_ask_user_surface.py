"""Asking the user in the worker's session, through ``tsw tool run`` and the real hook process."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import REPO_ROOT, child_env, run_json

MACHINE = socket.gethostname()  # the hook process records on this machine
ORCH = ["--role", "orchestrator", "--session", "orch-1", "--machine", MACHINE]
WORKER = ["--role", "worker", "--session", "w-1", "--machine", MACHINE]


def tool(name: str, caller: list[str], **inp: Any) -> tuple[int, dict[str, Any]]:
    return run_json("tool", "run", name, "--input", json.dumps(inp, ensure_ascii=False), *caller)


def ok(name: str, caller: list[str], **inp: Any) -> dict[str, Any]:
    code, out = tool(name, caller, **inp)
    assert code == 0, out
    return out


def hook(event: str, session: str, payload: dict[str, Any]) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"),
            "guard",
            event,
        ],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
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
    ok("task_dispatch", ORCH, task_id="T001", workdir="~/wt/T001", user_turn=True)
    started = ok("attempt_start", ORCH, task_id="T001", purpose="dispatch", user_turn=True)
    ok(
        "attempt_reconcile",
        ORCH,
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    return root


@pytest.mark.use_case("UC-ASK-001")
def test_ask_record_and_answer_across_processes(home: Path) -> None:
    asked = ok("ask_user", WORKER, task_id="T001", question="CSV 用什么编码？")
    request_id = asked["events"][0]["data"]["request_id"]
    out = hook("UserPromptSubmit", "w-1", {"cwd": str(home), "prompt": "UTF-8"})
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "M-T001-1" in context
    # The orchestrator cannot answer before the handoff, and cannot record in-session words.
    code, refused = tool(
        "handoff_resolve",
        ORCH,
        task_id="T001",
        request_id=request_id,
        decision="answer",
        answer="GBK",
        rationale="r",
        basis=["b"],
        confidence="low",
    )
    assert code != 0 and refused["details"]["reason"] == "not_handed_off"
    resolved = ok(
        "user_answer",
        WORKER,
        task_id="T001",
        request_id=request_id,
        message_id="M-T001-1",
        answer="UTF-8",
    )
    assert resolved["events"][0]["data"]["source"]["text"] == "UTF-8"
    code, listed = run_json("task", "show", "T001")
    assert code == 0 and listed["task"]["status"] == "in_progress"
