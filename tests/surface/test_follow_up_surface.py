"""Follow-ups through the real surfaces (DN002 §5.7): the hook records the user's words, the
worker's MCP server over stdio records and reads follow-ups, the orchestrator's tools turn them
into a task, and ``tsw task show`` and the STATUS page show the result."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json
from tests.surface.test_requirement_change_surface import (
    INIT,
    LIST,
    MACHINE,
    ORCH,
    PLUGIN,
    call,
    hook,
    ok,
)

ITEM = {"title": "周期对账", "detail": "目标→源清扫与源→目标补齐", "kind": "remaining"}


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


def result(reply: dict[str, Any]) -> Any:
    assert reply["result"].get("isError") is not True, reply
    return json.loads(reply["result"]["content"][0]["text"])


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


@pytest.mark.use_case("UC-FOLLOWUP-001")
@pytest.mark.use_case("UC-FOLLOWUP-002")
@pytest.mark.use_case("UC-FOLLOWUP-004")
def test_worker_records_over_mcp_and_the_orchestrator_turns_it_into_a_task(
    started: tuple[Path, Path],
) -> None:
    home, workdir = started
    out = hook("w-1", home, "把还没做的事记成 follow-up")
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "M-T001-1" in context and "follow_up_record" in context

    replies = mcp(
        workdir,
        "w-1",
        INIT,
        LIST,
        call(2, "follow_up_record", task_id="T001", items=[ITEM], message_ids=["M-T001-1"]),
        call(3, "follow_up_convert", follow_up_ids=["F-T001-1"], task={}),
        call(4, "task_read", task_id="T001"),
    )
    tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
    assert {"follow_up_record", "follow_up_update"} <= set(tools)
    # Turning follow-ups into tasks and dismissing them is the orchestrator's.
    assert "follow_up_convert" not in tools and "follow_up_dismiss" not in tools
    schema = tools["follow_up_record"]["inputSchema"]
    assert schema["required"] == ["task_id", "items"]
    assert schema["properties"]["items"]["items"]["required"] == ["title", "detail"]
    recorded = result(replies[2])["events"][0]
    assert recorded["type"] == "follow_up.recorded"
    assert recorded["data"]["source"]["text"] == "把还没做的事记成 follow-up"
    assert replies[3]["result"]["isError"] is True
    read = result(replies[4])
    assert [(f["follow_up_id"], f["status"]) for f in read["follow_ups"]] == [("F-T001-1", "open")]
    assert read["follow_up_of"] == []

    converted = ok("follow_up_convert", ORCH, follow_up_ids=["F-T001-1"], task={})
    assert [e["type"] for e in converted["events"]] == ["task.registered", "follow_up.converted"]
    code, shown = run_json("task", "show", "T002")
    assert code == 0
    assert shown["task"]["title"] == "周期对账"
    assert shown["task"]["follow_up_of"] == [
        {"task_id": "T001", "follow_up_id": "F-T001-1", "title": "周期对账"}
    ]
    code, source = run_json("task", "show", "T001")
    assert code == 0
    assert source["task"]["follow_ups"][0]["converted_to"] == "T002"
    page = (home / "views" / "STATUS.html").read_text(encoding="utf-8")
    assert '<a href="#task-T001">F-T001-1</a>' in page
    # A refused call over the CLI keeps its reason on stdout.
    code, refused = run_json(
        "tool",
        "run",
        "follow_up_dismiss",
        "--input",
        json.dumps({"follow_up_ids": ["F-T001-1"], "reason": "r"}),
        *ORCH,
    )
    assert code != 0 and "follow_up_closed" in json.dumps(refused)


@pytest.mark.use_case("UC-FOLLOWUP-003")
def test_the_orchestrator_dismisses_and_attention_lists_what_is_left(
    started: tuple[Path, Path],
) -> None:
    home, _ = started
    ok("follow_up_record", ORCH, task_id="T001", items=[ITEM, {**ITEM, "title": "初始全量"}])
    dismissed = ok("follow_up_dismiss", ORCH, follow_up_ids=["F-T001-1"], reason="已在别处完成")
    assert dismissed["events"][0]["type"] == "follow_up.dismissed"
    replies = mcp(home, "orch-1", INIT, call(1, "attention"))
    due = result(replies[1])["follow_ups"]
    assert [(d["follow_up_id"], d["title"]) for d in due] == [("F-T001-2", "初始全量")]
