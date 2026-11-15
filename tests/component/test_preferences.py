"""The user's standing preferences as data (DN007 §6.1, §6.2): listed in full without a topic,
managed by record / replace / revoke, and checked in every brief tsw writes, all read from the
ledger at the time, on a real data directory, ledger and SQLite."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw.context import ContextService, Reader
from tsw.errors import TswError
from tsw.home import Home
from tsw.machine.base import Caller

ORCH_READER = Reader("orchestrator", "orch-1")
USER = Caller(role="user", session_id=None, machine=MACHINE)


def said(scope: str, ref: str, channel: str = "main-session") -> dict[str, Any]:
    return {
        "channel": channel,
        "principal": "owner",
        "message_ref": ref,
        "text": "原话",
        "target": ["preference", scope, "standing"],
    }


def revoke_said(ref: str, message: str, channel: str = "main-session") -> dict[str, Any]:
    return {
        "channel": channel,
        "principal": "owner",
        "message_ref": message,
        "text": "不要了",
        "target": ["preference", ref, "revoke"],
    }


def record(h: Harness, scope: str, text: str, *, caller: Caller = ORCH, **more: Any) -> str:
    channel = "user-cli" if caller.role == "user" else "main-session"
    out = h.run(
        "preference_record",
        caller,
        topic=more.pop("topic", "做法"),
        text=text,
        scope=scope,
        user_source=said(scope, f"m-{text}", channel),
        **more,
    )
    return str(out["events"][0]["data"]["ref"])


def reason_of(h: Harness, name: str, caller: Caller = ORCH, **inp: Any) -> str:
    with pytest.raises(TswError) as exc:
        h.run(name, caller, **inp)
    return str(exc.value.details.get("reason"))


def listed(svc: ContextService, **args: Any) -> dict[str, Any]:
    out = svc.decisions(ORCH_READER, {"kind": "standing", **args})["decisions"]
    return {d["ref"]: d["usable_as_basis"] for d in out}


def brief(h: Harness, task_id: str) -> str:
    return (h.home.root / "runs" / task_id / "a1" / "brief.md").read_text(encoding="utf-8")


@pytest.mark.use_case("UC-CONTEXT-005")
def test_every_standing_preference_reaching_a_task_is_listed_without_a_topic(home: Home) -> None:
    h = Harness(home)
    h.register("T001", goal="实现功能")
    # A task decision: an answer the user gave on T001 (not a standing preference).
    h.run(
        "task_update",
        task_id="T001",
        goal="实现功能，导出为 CSV",
        acceptance=["功能可用"],
        user_source=h.source("T001", 1, "change", "改成 CSV"),
    )
    record(h, "global", "画架构图时用某个流程", topic="图")
    record(h, "repo:demo", "demo 的 PR 默认 ready", topic="PR")
    record(h, "repo:other", "other 的提交要签名", topic="提交")
    record(h, "project:alpha", "alpha 的报告用英文", topic="报告")
    svc = ContextService(h.home, h.ledger, h.config)

    # Nothing in the task's words matches "图" or "PR": listing does not depend on a topic.
    assert listed(svc, scope="repo", repo="demo") == {"P-1": "in_scope", "P-2": "in_scope"}
    assert listed(svc, scope="repo", repo="demo", project="alpha") == {
        "P-1": "in_scope",
        "P-2": "in_scope",
        "P-4": "in_scope",
    }
    assert set(listed(svc)) == {"P-1", "P-2", "P-3", "P-4"}
    # kind keeps the other decisions out, and lists them alone when asked.
    kinds = {
        d["kind"]
        for d in svc.decisions(ORCH_READER, {"scope": "task", "task_id": "T001"})["decisions"]
    }
    assert kinds == {"standing", "task"}
    tasks = svc.decisions(ORCH_READER, {"kind": "task", "scope": "task", "task_id": "T001"})
    assert [d["kind"] for d in tasks["decisions"]] == ["task"] and tasks["kind"] == "task"
    # Paging walks the whole list.
    first = svc.decisions(ORCH_READER, {"kind": "standing", "limit": 3})
    rest = svc.decisions(ORCH_READER, {"kind": "standing", "limit": 3, "offset": 3})
    assert first["next_offset"] == 3 and rest["next_offset"] is None
    assert {d["ref"] for d in first["decisions"] + rest["decisions"]} == {
        "P-1",
        "P-2",
        "P-3",
        "P-4",
    }
    with pytest.raises(TswError) as bad_kind:
        svc.decisions(ORCH_READER, {"kind": "habit"})
    assert bad_kind.value.details["reason"] == "invalid_input"
    with pytest.raises(TswError) as bad_project:
        svc.decisions(ORCH_READER, {"project": "alpha"})
    assert bad_project.value.details["field"] == "project"


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_revoked_preference_stops_counting_and_lifts_its_override(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    wide = record(h, "global", "PR 先开 draft", topic="PR")
    narrow = record(h, "repo:demo", "demo 的 PR 直接 ready", topic="PR", supersedes=[wide])
    svc = ContextService(h.home, h.ledger, h.config)
    assert listed(svc, scope="repo", repo="demo") == {wide: False, narrow: "in_scope"}

    out = h.run(
        "preference_revoke",
        ref=narrow,
        reason="demo 也先开 draft",
        user_source=revoke_said(narrow, "m-revoke"),
    )
    event = out["events"][0]
    assert (event["type"], event["data"]["ref"], event["data"]["scope"]) == (
        "user.preference_revoked",
        narrow,
        "repo:demo",
    )
    # The global one holds in demo again; the revoked one is still there to read.
    assert listed(svc, scope="repo", repo="demo") == {wide: "in_scope", narrow: False}
    revoked = next(
        d
        for d in svc.decisions(ORCH_READER, {"kind": "standing"})["decisions"]
        if d["ref"] == narrow
    )
    assert revoked["revoked"]["reason"] == "demo 也先开 draft"
    assert "overridden_in" not in next(
        d for d in svc.decisions(ORCH_READER, {"kind": "standing"})["decisions"] if d["ref"] == wide
    )
    # Only a preference in force can be revoked or replaced.
    assert (
        reason_of(
            h,
            "preference_revoke",
            ref=narrow,
            reason="again",
            user_source=revoke_said(narrow, "m-again"),
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            h,
            "preference_record",
            topic="PR",
            text="x",
            scope="repo:demo",
            supersedes=[narrow],
            user_source=said("repo:demo", "m-x"),
        )
        == "invalid_input"
    )
    newer = record(h, "global", "PR 先开 draft 并请求评审", topic="PR", supersedes=[wide])
    assert (
        reason_of(
            h, "preference_revoke", ref=wide, reason="旧的", user_source=revoke_said(wide, "m-old")
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            h, "preference_revoke", ref="P-99", reason="无", user_source=revoke_said("P-99", "m-99")
        )
        == "invalid_input"
    )
    # The words must be about revoking this one.
    assert (
        reason_of(h, "preference_revoke", ref=newer, reason="x", user_source=said("global", "m-y"))
        == "g_user"
    )
    # The ledger keeps every event: nothing is rewritten.
    types = [e.type for e in h.ledger.storage.read(since=0) if e.type.startswith("user.pref")]
    assert types == [
        "user.preference_recorded",
        "user.preference_recorded",
        "user.preference_revoked",
        "user.preference_recorded",
    ]


@pytest.mark.use_case("UC-CONTEXT-006")
def test_the_user_manages_preferences_only_on_the_direct_channels(home: Home) -> None:
    h = Harness(home)
    # The user's own terminal: the user caller, on user-cli; no lease needed.
    ref = record(h, "global", "邮件只存草稿", caller=USER, topic="邮件")
    assert (
        reason_of(
            h,
            "preference_record",
            USER,
            topic="x",
            text="y",
            scope="global",
            user_source=said("global", "m-1", "main-session"),
        )
        == "g_user"
    )
    # A session cannot claim the terminal's channel.
    assert (
        reason_of(
            h,
            "preference_revoke",
            ORCH,
            ref=ref,
            reason="不要了",
            user_source=revoke_said(ref, "m-2", "user-cli"),
        )
        == "g_user"
    )
    out = h.run(
        "preference_revoke",
        USER,
        ref=ref,
        reason="不要了",
        user_source=revoke_said(ref, "m-3", "user-cli"),
    )
    assert out["events"][0]["data"]["source"]["channel"] == "user-cli"
    # A worker has neither tool.
    worker = Caller(role="worker", session_id="w-1", machine=MACHINE)
    assert (
        reason_of(
            h,
            "preference_record",
            worker,
            topic="x",
            text="y",
            scope="global",
            user_source=said("global", "m-4", "in-session"),
        )
        == "role_denied"
    )


@pytest.mark.use_case("UC-CONTEXT-006")
def test_every_brief_checks_the_preferences_its_goal_cites_against_the_ledger(
    home: Home,
) -> None:
    """A preference replaced or revoked after the goal was written shows in the next brief,
    without touching the product (no render, no new code): it is read from the ledger."""
    h = Harness(home)
    p1 = record(h, "global", "画图用个人 skill", topic="图")
    p2 = record(h, "repo:other", "other 仓库的约定", topic="约定")
    p3 = record(h, "project:alpha", "alpha 的报告用英文", topic="报告")
    goal = (
        f"## 背景\n\n适用的用户偏好：\n- {p1}（global）：画图用个人 skill\n"
        f"- {p3}（project:alpha）\n"
    )
    h.register("T001", goal=goal)
    h.plan("T001")
    h.dispatch("T001")
    first = brief(h, "T001")
    assert "偏好核对" not in first  # every cited one is in force for this task

    # The user replaces P-1 and the orchestrator cites another repository's and a missing one.
    p4 = record(h, "global", "画图用个人 skill，只出 SVG", topic="图", supersedes=[p1])
    h.run(
        "task_update",
        task_id="T001",
        goal=goal + f"- {p2}\n- P-99\n- 不是编号的 SP-1 与 P-1x\n",
        acceptance=["功能可用"],
        user_source=h.source("T001", 1, "change", "补充"),
    )
    second = brief(h, "T001")
    line = next(x for x in second.splitlines() if x.startswith("- 偏好核对"))
    assert f"{p1} 已被替代，现在有效的是 {p4}（global）：“画图用个人 skill，只出 SVG”" in line
    assert f"{p2}（repo:other）的范围不包括仓库 demo" in line
    assert "P-99 不存在" in line
    assert p3 not in line  # a project preference the goal cites is taken to reach the task
    assert "SP-1" not in line and "P-1x" not in line
    assert second.index("- 偏好核对") < second.index("## 目标")

    # Revoked after the brief was written: the next brief (a later revision) says so.
    h.run(
        "preference_revoke",
        ref=p4,
        reason="不再需要",
        user_source=revoke_said(p4, "m-revoke"),
    )
    h.run(
        "task_update",
        task_id="T001",
        goal=goal.replace(p1, p4),
        acceptance=["功能可用"],
        user_source=h.source("T001", 2, "change", "改引用"),
    )
    third = brief(h, "T001")
    assert f"{p4} 已撤销（理由：不再需要）" in third


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_narrower_preference_in_the_tasks_repository_is_named_in_the_brief(home: Home) -> None:
    h = Harness(home)
    wide = record(h, "global", "PR 先开 draft", topic="PR")
    narrow = record(h, "repo:demo", "demo 的 PR 直接 ready", topic="PR", supersedes=[wide])
    h.register("T001", goal=f"适用的用户偏好：{wide}（global）：PR 先开 draft")
    h.plan("T001")
    h.dispatch("T001")
    assert (
        f"{wide} 在仓库 demo 中被更窄范围的 {narrow}（repo:demo）：“demo 的 PR 直接 ready”覆盖"
        in brief(h, "T001")
    )


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_replacement_overridden_in_the_repository_is_not_called_in_force(home: Home) -> None:
    """Review r1 F2: global P-1 replaced by global P-2, which repo:demo's P-3 overrides."""
    h = Harness(home)
    p1 = record(h, "global", "PR 先开 draft", topic="PR")
    p2 = record(h, "global", "PR 先开 draft 并请求评审", topic="PR", supersedes=[p1])
    p3 = record(h, "repo:demo", "demo 的 PR 直接 ready", topic="PR", supersedes=[p2])
    h.register("T001", goal=f"适用的用户偏好：{p1}（global）：PR 先开 draft")
    h.plan("T001")
    h.dispatch("T001")
    line = next(x for x in brief(h, "T001").splitlines() if x.startswith("- 偏好核对"))
    assert (
        f"{p1} 已被 {p2} 替代，{p2} 在仓库 demo 中又被更窄范围的 "
        f"{p3}（repo:demo）：“demo 的 PR 直接 ready”覆盖"
    ) in line
    assert "现在有效的是" not in line


