"""A development task's worker merges its own PR before submitting; the orchestrator checks after
the merge (DN002 G-SUBMIT / G-ACCEPT, decision 0002)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import TswError
from tsw.home import Home, workdir_path
from tsw.host.guard import Guard
from tsw.machine.base import Caller

pytestmark = pytest.mark.use_case("UC-MERGE-001")
PR = "https://example.com/pr/1"


def reason_of(call: Callable[[], Any]) -> tuple[str, str | None]:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason")), exc.value.details.get("check")


def harness(home: Home, merge: bool, kind: str = "code") -> tuple[Harness, Caller]:
    delivery = {"push": True, "create_pr": True, **({"merge": True} if merge else {})}
    repo = {"path": "~/src/demo", "repository": "example/demo", "delivery": delivery}
    h = Harness(home, {"repositories": {"demo": repo}})
    h.register("T001", kind=kind)
    h.plan("T001")
    worker = h.dispatch("T001")
    h.finalize(worker, "T001", head="h1")
    return h, worker


def submit(h: Harness, worker: Caller, **extra: Any) -> dict[str, Any]:
    attempt = h.task("T001")["current_attempt"]
    draft = h.home.root / "work" / "T001" / attempt
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "result.md").write_text("# 报告", encoding="utf-8")
    (draft / "result.html").write_text("<h1>报告</h1>", encoding="utf-8")
    return h.run(
        "submit",
        worker,
        task_id="T001",
        head="h1",
        report_md=f"work/T001/{attempt}/result.md",
        report_html=f"work/T001/{attempt}/result.html",
        worktree_clean=True,
        **extra,
    )


def accept(h: Harness, **extra: Any) -> dict[str, Any]:
    return h.run(
        "task_accept",
        task_id="T001",
        verified_head="h1",
        summary="验收通过",
        acceptance=[
            {"item": "功能可用", "status": "done", "evidence": "测试"},
            {"item": "测试通过", "status": "done", "evidence": "CI"},
        ],
        **extra,
    )


def link(h: Harness, ref: str = PR) -> None:
    h.run(
        "output_link",
        task_id="T001",
        output_id="pr-1",
        type="pr",
        ref=ref,
        role="current",
        evidence=["gh pr view: MERGED"],
        verified_at="2026-11-05T00:00:00Z",
    )


@pytest.mark.transition("T13")
@pytest.mark.transition("T18")
@pytest.mark.transition("G-SUBMIT")
@pytest.mark.transition("G-ACCEPT")
def test_the_worker_merges_and_the_orchestrator_checks_after_the_merge(home: Home) -> None:
    h, w = harness(home, merge=True)
    brief = home.path("runs/T001/a1/brief.md")
    assert "worker 在门禁、评审与意见都解决后自行合并" in brief.read_text(encoding="utf-8")
    assert reason_of(lambda: submit(h, w)) == ("g_submit", "merge")
    merged = {"pr": PR, "merge_commit": "m1", "merged_head": "h0"}
    assert reason_of(lambda: submit(h, w, merge=merged)) == ("g_submit", "merged_head")
    out = submit(h, w, merge={**merged, "merged_head": "h1"})
    submission = out["events"][0]["data"]["submission"]
    assert submission["merge"] == {"pr": PR, "merge_commit": "m1", "merged_head": "h1"}
    assert h.status("T001") == "verifying"
    link(h, ref="https://example.com/pr/2")
    assert reason_of(lambda: accept(h)) == ("g_accept", "merged_pr")
    link(h)
    assert reason_of(lambda: accept(h)) == ("g_accept", "post_merge")
    accepted = accept(h, post_merge="main 上 m1 的 CI 通过")
    assert accepted["events"][0]["data"]["post_merge"] == "main 上 m1 的 CI 通过"
    review = home.path("runs/T001/a1/review.md").read_text(encoding="utf-8")
    assert "## 合并后核对" in review and "m1" in review


@pytest.mark.transition("G-SUBMIT")
def test_the_brief_or_repository_rules_may_keep_the_merge_with_others(home: Home) -> None:
    h, w = harness(home, merge=True)
    out = submit(h, w, merge_skipped="任务书要求由主会话合并")
    assert out["events"][0]["data"]["submission"]["merge_skipped"] == "任务书要求由主会话合并"
    link(h)
    accept(h)  # merged by others: no post-merge check is required here


@pytest.mark.transition("G-SUBMIT")
def test_without_the_authorization_the_worker_does_not_merge(home: Home) -> None:
    h, w = harness(home, merge=False)
    brief = home.path("runs/T001/a1/brief.md").read_text(encoding="utf-8")
    assert "合并：worker 不合并" in brief
    merged = {"pr": PR, "merge_commit": "m1", "merged_head": "h1"}
    assert reason_of(lambda: submit(h, w, merge=merged)) == ("g_submit", "merge_authorized")
    submit(h, w)


def test_only_development_tasks_merge(home: Home) -> None:
    h, w = harness(home, merge=True, kind="research")
    submit(h, w)
    assert h.status("T001") == "verifying"


def bash(h: Harness, worker: Caller, command: str) -> dict[str, Any] | None:
    """The real PreToolUse hook for the worker's shell command."""
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    payload = {
        "cwd": str(workdir_path(h.home, "~/wt/T001")),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    return Guard(h.home, env, payload).run("PreToolUse")


@pytest.mark.parametrize("kind", ["research", "design"])
def test_the_hook_refuses_merging_for_other_task_kinds(home: Home, kind: str) -> None:
    h, w = harness(home, merge=True, kind=kind)
    out = bash(h, w, "gh pr merge 3 --squash")
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hook_lets_a_development_worker_merge_only_its_repository(home: Home) -> None:
    h, w = harness(home, merge=True)
    assert bash(h, w, "gh pr merge 3 --squash") is None
    out = bash(h, w, "gh -R example/other pr merge 3 --squash")
    assert (
        out is not None and "example/other" in out["hookSpecificOutput"]["permissionDecisionReason"]
    )
