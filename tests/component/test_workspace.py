"""A repository task's worktree and the repository part of its brief (CC006 §8)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
    assert brief.index("## 仓库与 worktree") < brief.index("## 目标")
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