@pytest.mark.use_case("UC-CONTEXT-006")
def test_preferences_found_after_intake_are_added_through_the_read_set(home: Home) -> None:
    """Review r1 F1 (the user chose this way): a preference that applies but is not in the goal
    goes into the read set at dispatch or continuation; tsw checks it and names it in the brief,
    without rewriting the goal."""
    h = Harness(home)
    h.register("T001", goal="实现功能")
    h.register("T002")
    h.plan("T001")
    old = record(h, "global", "画图前先列关系", topic="图")
    p_new = record(h, "global", "画图前先列关系并写读者", topic="图", supersedes=[old])
    other = record(h, "repo:other", "other 的约定", topic="约定")
    for bad, why in (("P-99", "P-99 不存在"), (old, "已被替代"), (other, "范围不包括仓库 demo")):
        with pytest.raises(TswError) as exc:
            h.run(
                "task_dispatch",
                task_id="T001",
                workdir="~/wt/T001",
                user_turn=True,
                read_set=["T002", bad],
            )
        assert exc.value.details["reason"] == "invalid_input"
        assert why in exc.value.details["problems"][0]
    h.run(
        "task_dispatch",
        task_id="T001",
        workdir="~/wt/T001",
        user_turn=True,
        read_set=["T002", p_new],
    )
    first = brief(h, "T001")
    assert "- 授权读取集合：T001, T002\n" in first
    assert (
        "- 派发时补充的适用偏好（主会话选定，DN007 §6.1）："
        f"{p_new}（global）：“画图前先列关系并写读者”" in first
    )
    assert h.task("T001")["goal"] == "实现功能"  # the goal is the user's record: not rewritten
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
        receipt={"model": "claude-opus-5-5[1m]"},
    )
    # A preference recorded while the worker is at work: added on continuation, the brief is
    # rewritten at once at the same revision.
    later = record(h, "repo:demo", "demo 的报告附截图", topic="报告")
    out = h.run("attempt_continue", task_id="T001", read_set=[later])
    rewritten = out["events"][0]["data"]["brief_rewritten"]
    assert rewritten == {"path": "runs/T001/a1/brief.md", "preferences": [later]}
    second = brief(h, "T001")
    assert f"{later}（repo:demo）：“demo 的报告附截图”" in second and "- revision：1\n" in second
    # Replaced after it was added: the next brief says which one is in force now.
    newest = record(h, "global", "画图只出 SVG", topic="图", supersedes=[p_new])
    h.run(
        "task_update",
        task_id="T001",
        goal="实现功能，补充",
        acceptance=["功能可用"],
        user_source=h.source("T001", 1, "change", "补充"),
    )
    third = brief(h, "T001")
    assert f"{p_new} 已被替代，现在有效的是 {newest}（global）：“画图只出 SVG”" in third
    # Adding nothing new does not rewrite the brief.
    again = h.run("attempt_continue", task_id="T001", read_set=["T002"])
    assert "brief_rewritten" not in again["events"][0]["data"]


