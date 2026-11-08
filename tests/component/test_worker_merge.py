"""In a repository that lets the worker merge, the worker merges its own PR, waits for the merge
commit's CI and submits with the evidence; the orchestrator checks the evidence (DN002 §3.5,
G-SUBMIT / G-ACCEPT, decision 0003)."""

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
RUN = "https://example.com/actions/runs/7"
CI = {"commit": "m1", "conclusion": "success", "url": RUN}
LIVE = "host-e2e 在最终 head 上通过（运行 r1）"
MERGED = {"pr": PR, "merge_commit": "m1", "merged_head": "h1", "ci": CI, "live": LIVE}


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
    brief_text = brief.read_text(encoding="utf-8")
    assert "等主分支上合并提交的 CI 通过" in brief_text and "- live：" in brief_text
    assert reason_of(lambda: submit(h, w)) == ("g_submit", "merge")
    assert reason_of(lambda: submit(h, w, merge={**MERGED, "merged_head": "h0"})) == (
        "g_submit",
        "merged_head",
    )
    out = submit(h, w, merge=MERGED)
    submission = out["events"][0]["data"]["submission"]
    assert submission["merge"] == MERGED
    assert h.status("T001") == "verifying"
    link(h, ref="https://example.com/pr/2")
    assert reason_of(lambda: accept(h)) == ("g_accept", "merged_pr")
    link(h)
    assert reason_of(lambda: accept(h)) == ("g_accept", "post_merge")
    accepted = accept(h, post_merge="已打开 CI 链接，m1 的 Gate 为绿")
    assert accepted["events"][0]["data"]["post_merge"] == "已打开 CI 链接，m1 的 Gate 为绿"
    review = home.path("runs/T001/a1/review.md").read_text(encoding="utf-8")
    assert "## 合并后核对" in review and "m1" in review
    assert RUN in review and LIVE in review


@pytest.mark.transition("G-SUBMIT")
@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"ci": None}, "merge_ci"),
        ({"ci": {"commit": "m1", "conclusion": "success"}}, "merge_ci"),
        ({"ci": {**CI, "commit": "h1"}}, "ci_commit"),
        ({"ci": {**CI, "conclusion": "in_progress"}}, "ci_pending"),
        ({"ci": {**CI, "conclusion": ""}}, "ci_pending"),
        ({"ci": {**CI, "conclusion": "failure"}}, "ci_failed"),
        ({"ci": {**CI, "conclusion": "cancelled"}}, "ci_failed"),
        ({"live": " "}, "merge"),
    ],
)
def test_the_worker_submits_only_after_the_merge_commits_ci_passed(
    home: Home, change: dict[str, Any], check: str
) -> None:
    h, w = harness(home, merge=True)
    merge = {k: v for k, v in {**MERGED, **change}.items() if v is not None}
    with pytest.raises(TswError) as exc:
        submit(h, w, merge=merge)
    assert (exc.value.details.get("reason"), exc.value.details.get("check")) == ("g_submit", check)
    # The refusal says what to do next: wait, or fix or revert within the task.
    if check == "ci_pending":
        assert "等它完成" in str(exc.value)
    if check == "ci_failed":
        assert "修复" in str(exc.value) and "revert" in str(exc.value)
    assert h.status("T001") == "in_progress"
    submit(h, w, merge={**MERGED, "ci": {**CI, "conclusion": "SUCCESS"}})
    assert h.status("T001") == "verifying"


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
    assert reason_of(lambda: submit(h, w, merge=MERGED)) == ("g_submit", "merge_authorized")
    submit(h, w)


@pytest.mark.transition("G-SUBMIT")
@pytest.mark.parametrize("kind", ["design", "research"])
def test_the_repository_not_the_task_kind_authorizes_the_merge(home: Home, kind: str) -> None:
    h, w = harness(home, merge=True, kind=kind)
    brief = home.path("runs/T001/a1/brief.md").read_text(encoding="utf-8")
    assert "worker 在门禁、评审与意见都解决后自行合并" in brief
    assert reason_of(lambda: submit(h, w)) == ("g_submit", "merge")
    submit(h, w, merge=MERGED)
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
@pytest.mark.parametrize("merge", [True, False])
def test_the_hook_follows_the_repository_whatever_the_task_kind(
    home: Home, kind: str, merge: bool
) -> None:
    h, w = harness(home, merge=merge, kind=kind)
    out = bash(h, w, "gh pr merge 3 -R github.com/example/demo --squash")
    if merge:
        assert out is None
    else:
        assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hook_lets_a_worker_merge_only_its_repository(home: Home) -> None:
    h, w = harness(home, merge=True)
    assert bash(h, w, "gh pr merge 3 -R github.com/example/demo --squash") is None
    assert bash(h, w, "gh pr merge 3 --squash") is not None
    out = bash(h, w, "gh -R example/other pr merge 3 --squash")
    assert (
        out is not None and "example/other" in out["hookSpecificOutput"]["permissionDecisionReason"]
    )
