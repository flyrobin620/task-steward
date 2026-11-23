"""A repository task's worktree and the repository part of its brief (CC006 §8)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV, resolve
from tsw.host.mcp import Server
from tsw.review import run as review_run
from tsw.review.codex import ReviewRun

pytestmark = pytest.mark.use_case("UC-DISPATCH-002")


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout(tmp_path: Path) -> Path:
    """A local checkout whose only remote ``origin`` has ``main`` with one commit."""
    origin, repo = tmp_path / "origin.git", tmp_path / "src" / "demo"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@example.com"),
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
    ):
        git(repo, "config", key, value)
    (repo / "AGENTS.md").write_text("# demo\n", encoding="utf-8")
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


def harness(home: Home, repo: Path, **extra: object) -> Harness:
    return Harness(home, {"repositories": {"demo": {"path": str(repo), "aliases": ["d"], **extra}}})


def skill(home: Home, name: str, description: str) -> None:
    path = home.root / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n正文\n", encoding="utf-8")


def test_prestart_creates_the_worktree_and_the_session_starts_in_it(
    home: Home, tmp_path: Path
) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    skill(home, "weekly-update-email", "周报邮件")
    skill(home, "orchestrate", "主会话流程")
    skill(home, "task-intake", "受理与写任务书")
    skill(home, "gh-account", "按仓库选用 gh 账号")
    h.register("T001")
    h.plan("T001")
    orchestrator = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    orchestrator.machine = "m1"
    started = orchestrator.call(
        "attempt_start", {"task_id": "T001", "purpose": "prestart", "user_turn": True}
    )
    worktree = h.home.root / "work" / "T001" / "a1" / "demo"
    data = started["events"][0]["data"]
    assert data["worktree"] == {
        "repo": "demo",
        "path": str(worktree),
        "branch": "tsw/t001-a1",
        "base": data["worktree"]["base"],
        "created": True,
        "fetched": True,
    }
    # git 2.48+ records the remote's HEAD on fetch; older versions leave origin/main first.
    assert data["worktree"]["base"] in ("origin/HEAD", "origin/main")
    assert data["workdir"] == "work/T001/a1"  # the execution directory is still the attempt's
    # start_session is told the worktree, spelled as on disk.
    assert started["cwd"] == os.path.realpath(worktree)
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tsw/t001-a1"
    assert (worktree / "AGENTS.md").is_file()
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "## 仓库与 worktree" in brief
    assert f"会话目录：`{worktree}`" in brief and "分支 `tsw/t001-a1`" in brief
    assert "`AGENTS.md`、`CLAUDE.md`、`.github/copilot-instructions.md`" in brief
    assert "报告中写明读了哪些" in brief
    # The data directory's skills are listed with their paths; the orchestrator's are not.
    assert "`weekly-update-email`：周报邮件" in brief and "`orchestrate`" not in brief
    # gh-account is for every session; the intake rules are the main session's (T279).
    assert "`gh-account`：按仓库选用 gh 账号" in brief and "`task-intake`" not in brief
    assert brief.index("## 仓库与 worktree") < brief.index("## 目标")
    # The user's language from control.language (default zh-CN) is written into the brief (T279).
    assert "- 语言：与用户交流（会话中的答复与提问、`ask_user` 的问题、报告）使用 简体中文" in brief
    assert brief.index("- 语言：") < brief.index("- 交付：")
    # The worker bound to the attempt works in the worktree: its directory is consistent.
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
    )
    with h.ledger.view() as conn:
        who = resolve(conn, h.home, {SESSION_ENV: "w-1"}, worktree)
    assert (who.role, who.cwd_consistent) == ("worker", True)


def test_a_later_generation_and_dispatch_reuse_the_worktree(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    first = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    worktree = Path(first["events"][0]["data"]["worktree"]["path"])
    (worktree / "work.txt").write_text("in progress\n", encoding="utf-8")
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=1,
        outcome="not_created",
    )
    again = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    made = again["events"][0]["data"]["worktree"]
    assert (made["path"], made["created"], made["base"]) == (str(worktree), False, None)
    assert (worktree / "work.txt").is_file()


def test_an_existing_branch_gets_a_new_worktree(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    git(repo, "branch", "tsw/t001-a1")
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    made = out["events"][0]["data"]["worktree"]
    assert (made["created"], made["base"]) == (True, None)
    assert git(Path(made["path"]), "rev-parse", "--abbrev-ref", "HEAD") == "tsw/t001-a1"


def test_configured_instructions_and_base_are_used(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = harness(
        home, repo, instructions=["AGENTS.md", ".github/copilot-instructions.md"], base_ref="main"
    )
    h.register("T001")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert out["events"][0]["data"]["worktree"]["base"] == "main"
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "`AGENTS.md`、`.github/copilot-instructions.md`（不存在的跳过）" in brief
    assert "`CLAUDE.md`" not in brief


def test_something_else_at_the_worktree_path_refuses_the_start(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    clone = h.home.root / "work" / "T001" / "a1" / "demo"
    clone.mkdir(parents=True)
    (clone / "stray.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(TswError) as refused:
        h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert refused.value.details["reason"] == "worktree_conflict"
    assert h.query("SELECT * FROM attempts WHERE task_id = 'T001'") == []
    # A base that does not exist is refused the same way, before anything is recorded.
    h2 = harness(home, repo, base_ref="no-such-branch")
    h2.register("T002")
    h2.plan("T002")
    with pytest.raises(TswError) as failed:
        h2.run("attempt_start", task_id="T002", purpose="prestart", user_turn=True)
    assert failed.value.details["reason"] == "worktree_unavailable"
    assert h2.query("SELECT * FROM attempts WHERE task_id = 'T002'") == []


def test_without_a_checkout_the_session_starts_in_the_execution_directory(
    home: Home, tmp_path: Path
) -> None:
    h = harness(home, tmp_path / "missing")
    skill(home, "weekly-update-email", "周报邮件")
    h.register("T001")
    h.plan("T001")
    orchestrator = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    orchestrator.machine = "m1"
    started = orchestrator.call(
        "attempt_start", {"task_id": "T001", "purpose": "prestart", "user_turn": True}
    )
    assert "worktree" not in started["events"][0]["data"]
    assert started["cwd"] == os.path.realpath(h.home.root / "work" / "T001" / "a1")
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "tsw 没有为仓库 demo 创建 worktree" in brief and "不存在" in brief
    assert "仓库指令文件" in brief
    # The session is in the data directory's tree, where the host loads its skills itself.
    assert "weekly-update-email" not in brief


@pytest.mark.use_case("UC-REVIEW-002")
def test_the_worker_is_reviewed_in_its_worktree(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout(tmp_path)
    h = Harness(
        home,
        {
            "repositories": {"demo": {"path": str(repo)}},
            "roles": {"reviewer": {"executable": sys.executable}},
        },
    )
    h.register("T001")
    h.plan("T001")
    h.run("task_dispatch", task_id="T001", workdir="work/T001/a1", user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    data = started["events"][0]["data"]
    worktree = Path(data["worktree"]["path"])
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    base = git(worktree, "rev-parse", "HEAD")
    (worktree / "b.txt").write_text("b\n", encoding="utf-8")
    git(worktree, "add", "b.txt")
    git(worktree, "commit", "-q", "-m", "work")
    head = git(worktree, "rev-parse", "HEAD")
    calls: list[dict[str, object]] = []

    def reviewer(prompt: str, **options: object) -> ReviewRun:
        calls.append({"prompt": prompt, **options})
        answer = {"verdict": "pass", "summary": "s", "reviewed_head": head, "findings": []}
        return ReviewRun(
            thread_id="thread-1",
            status="completed",
            final_message=json.dumps(answer),
            model=str(options["model"]),
        )

    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", reviewer)
    out = review_run.run_round(h.home, {SESSION_ENV: "w-1"}, worktree, machine="m1")
    # The round's scope comes from the worktree's commits: no head had to be named.
    assert out["reviewed_head"] == head
    assert calls[0]["cwd"] == worktree
    assert f"git diff {base}..{head}" in str(calls[0]["prompt"])


def test_a_repository_id_that_is_not_a_plain_name_gets_no_worktree(
    home: Home, tmp_path: Path
) -> None:
    repo = checkout(tmp_path)
    h = Harness(home, {"repositories": {"../escape": {"path": str(repo)}}})
    h.register("T001", repo="../escape")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert "worktree" not in out["events"][0]["data"]
    assert not (h.home.root / "work" / "T001" / "escape").exists()
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "不能用作执行目录下的目录名" in brief


def test_a_relative_checkout_path_is_relative_to_the_data_directory(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the process runs somewhere else
    relative = Path(os.path.relpath(repo, home.root))
    assert not relative.is_absolute()
    h = harness(home, relative)
    h.register("T001")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert out["events"][0]["data"]["worktree"]["created"] is True


def test_a_dotted_repository_id_keeps_its_settings(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = Harness(
        home,
        {
            "repositories": {
                "my.repo": {"path": str(repo), "aliases": ["mr"], "instructions": ["RULES.md"]}
            }
        },
    )
    h.register("T001", repo="mr")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    made = out["events"][0]["data"]["worktree"]
    assert Path(made["path"]) == h.home.root / "work" / "T001" / "a1" / "my.repo"
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "`RULES.md`（不存在的跳过）" in brief


def test_a_clone_of_another_repository_on_the_task_branch_is_refused(
    home: Home, tmp_path: Path
) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    other = h.home.root / "work" / "T001" / "a1" / "demo"
    other.mkdir(parents=True)
    git(other, "init", "-q", "-b", "tsw/t001-a1")
    git(
        other,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    with pytest.raises(TswError) as refused:
        h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert refused.value.details["reason"] == "worktree_conflict"
    assert h.query("SELECT * FROM attempts WHERE task_id = 'T001'") == []


@pytest.mark.parametrize("base_ref", ["main^{commit}", "main@{0}"])
def test_a_configured_base_is_used_as_written(home: Home, tmp_path: Path, base_ref: str) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo, base_ref=base_ref)
    h.register("T001")
    h.plan("T001")
    out = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert out["events"][0]["data"]["worktree"]["base"] == base_ref


def test_a_redirected_worktree_path_is_refused(home: Home, tmp_path: Path) -> None:
    """A link (a junction on Windows) at the worktree path would lead outside the execution
    directory, even to a worktree of the same repository on the task's branch."""
    repo = checkout(tmp_path)
    outside = tmp_path / "outside"
    git(repo, "worktree", "add", "-q", "-b", "tsw/t001-a1", str(outside), "main")
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    link = h.home.root / "work" / "T001" / "a1" / "demo"
    link.parent.mkdir(parents=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TswError) as refused:
        h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert refused.value.details["reason"] == "worktree_conflict"
    assert h.query("SELECT * FROM attempts WHERE task_id = 'T001'") == []


