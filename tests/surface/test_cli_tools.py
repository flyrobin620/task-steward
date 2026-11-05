"""The state machine through ``tsw tool run`` as a separate process (the CLI side of DN001 §7.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import run_json, run_tsw

ORCH = ["--role", "orchestrator", "--session", "orch-1", "--machine", "m1"]


def worker(session: str) -> list[str]:
    return ["--role", "worker", "--session", session, "--machine", "m1"]


def tool(name: str, caller: list[str], **inp: Any) -> dict[str, Any]:
    code, out = run_json(
        "tool", "run", name, "--input", json.dumps(inp, ensure_ascii=False), *caller
    )
    assert code == 0, out
    return out


def source(task_id: str, kind: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        **({"request_id": request_id} if request_id else {}),
        "channel": "main-session",
        "principal": "owner",
        "message_ref": f"m-{kind}",
        "text": "好",
        "target": [task_id, 1, kind],
    }


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    assert run_json("lease", "acquire", "--session", "orch-1", "--machine", "m1")[0] == 0
    return root


@pytest.mark.use_case("UC-PLAN-001")
@pytest.mark.use_case("UC-DISPATCH-001")
@pytest.mark.use_case("UC-REVIEW-001")
@pytest.mark.use_case("UC-JUDGE-001")
@pytest.mark.use_case("UC-VERIFY-001")
@pytest.mark.use_case("UC-LIFE-001")
def test_a_task_from_registration_to_user_acceptance(home: Path) -> None:
    tool(
        "task_register",
        ORCH,
        task_id="T001",
        title="导出",
        repo="demo",
        kind="code",
        goal="导出 CSV",
        acceptance=["可导出"],
    )
    tool("task_plan", ORCH, task_id="T001", reason="无依赖")
    tool("task_dispatch", ORCH, task_id="T001", workdir="~/wt/T001", user_turn=True)
    started = tool("attempt_start", ORCH, task_id="T001", purpose="dispatch", user_turn=True)
    generation = started["events"][0]["data"]["generation"]
    tool(
        "attempt_reconcile",
        ORCH,
        task_id="T001",
        attempt="a1",
        generation=generation,
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    w = worker("w-1")
    tool("question", w, task_id="T001", text="CSV 用什么编码？")
    tool(
        "judge",
        ORCH,
        task_id="T001",
        items=["J-T001-1"],
        decision="escalate",
        question="编码？",
        rationale="用户偏好",
        basis=["任务书"],
        confidence="low",
    )
    tool(
        "input_resolve",
        ORCH,
        task_id="T001",
        request_id="Q-T001-1",
        answer="UTF-8",
        user_source=source("T001", "answer", "Q-T001-1"),
    )
    round_no = tool(
        "review_start",
        w,
        task_id="T001",
        head="h1",
        reviewer={"session": "codex-app-server", "model": "gpt-6-astra"},
    )["events"][0]["data"]["round"]
    tool(
        "review_complete",
        w,
        task_id="T001",
        round=round_no,
        result_path="r1.json",
        result={"verdict": "pass", "summary": "ok", "reviewed_head": "h1", "findings": []},
    )
    tool("review_triage", w, task_id="T001", round=round_no, items=[])
    tool("review_decide", w, task_id="T001", round=round_no, decision="finalize", head="h1")
    draft = home / "work" / "T001" / "a1"
    draft.mkdir(parents=True)
    (draft / "result.md").write_text("# 报告", encoding="utf-8")
    (draft / "result.html").write_text("<h1>报告</h1>", encoding="utf-8")
    tool(
        "submit",
        w,
        task_id="T001",
        head="h1",
        report_md="work/T001/a1/result.md",
        report_html="work/T001/a1/result.html",
        worktree_clean=True,
    )
    tool(
        "output_link",
        ORCH,
        task_id="T001",
        output_id="pr-1",
        type="pr",
        ref="u",
        evidence=["e"],
        verified_at="2026-11-05T00:00:00Z",
    )
    tool(
        "task_accept",
        ORCH,
        task_id="T001",
        verified_head="h1",
        summary="通过",
        acceptance=[{"item": "可导出", "status": "done", "evidence": "截图"}],
    )
    tool("user_accept", ORCH, task_id="T001", user_source=source("T001", "accept"))
    out = tool("sync", ORCH)
    assert out["events"] == []
    code, shown = run_json("task", "show", "T001")
    assert (code, shown["task"]["status"]) == (0, "accepted")


@pytest.mark.use_case("UC-PLAN-001")
def test_tool_errors_are_reported_on_stderr(home: Path) -> None:
    proc = run_tsw(
        "tool", "run", "task_plan", "--input", '{"task_id": "T404", "reason": "r"}', *ORCH, "--json"
    )
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert json.loads(proc.stderr)["details"]["reason"] == "task_not_found"
    proc = run_tsw("tool", "run", "task_plan", "--input", "[1]", *ORCH)
    assert proc.returncode == 3
    proc = run_tsw("tool", "run", "nope", "--input", "{}", *ORCH, "--json")
    assert json.loads(proc.stderr)["details"]["reason"] == "unknown_tool"
    code, listing = run_json("tool", "list")
    assert code == 0
    names = [t["name"] for t in listing["tools"]]
    assert "task_dispatch" in names
    assert "sync" in names