@pytest.mark.use_case("UC-CONTEXT-006")
def test_continuing_brings_the_brief_up_to_a_revoked_preference(home: Home) -> None:
    """Review r2 F3: the only added preference is revoked; continuing with nothing new still
    rewrites the brief so the worker sees it."""
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    only = record(h, "global", "报告附截图", topic="报告")
    h.run("task_dispatch", task_id="T001", workdir="~/wt/T001", user_turn=True, read_set=[only])
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
        receipt={"model": "claude-opus-5-5[1m]"},
    )
    unchanged = h.run("attempt_continue", task_id="T001")
    assert "brief_rewritten" not in unchanged["events"][0]["data"]
    h.run("preference_revoke", ref=only, reason="不需要了", user_source=revoke_said(only, "m-r"))
    out = h.run("attempt_continue", task_id="T001")
    assert out["events"][0]["data"]["brief_rewritten"] == {
        "path": "runs/T001/a1/brief.md",
        "preferences": [],
    }
    assert f"{only} 已撤销（理由：不需要了）" in brief(h, "T001")


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_refused_continuation_leaves_the_brief_alone(home: Home) -> None:
    """Review r2 F4: every check that can refuse comes before the brief is written."""
    h = Harness(home)
    worker = h.complete("T001")
    assert worker is not None
    before = brief(h, "T001")
    added = record(h, "global", "报告附截图", topic="报告")
    assert reason_of(h, "attempt_continue", task_id="T001", read_set=[added]) == "invalid_state"
    assert brief(h, "T001") == before
    assert [e for e in h.ledger.storage.read(since=0) if e.type == "attempt.continued"] == []


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_preference_with_line_breaks_stays_on_one_brief_line(home: Home) -> None:
    """Review r3 F5: after a multi-line preference, one added on continuation still shows."""
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    first = record(h, "global", "报告要求：\n附截图", topic="报告")
    h.run("task_dispatch", task_id="T001", workdir="~/wt/T001", user_turn=True, read_set=[first])
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
        receipt={"model": "claude-opus-5-5[1m]"},
    )
    assert f"{first}（global）：“报告要求： 附截图”" in brief(h, "T001")
    second = record(h, "global", "图只出 SVG", topic="图")
    out = h.run("attempt_continue", task_id="T001", read_set=[second])
    assert out["events"][0]["data"]["brief_rewritten"]["preferences"] == [second]
    line = next(x for x in brief(h, "T001").splitlines() if x.startswith("- 派发时补充的适用偏好"))
    assert f"{first}（global）" in line and f"{second}（global）：“图只出 SVG”" in line