@pytest.mark.parametrize(
    ("language", "line"),
    [
        ("en-US", "使用 English（`en-US`）（配置 `control.language`）"),
        ("fr", "使用 `fr`（配置 `control.language`）"),
        ("", None),
    ],
)
def test_the_brief_names_the_language_the_worker_talks_to_the_user_in(
    home: Home, tmp_path: Path, language: str, line: str | None
) -> None:
    """DN003 §3.5a: the user's preferred language, as configured, or no line when unset."""
    repo = checkout(tmp_path)
    h = Harness(
        home,
        {"control": {"language": language}, "repositories": {"demo": {"path": str(repo)}}},
    )
    h.register("T001")
    h.plan("T001")
    orchestrator = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    orchestrator.machine = "m1"
    orchestrator.call(
        "attempt_start", {"task_id": "T001", "purpose": "prestart", "user_turn": True}
    )
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    if line is None:
        assert "- 语言：" not in brief
    else:
        assert line in brief


GH_LINE = "- gh 账号："


@pytest.mark.parametrize(
    ("repo_extra", "accounts", "rendered", "expected"),
    [
        (
            {"repository": "octo/demo", "auth_profile": "gh-octo"},
            {"gh-octo": {"provider": "github", "host": "github.com", "account": "octo"}},
            True,
            "仓库账号为 `octo`（github.com，配置 `repositories.demo.auth_profile` 为 `gh-octo`）",
        ),
        ({"repository": "octo/demo"}, {}, True, "配置中没有为仓库 demo 指定账号"),
        ({"repository": "octo/demo"}, {}, False, "先确定仓库账号"),
        (
            {"provider": "azure-devops", "repository": "org/proj/demo", "auth_profile": "ado"},
            {"ado": {"provider": "azure-devops", "host": "dev.azure.com", "account": "me"}},
            True,
            None,
        ),
        (  # no provider on the repository, but its account is Azure DevOps (review r8)
            {"repository": "org/proj/demo", "auth_profile": "ado"},
            {"ado": {"provider": "azure-devops", "host": "dev.azure.com", "account": "me"}},
            True,
            None,
        ),
        ({}, {}, True, None),  # no owner/name: not known to be on GitHub
    ],
)
def test_the_brief_names_the_repository_gh_account(  # noqa: PLR0917 - one per table column
    home: Home,
    tmp_path: Path,
    repo_extra: dict[str, str],
    accounts: dict[str, object],
    rendered: bool,
    expected: str | None,
) -> None:
    """CC006 §8.3 (T279, F-T279-1): the account from auth_profile, or how to find it, with the
    gh-account skill's path once the host integration rendered it."""
    repo = checkout(tmp_path)
    skill_path = home.root / ".claude" / "skills" / "gh-account" / "SKILL.md"
    if rendered:
        skill(home, "gh-account", "按仓库选用 gh 账号")
    h = Harness(
        home,
        {"accounts": accounts, "repositories": {"demo": {"path": str(repo), **repo_extra}}},
    )
    h.register("T001")
    h.plan("T001")
    orchestrator = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    orchestrator.machine = "m1"
    orchestrator.call(
        "attempt_start", {"task_id": "T001", "purpose": "prestart", "user_turn": True}
    )
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    if expected is None:
        assert GH_LINE not in brief
        return
    line = next(x for x in brief.splitlines() if x.startswith(GH_LINE))
    assert expected in line and "不用 `gh auth switch`" in line
    assert (f"`{skill_path}`" in line) is rendered
    assert brief.index(GH_LINE) < brief.index("## 目标")


