"""Preference candidates through the real surfaces (DN007 §6.3, CC007): the hook records the
user's words, the worker's MCP server over stdio hands out the materials and the subagent's prompt
and records a scan, the orchestrator's server confirms a candidate as a preference, and the user
lists, confirms and turns candidates down with ``tsw preference`` in a separate process."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tests.support import child_env, run_json
from tests.surface.test_direct_surface import handed_back
from tests.surface.test_follow_up_surface import mcp, result, started
from tests.surface.test_requirement_change_surface import INIT, LIST, call, hook
from tsw.home import Home

__all__ = ["started"]  # the fixture, used by name


def errored(reply: dict[str, Any]) -> bool:
    return reply["result"].get("isError") is True


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_worker_scans_over_mcp_and_the_orchestrator_confirms_with_the_users_words(
    started: tuple[Path, Path],
) -> None:
    home, workdir = started
    hook("w-1", home, "以后邮件一律只存草稿，不要直接发。")
    worker = mcp(
        workdir,
        "w-1",
        INIT,
        LIST,
        call(2, "preference_sources", report_path="work/T001/a1/result.md"),
    )
    tools = {t["name"]: t for t in worker[1]["result"]["tools"]}
    assert {"preference_sources", "preference_candidates_record"} <= set(tools)
    # Confirming or turning a candidate down is the user's, through the orchestrator.
    assert "preference_record" not in tools and "preference_candidate_dismiss" not in tools
    assert tools["preference_candidates_record"]["inputSchema"]["required"] == [
        "task_id",
        "scanned_through",
        "candidates",
    ]
    given = result(worker[2])
    assert [m["ref"] for m in given["materials"]] == ["goal@r1", "M-T001-1"]
    assert given["prompt"].startswith("# 搜集用户偏好候选") and "M-T001-1" in given["prompt"]
    assert given["scan"]["state"] == "missing"
    quote = {
        "quote": "邮件一律只存草稿",
        "source_ref": "M-T001-1",
        "topic": "邮件",
        "reason": "长期",
    }
    recorded = mcp(
        workdir,
        "w-1",
        INIT,
        call(
            2,
            "preference_candidates_record",
            task_id="T001",
            scanned_through=given["scanned_through"],
            candidates=[{**quote, "quote": "邮件都直接发"}],
        ),
        call(
            3,
            "preference_candidates_record",
            task_id="T001",
            scanned_through=given["scanned_through"],
            candidates=[quote],
        ),
        call(4, "task_read", task_id="T001"),
    )
    assert errored(recorded[2]) and "quote_not_found" in recorded[2]["result"]["content"][0]["text"]
    assert [e["type"] for e in result(recorded[3])["events"]] == [
        "user.preference_candidate",
        "task.preference_scanned",
    ]
    read = result(recorded[4])
    assert read["preference_scan"]["state"] == "done"
    assert [(c["ref"], c["status"]) for c in read["preference_candidates"]] == [("PC-1", "open")]

    orchestrator = mcp(
        home,
        "orch-1",
        INIT,
        call(2, "attention"),
        call(3, "context_decisions", kind="candidate"),
        call(
            4,
            "preference_record",
            candidate="PC-1",
            scope="global",
            user_source={
                "channel": "main-session",
                "principal": "owner",
                "message_ref": "m-1",
                "text": "PC-1 记下来",
                "target": ["preference", "global", "standing"],
            },
        ),
        call(5, "context_decisions", kind="standing"),
        call(6, "attention"),
    )
    assert [c["ref"] for c in result(orchestrator[2])["preference_candidates"]] == ["PC-1"]
    listed = result(orchestrator[3])["decisions"]
    assert [(d["ref"], d["usable_as_basis"]) for d in listed] == [("PC-1", False)]
    data = result(orchestrator[4])["events"][0]["data"]
    assert (data["ref"], data["text"], data["candidate"]) == ("P-1", "邮件一律只存草稿", "PC-1")
    standing = result(orchestrator[5])["decisions"]
    assert [(d["ref"], d["usable_as_basis"]) for d in standing] == [("P-1", "in_scope")]
    assert result(orchestrator[6])["preference_candidates"] == []


def candidates_of(h: Harness) -> None:
    """T001 dispatched; its worker records two candidates quoting the goal."""
    h.register("T001", goal="用户原话：“以后画图都用 diagram skill；以后报告先写结论。”")
    h.plan("T001")
    worker = h.dispatch("T001")
    through = max(e.sequence for e in h.ledger.storage.read(since=0))
    h.run(
        "preference_candidates_record",
        worker,
        task_id="T001",
        scanned_through=through,
        candidates=[
            {
                "quote": "以后画图都用 diagram skill",
                "source_ref": "goal@r1",
                "topic": "画图",
                "reason": "长期",
            },
            {
                "quote": "以后报告先写结论",
                "source_ref": "goal@r1",
                "topic": "报告",
                "reason": "长期",
            },
        ],
    )


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_user_lists_confirms_and_turns_down_candidates_in_the_terminal(home: Home) -> None:
    h = Harness(home)
    candidates_of(h)
    handed_back(h)  # the harness's orchestrator is on another machine: this one may write
    code, listed = run_json("preference", "list", "--candidates")
    assert (code, [c["ref"] for c in listed["candidates"]]) == (0, ["PC-1", "PC-2"])
    code, out = run_json("preference", "record", "--candidate", "PC-1", "--yes")
    assert (code, out["ref"], out["event"], out["channel"]) == (
        0,
        "P-1",
        "user.preference_recorded",
        "user-cli",
    )
    recorded = [e for e in h.ledger.storage.read(since=0) if e.type == "user.preference_recorded"]
    data = recorded[-1].data
    assert (data["text"], data["topic"], data["scope"], data["candidate"]) == (
        "以后画图都用 diagram skill",
        "画图",
        "global",
        "PC-1",
    )
    assert data["source"]["text"] == "以后画图都用 diagram skill"
    code, out = run_json("preference", "dismiss", "PC-2", "--reason", "只是这次", "--yes")
    assert (code, out["event"], out["ref"]) == (0, "user.preference_candidate_dismissed", "PC-2")
    code, listed = run_json("preference", "list", "--candidates")
    assert (code, listed["candidates"]) == (0, [])
    code, every = run_json("preference", "list", "--candidates", "--all")
    assert [(c["ref"], c["status"]) for c in every["candidates"]] == [
        ("PC-1", "recorded"),
        ("PC-2", "dismissed"),
    ]
    code, err = run_json("preference", "dismiss", "PC-2", "--reason", "x", "--yes")
    assert (code, err["details"]["reason"]) == (3, "candidate_closed")
    code, err = run_json("preference", "record", "--topic", "t", "--yes")
    assert (code, err["details"]["reason"]) == (3, "invalid_input")


@pytest.mark.use_case("UC-CONTEXT-007")
@pytest.mark.parametrize("marker", ["CLAUDE_CODE_HOST_SESSION_ID", "CLAUDECODE"])
def test_the_users_candidate_commands_refuse_inside_any_host_session(
    home: Home, marker: str
) -> None:
    Harness(home)
    env = child_env(**{marker: "1"})
    for args in (
        ("preference", "record", "--candidate", "PC-1", "--yes"),
        ("preference", "dismiss", "PC-1", "--reason", "x", "--yes"),
    ):
        code, err = run_json(*args, env=env)
        assert (code, err["details"]["reason"]) == (3, "host_session_cli"), args
