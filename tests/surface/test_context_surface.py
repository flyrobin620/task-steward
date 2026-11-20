"""Task context retrieval through the real surfaces (DN007 §5.3, §7, §12): the orchestrator's and
a worker's MCP servers over stdio, and ``tsw context rebuild`` as a separate process."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import run_json
from tests.surface.test_follow_up_surface import mcp, result, started
from tests.surface.test_requirement_change_surface import INIT, LIST, call

__all__ = ["started"]  # the fixture, used by name

SOURCE = {
    "channel": "main-session",
    "principal": "owner",
    "message_ref": "msg-1",
    "text": "以后 PR 一律先开 draft",
    "target": ["preference", "global", "standing"],
}


@pytest.mark.use_case("UC-CONTEXT-001")
@pytest.mark.use_case("UC-CONTEXT-002")
@pytest.mark.use_case("UC-CONTEXT-004")
@pytest.mark.use_case("UC-CONTEXT-005")
def test_the_orchestrator_searches_reads_and_records_over_stdio(
    started: tuple[Path, Path],
) -> None:
    home, _ = started
    replies = mcp(
        home,
        "orch-1",
        INIT,
        LIST,
        call(2, "task_summary", task_id="T001", keywords=["对账", "补齐"], stage="intake"),
        call(3, "context_catalog", query="对账", scope="repo", repo="demo"),
        call(4, "context_read", task_ids=["T001"], part="spec"),
        call(5, "context_read", task_ids=["T001"], part="spec", reason="核对目标"),
        call(
            6,
            "preference_record",
            topic="PR",
            text="PR 先开 draft",
            scope="global",
            user_source=SOURCE,
        ),
        call(7, "context_decisions", query="draft"),
        call(8, "context_rebuild"),
        call(9, "context_search", query="对账补齐", scope="global"),
    )
    tools = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {
        "context_catalog",
        "context_read",
        "context_search",
        "context_decisions",
        "context_task",
        "context_rebuild",
        "task_summary",
        "preference_record",
    } <= tools
    assert result(replies[2])["events"][0]["type"] == "task.summary_recorded"
    catalog = result(replies[3])
    assert [c["task_id"] for c in catalog["cards"]] == ["T001"]
    assert catalog["cards"][0]["keywords"] == ["对账", "补齐"]
    assert replies[4]["result"]["isError"] is True
    read = result(replies[5])
    assert read["text"].startswith("## T001 · spec") and read["recorded"] > 0
    assert result(replies[6])["events"][0]["data"]["ref"] == "P-1"
    decisions = result(replies[7])["decisions"]
    assert [(d["ref"], d["usable_as_basis"]) for d in decisions] == [("P-1", "in_scope")]
    rebuilt = result(replies[8])
    assert rebuilt["kind"] == "context_rebuild" and rebuilt["cards"] == 1
    assert rebuilt["keyword_sources"] == {"recorded": 1}
    assert result(replies[9])["index"]["full_text"] is True


@pytest.mark.use_case("UC-CONTEXT-002")
@pytest.mark.use_case("UC-CONTEXT-004")
def test_a_worker_reads_any_task_but_cannot_write_cards(started: tuple[Path, Path]) -> None:
    _, workdir = started
    replies = mcp(
        workdir,
        "w-1",
        INIT,
        LIST,
        call(2, "context_catalog"),
        call(3, "context_task", task_id="T001"),
        call(4, "context_read", task_ids=["T001"], part="brief", reason="重读任务书"),
        call(5, "task_summary", task_id="T001", keywords=["x"], stage="intake"),
        call(6, "context_rebuild"),
    )
    tools = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"context_catalog", "context_read", "context_search", "context_decisions"} <= tools
    assert "task_summary" not in tools and "context_rebuild" not in tools
    catalog = result(replies[2])
    assert catalog["scope"] == "repo" and catalog["repo"] == "demo"  # its own repository
    assert [c["task_id"] for c in catalog["cards"]] == ["T001"]
    history = result(replies[3])
    assert history["attempts"][0]["session_ref"] == "w-1"
    assert "attempt.bound" in [t["type"] for t in history["timeline"]] or history["timeline"]
    brief = result(replies[4])
    assert "T001" in brief["text"] and brief["sources"][0]["refs"] == ["runs/T001/a1/brief.md"]
    for i in (5, 6):
        assert replies[i]["result"]["isError"] is True


@pytest.mark.use_case("UC-CONTEXT-003")
def test_context_rebuild_on_the_command_line(started: tuple[Path, Path]) -> None:
    home, _ = started
    index = home / "cache" / "context.sqlite"
    code, out = run_json("context", "rebuild", "--home", str(home))
    assert code == 0, out
    assert out["cards"] == 1 and out["keyword_sources"] == {"generated": 1}
    assert out["indexed_seq"] > 0 and index.is_file()
    index.write_bytes(b"garbage")
    code, out = run_json("context", "rebuild", "--home", str(home))
    assert code == 0 and out["cards"] == 1