def _dispatch(h: Harness, task_id: str, **given: object) -> dict[str, Any]:
    """T07 and attempt_start (dispatch), bound to a worker session; returns the start's data."""
    h.run("task_dispatch", task_id=task_id, user_turn=True, **given)
    started = h.run("attempt_start", task_id=task_id, purpose="dispatch", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id=task_id,
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref=f"w-{task_id}-{data['attempt']}",
        brief_delivered=True,
    )
    return dict(data)


def _lose(h: Harness, task_id: str, attempt: str) -> None:
    """T15: the session is gone; the attempt is replaced and the task is ready again."""
    h.run("attempt_update", task_id=task_id, attempt=attempt, event="lost", evidence="不存在")


@pytest.mark.transition("T15")
def test_a_new_attempt_takes_over_a_replaced_attempts_worktree(home: Home, tmp_path: Path) -> None:
    """DN002 §5.5, CC006 §8.3 (T344): a2 keeps the worktree, branch and uncommitted work of a1."""
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    worktree = Path(_dispatch(h, "T001")["worktree"]["path"])
    # The work of a1: a commit on its branch, a changed file and an untracked one.
    (worktree / "done.txt").write_text("committed\n", encoding="utf-8")
    git(worktree, "add", "done.txt")
    git(worktree, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "a1")
    head = git(worktree, "rev-parse", "HEAD")
    (worktree / "AGENTS.md").write_bytes(b"# demo\nchanged by a1\n")
    (worktree / "draft.txt").write_text("untracked\n", encoding="utf-8")
    _lose(h, "T001", "a1")
    assert h.status("T001") == "ready"

    second = _dispatch(h, "T001")  # no workdir: the directory of a1 is taken over
    assert (second["attempt"], h.task("T001")["current_attempt"]) == ("a2", "a2")
    rows = h.query("SELECT attempt, workdir FROM attempts WHERE task_id = 'T001' ORDER BY attempt")
    assert [(r["attempt"], r["workdir"]) for r in rows] == [
        ("a1", "work/T001/a1"),
        ("a2", "work/T001/a1"),
    ]
    assert second["worktree"] == {
        "repo": "demo",
        "path": str(worktree),
        "branch": "tsw/t001-a1",
        "base": None,
        "created": False,
        "fetched": False,
        "taken_over": "a1",
    }
    # Nothing was switched, fetched or cleaned: the commit and uncommitted work of a1 are there.
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tsw/t001-a1"
    assert git(worktree, "rev-parse", "HEAD") == head
    assert (worktree / "AGENTS.md").read_bytes() == b"# demo\nchanged by a1\n"
    assert (worktree / "draft.txt").is_file()
    assert "tsw/t001-a2" not in git(repo, "branch", "--list")
    brief = (h.home.root / "runs" / "T001" / "a2" / "brief.md").read_text(encoding="utf-8")
    assert "- 执行目录：work/T001/a1" in brief
    assert f"会话目录：`{worktree}`" in brief and "分支 `tsw/t001-a1`" in brief
    assert "接手 a1 的执行目录" in brief and "未提交的改动原样保留" in brief
    # The worker of a2 works in the worktree it took over.
    with h.ledger.view() as conn:
        who = resolve(conn, h.home, {SESSION_ENV: "w-T001-a2"}, worktree)
    assert (who.role, who.cwd_consistent) == ("worker", True)


