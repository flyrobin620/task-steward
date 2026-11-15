"""Standing preferences through the real surfaces (DN007 §6.1, §6.2): the orchestrator's MCP
server over stdio lists, records, replaces and revokes them, the user does the same with
``tsw preference`` in a separate process, and a preference recorded in one MCP server process is
in the next brief written by another, with nothing rendered or restarted in between."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tests.support import child_env, run_json, run_tsw
from tests.surface.test_direct_surface import handed_back
from tests.surface.test_follow_up_surface import mcp, result, started
from tests.surface.test_requirement_change_surface import INIT, LIST, call
from tsw.home import Home

__all__ = ["started"]  # the fixture, used by name


def said(scope: str, ref: str) -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": ref,
        "text": "以后都这样",
        "target": ["preference", scope, "standing"],
    }


def errored(reply: dict[str, Any]) -> bool:
    return reply["result"].get("isError") is True


@pytest.mark.use_case("UC-CONTEXT-005")
@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_preference_recorded_over_mcp_reaches_the_next_brief_without_updating_tsw(
    started: tuple[Path, Path],
) -> None:
    home, workdir = started
    goal = "适用的用户偏好：\n- P-1（global）：画图前先列出要表达的关系"
    first = mcp(
        home,
        "orch-1",
        INIT,
        LIST,
        call(
            2,
            "preference_record",
            topic="画图",
            text="画图前先列出要表达的关系",
            scope="global",
            user_source=said("global", "m-1"),
        ),
        call(
            3,
            "task_register",
            task_id="T002",
            title="t2",
            repo="demo",
            kind="code",
            goal=goal,
            acceptance=["a"],
        ),
        call(4, "task_plan", task_id="T002", reason="无依赖"),
    )
    tools = {t["name"] for t in first[1]["result"]["tools"]}
    assert {"preference_record", "preference_revoke", "context_decisions"} <= tools
    assert result(first[2])["events"][0]["data"]["ref"] == "P-1"
    assert not errored(first[3]) and not errored(first[4])

    # A new server process, same product: list without a topic, replace P-1, dispatch T002.
    second = mcp(
        home,
        "orch-1",
        INIT,
        call(2, "context_decisions", kind="standing", scope="repo", repo="demo"),
        call(
            3,
            "preference_record",
            topic="画图",
            text="画图前先列出要表达的关系，并写明读者",
            scope="global",
            supersedes=["P-1"],
            user_source=said("global", "m-2"),
        ),
        call(4, "context_decisions", kind="standing", scope="repo", repo="demo"),
        call(
            5,
            "task_dispatch",
            task_id="T002",
            workdir=str(workdir / "t2"),
            user_turn=True,
            read_set=["P-2"],  # found after intake: added for the worker, the goal unchanged
        ),
        call(
            6,
            "preference_revoke",
            ref="P-1",
            reason="已被替代",
            user_source={**said("global", "m-3"), "target": ["preference", "P-1", "revoke"]},
        ),
        call(
            7,
            "preference_revoke",
            ref="P-2",
            reason="不再需要",
            user_source={**said("global", "m-4"), "target": ["preference", "P-2", "revoke"]},
        ),
        call(8, "context_decisions", kind="standing", scope="repo", repo="demo"),
    )
    listed = result(second[2])
    assert (listed["kind"], [d["ref"] for d in listed["decisions"]]) == ("standing", ["P-1"])
    assert result(second[3])["events"][0]["data"]["supersedes"] == ["P-1"]
    now = {d["ref"]: d["usable_as_basis"] for d in result(second[4])["decisions"]}
    assert now == {"P-1": False, "P-2": "in_scope"}
    assert not errored(second[5])
    brief = (home / "runs" / "T002" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert (
        "P-1 已被替代，现在有效的是 P-2（global）：“画图前先列出要表达的关系，并写明读者”" in brief
    )
    assert "- 派发时补充的适用偏好（主会话选定，DN007 §6.1）：P-2（global）" in brief
    assert "- 授权读取集合：T002\n" in brief
    assert errored(second[6])  # a replaced one cannot be revoked
    assert result(second[7])["events"][0]["type"] == "user.preference_revoked"
    gone = {d["ref"]: d for d in result(second[8])["decisions"]}
    assert (
        gone["P-2"]["usable_as_basis"] is False and gone["P-2"]["revoked"]["reason"] == "不再需要"
    )


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_worker_reads_preferences_but_cannot_change_them(started: tuple[Path, Path]) -> None:
    _, workdir = started
    replies = mcp(
        workdir,
        "w-1",
        INIT,
        LIST,
        call(2, "context_decisions", kind="standing"),
    )
    tools = {t["name"] for t in replies[1]["result"]["tools"]}
    assert "context_decisions" in tools
    assert "preference_record" not in tools and "preference_revoke" not in tools
    assert result(replies[2])["decisions"] == []


@pytest.mark.use_case("UC-CONTEXT-006")
def test_the_user_lists_records_and_revokes_preferences_in_the_terminal(home: Home) -> None:
    h = Harness(home)
    handed_back(h)  # the harness's orchestrator is on another machine: this one may write
    code, out = run_json(
        "preference",
        "record",
        "--scope",
        "repo:demo",
        "--topic",
        "PR",
        "--text",
        "demo 的 PR 默认 ready",
        "--yes",
    )
    assert code == 0, out
    assert (out["ref"], out["event"], out["channel"]) == (
        "P-1",
        "user.preference_recorded",
        "user-cli",
    )
    source = next(
        e for e in h.ledger.storage.read(since=0) if e.type == "user.preference_recorded"
    ).data["source"]
    assert source["channel"] == "user-cli" and source["message_ref"].startswith("cli:")
    assert (source["text"], source["target"]) == (
        "demo 的 PR 默认 ready",
        ["preference", "repo:demo", "standing"],
    )
    code, out = run_json(
        "preference",
        "record",
        "--scope",
        "repo:demo",
        "--topic",
        "PR",
        "--text",
        "demo 的 PR 默认 draft",
        "--supersedes",
        "P-1",
        "--yes",
    )
    assert (code, out["ref"]) == (0, "P-2")
    code, listed = run_json("preference", "list", "--repo", "demo")
    assert (code, [p["ref"] for p in listed["preferences"]]) == (0, ["P-2"])
    code, other = run_json("preference", "list", "--repo", "other")
    assert (code, other["preferences"]) == (0, [])

    code, out = run_json("preference", "revoke", "P-2", "--reason", "不需要了", "--yes")
    assert (code, out["event"], out["ref"]) == (0, "user.preference_revoked", "P-2")
    code, listed = run_json("preference", "list")
    assert (code, listed["preferences"]) == (0, [])
    code, everything = run_json("preference", "list", "--all")
    by_ref = {p["ref"]: p for p in everything["preferences"]}
    assert by_ref["P-1"]["superseded_by"] == "P-2"
    assert by_ref["P-2"]["revoked"]["reason"] == "不需要了"

    code, err = run_json("preference", "revoke", "P-9", "--reason", "x", "--yes")
    assert (code, err["details"]["reason"]) == (3, "invalid_input")
    # Without --yes and without a terminal to ask on, nothing is written.
    proc = run_tsw("preference", "record", "--scope", "global", "--topic", "t", "--text", "x")
    assert proc.returncode == 3 and "not_confirmed" in proc.stderr
    assert len(run_json("preference", "list", "--all")[1]["preferences"]) == 2
    assert run_tsw("preference", "revoke", "P-1", "--yes").returncode == 2  # --reason required


@pytest.mark.use_case("UC-CONTEXT-006")
@pytest.mark.parametrize("marker", ["CLAUDE_CODE_HOST_SESSION_ID", "CLAUDECODE"])
def test_the_users_preference_commands_refuse_inside_any_host_session(
    home: Home, marker: str
) -> None:
    Harness(home)
    env = child_env(**{marker: "1"})
    for args in (
        ("preference", "record", "--scope", "global", "--topic", "t", "--text", "x", "--yes"),
        ("preference", "revoke", "P-1", "--reason", "x", "--yes"),
    ):
        code, err = run_json(*args, env=env)
        assert (code, err["details"]["reason"]) == (3, "host_session_cli"), args
