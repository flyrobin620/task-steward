from __future__ import annotations

from pathlib import Path

import pytest

from tsw.home import Home
from tsw.host.guard import bash_problem, tsw_owned_path
from tsw.host.identity import within

pytestmark = pytest.mark.use_case("UC-HOST-002")


def test_within_is_path_aware(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    assert within(root, root)
    assert within(root / "a" / "b.txt", root)
    assert not within(tmp_path / "wt2" / "x", root)
    assert not within(tmp_path, root)


def test_tsw_owned_paths(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    assert tsw_owned_path(home, home.root / "ledger" / "events" / "x.jsonl")
    assert tsw_owned_path(home, home.root / "views" / "STATUS.html")
    assert not tsw_owned_path(home, home.root / "work" / "T001" / "a1" / "r.md")
    assert not tsw_owned_path(home, home.root / "CLAUDE.md")


@pytest.mark.parametrize(
    ("command", "push", "pr", "worker", "expected"),
    [
        ("git push origin HEAD", False, False, True, "推送授权"),
        ("git push origin HEAD", True, False, True, None),
        ("git -C repo push", False, False, False, None),  # the orchestrator is not limited here
        ("gh pr create --fill", False, False, True, "PR"),
        ("gh pr create --fill", True, True, True, None),
        ("gh pr merge 3", True, True, True, "PR"),
        ("gh.exe pr merge 3", True, True, True, "合并"),
        ('"C:/Program Files/GitHub CLI/gh.exe" pr merge 3', True, True, True, "合并"),
        ("gh api -X PUT repos/o/r/pulls/3/merge", True, True, True, "合并"),
        ("gh --repo owner/repo pr merge 1", True, True, True, "合并"),
        ("gh pr --repo owner/repo merge 1", True, True, True, "合并"),
        ("gh -R owner/repo pr create --fill", True, False, True, "create_pr"),
        ("gh pr view 3 --json mergeable", True, True, True, None),
        ("git.exe push origin HEAD", False, False, True, "推送授权"),
        ("/usr/bin/git -C repo -c a=b push", False, False, True, "推送授权"),
        ("echo done && GIT_TRACE=1 git push", False, False, True, "推送授权"),
        ("bash -c 'git push'", False, False, True, "推送授权"),
        ("git log --grep push", False, False, True, None),
        ("Get-FileHash C:/tools/app.exe", True, True, False, "哈希"),
        ("sha256sum report.md", True, True, True, None),
        ("git status", False, False, True, None),
        # The hook entry records the user's words: only the host runs it (DN006 §8.1a).
        ("echo '{}' | tsw guard UserPromptSubmit", True, True, True, "宿主调用"),
        (
            "python plugin/bin/tsw_launch.py guard UserPromptSubmit < p.json",
            True,
            True,
            False,
            "宿主",
        ),
        ("py -m tsw --home h guard Stop", True, True, True, "宿主"),
        ("python -mtsw guard UserPromptSubmit", True, True, True, "宿主"),
        ("python -B -mtsw.cli guard UserPromptSubmit", True, True, False, "宿主"),
        ("python -m tsw.__main__ guard UserPromptSubmit", True, True, True, "宿主"),
        ("python -mtsw tool run x", True, True, True, "tool run"),
        ("python -Bmtsw guard Stop", True, True, True, "宿主"),
        ("python -X utf8 -Bm tsw guard Stop", True, True, True, "宿主"),
        ("python -Ic 'import tsw'", True, True, True, "导入"),
        ("python -m pytest -k tsw", True, True, True, None),
        ("python script.py -m tsw guard", True, True, True, None),
    ],
)
def test_bash_rules(  # noqa: PLR0917 - one parameter per table column
    tmp_path: Path, command: str, push: bool, pr: bool, worker: bool, expected: str | None
) -> None:
    home = Home(tmp_path / "home")
    problem = bash_problem(command, home=home, push_allowed=push, pr_allowed=pr, worker=worker)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_writes_into_the_ledger_through_the_shell_are_refused(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    command = f'echo x > "{home.root / "ledger" / "events" / "a.jsonl"}"'
    problem = bash_problem(command, home=home, push_allowed=True, pr_allowed=True, worker=False)
    assert problem is not None and "ledger" in problem
    reading = f'type "{home.root / "views" / "STATUS.html"}"'
    assert (
        bash_problem(reading, home=home, push_allowed=True, pr_allowed=True, worker=False) is None
    )