def test_the_branch_taken_over_is_the_one_the_worktree_is_on(home: Home, tmp_path: Path) -> None:
    """a1 moved to another branch (a later PR); a2 and then a3 keep that branch."""
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    worktree = Path(_dispatch(h, "T001")["worktree"]["path"])
    git(worktree, "switch", "-q", "-c", "fix-ci")
    (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
    _lose(h, "T001", "a1")
    assert _dispatch(h, "T001")["worktree"]["branch"] == "fix-ci"
    _lose(h, "T001", "a2")
    made = _dispatch(h, "T001")["worktree"]
    assert (made["branch"], made["taken_over"], made["created"]) == ("fix-ci", "a2", False)
    assert (worktree / "wip.txt").is_file()
    brief = (h.home.root / "runs" / "T001" / "a3" / "brief.md").read_text(encoding="utf-8")
    assert "分支 `fix-ci`" in brief and "接手 a2 的执行目录" in brief


def test_a_given_directory_is_not_taken_over(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    _dispatch(h, "T001")
    _lose(h, "T001", "a1")
    made = _dispatch(h, "T001", workdir="work/T001/a2")["worktree"]
    assert (made["branch"], made["created"]) == ("tsw/t001-a2", True)
    assert "taken_over" not in made
    brief = (h.home.root / "runs" / "T001" / "a2" / "brief.md").read_text(encoding="utf-8")
    assert "接手" not in brief


def test_a_detached_worktree_is_not_taken_over(home: Home, tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    worktree = Path(_dispatch(h, "T001")["worktree"]["path"])
    git(worktree, "switch", "-q", "--detach")
    _lose(h, "T001", "a1")
    h.run("task_dispatch", task_id="T001", user_turn=True)
    with pytest.raises(TswError) as refused:
        h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    assert refused.value.details["reason"] == "worktree_conflict"
    rows = h.query("SELECT phase FROM attempts WHERE task_id = 'T001' AND attempt = 'a2'")
    assert [r["phase"] for r in rows] == ["planned"]
    brief_path = h.home.root / "runs" / "T001" / "a2" / "brief.md"
    assert "分支 `tsw/t001-a2`" in brief_path.read_text(encoding="utf-8")  # as dispatched
    # Back on its branch, the worktree is taken over, and the brief says so (review r1 F1).
    git(worktree, "switch", "-q", "tsw/t001-a1")
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    made = started["events"][0]["data"]["worktree"]
    assert (made["branch"], made["taken_over"]) == ("tsw/t001-a1", "a1")
    brief = brief_path.read_text(encoding="utf-8")
    assert "分支 `tsw/t001-a1`" in brief and "接手 a1 的执行目录" in brief
    assert "tsw/t001-a2" not in brief


def test_another_tasks_directory_is_not_taken_over(home: Home, tmp_path: Path) -> None:
    """A directory an attempt of T001 recorded is not for T002 to take over, even when given."""
    repo = checkout(tmp_path)
    h = harness(home, repo)
    for task_id in ("T001", "T002"):
        h.register(task_id)
        h.plan(task_id)
    _dispatch(h, "T001")
    _lose(h, "T001", "a1")
    h.run("task_dispatch", task_id="T002", user_turn=True, workdir="work/T001/a1")
    with pytest.raises(TswError) as refused:
        h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    assert refused.value.details["reason"] == "worktree_conflict"


def test_a_prestart_takes_over_a_lost_standby_directory(home: Home, tmp_path: Path) -> None:
    """A standby session lost (replaced, DN002 §5.6) is prestarted again in the same directory."""
    repo = checkout(tmp_path)
    h = harness(home, repo)
    h.register("T001")
    h.plan("T001")
    first = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=first["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-standby",
    )
    h.run("attempt_update", task_id="T001", attempt="a1", event="lost", evidence="不存在")
    rows = h.query("SELECT phase FROM attempts WHERE task_id = 'T001' AND attempt = 'a1'")
    assert [r["phase"] for r in rows] == ["replaced"]
    made = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    data = made["events"][0]["data"]
    assert (data["attempt"], data["workdir"]) == ("a2", "work/T001/a1")
    assert (data["worktree"]["branch"], data["worktree"]["taken_over"]) == ("tsw/t001-a1", "a1")
    # A generation that was not created is retried in the same directory, not a new one.
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a2",
        generation=data["generation"],
        outcome="not_created",
    )
    retried = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    again = retried["events"][0]["data"]
    assert (again["attempt"], again["generation"], again["workdir"]) == ("a2", 2, "work/T001/a1")
    assert again["worktree"]["taken_over"] == "a1"
    assert not (h.home.root / "work" / "T001" / "a2").exists()
