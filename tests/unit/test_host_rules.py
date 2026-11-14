from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tsw.home import Home
from tsw.host.guard import (
    bash_problem,
    owned_reason,
    owned_top,
    tsw_owned_path,
    worker_start_text,
)
from tsw.host.identity import Identity, within

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
        # An unquoted command substitution is read too, and stays in its word (T279).
        ("echo $(git push origin HEAD)", False, False, True, "推送授权"),
        ("x=$(git rev-parse HEAD) git push origin HEAD", False, False, True, "推送授权"),
        ("echo $(git log -1 --format=%s (x)) done", False, False, True, None),
        # One inside double quotes is quoted text, as before: a heredoc in it may hold quotes.
        (
            "git commit -m \"$(cat <<'EOF'\nit's (x\nEOF\n)\" && git push",
            False,
            False,
            True,
            "推送",
        ),
        ("echo $(unclosed && git push", False, False, True, "推送授权"),
        # A heredoc in an unquoted substitution may hold any ) or quote (PR #45 review).
        (
            "body=$(cat <<'EOF'\n1) Don't change the target.\nEOF\n)\ngit push origin HEAD",
            False,
            False,
            True,
            "推送授权",
        ),
        ("x=$(cat <<-EOF\n\t(a\n\tEOF\n) git push origin HEAD", False, False, True, "推送授权"),
        ("x=$(cat <<< ')') ; git push origin HEAD", False, False, True, "推送授权"),
        ("body=$(cat <<'EOF'\nonly text)\nEOF\n)\ngit status", False, False, True, None),
        ("git -c credential.username=me push origin HEAD", False, False, True, "推送授权"),
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
        ("python -- - <<'PY'\nimport tsw\nPY", True, True, True, "导入"),
        ("python -B -- tsw_launch.py guard Stop", True, True, True, "宿主"),
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


@pytest.mark.use_case("UC-MERGE-001")
@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("gh pr merge 3 --squash", False),  # gh's default remote is not a confirmed target
        ("cd ../other && gh pr merge 3 --squash", False),
        ("gh -R example/demo pr merge 3 --squash --delete-branch", False),  # host not named
        ("gh -R github.com/example/demo pr merge 3 --squash --delete-branch", True),
        ("gh pr merge https://github.com/Example/Demo/pull/3 --squash", True),
        ("gh api -X PUT repos/example/demo/pulls/3/merge", False),  # GH_HOST may redirect it
        ("gh -R example/other pr merge 3 --squash", False),
        ("gh pr merge --repo=example/other 3", False),
        ("gh pr merge https://github.com/example/other/pull/3", False),
        ("gh api -X PUT repos/example/other/pulls/3/merge", False),
        ("gh api graphql -f query='mutation{mergePullRequest(input:{})}'", False),
        ("gh pr merge 3 -sdRother/repo", False),
        ("gh pr merge 3 -sdR other/repo", False),
        ("gh pr merge https://ghe.example.org/other/repo/pull/3 --squash", False),
        ("gh pr merge https://ghe.example.org/example/demo/pull/3 --squash", False),
        ("GH_REPO=other/repo gh pr merge 3 --squash", False),
        ("gh pr merge 3 -R github.com/example/demo --body-file ../m.md -t 'a/b'", True),
        ("gh api --hostname ghe.example.org -X PUT repos/example/demo/pulls/3/merge", False),
        ("gh api --hostname=github.com -X PUT repos/example/demo/pulls/3/merge", True),
        ("gh pr merge 3 -R github.com/example/demo", True),
        # The gh-account skill's account prefix chooses the account, never the target (T279).
        (
            'GH_TOKEN="$(gh auth token --hostname github.com --user me)" '
            "gh pr merge 3 -R github.com/example/demo --squash",
            True,
        ),
        ("GH_TOKEN=$(gh auth token --user me) gh pr merge 3 -R github.com/example/demo", True),
        # The skill's fail-closed form: the || inside the substitution joins no other command.
        (
            'GH_TOKEN="$(gh auth token --hostname github.com --user me || echo none)" '
            "gh pr merge 3 -R github.com/example/demo --squash",
            True,
        ),
        (
            "GH_TOKEN=$(gh auth token --user me || echo none) "
            "gh pr merge 3 -R github.com/example/demo --squash",
            True,
        ),
        ('GH_TOKEN="$(gh auth token --user me || echo none)" gh pr merge 3 --squash', False),
        ("GH_TOKEN=$(gh auth token --user me) gh pr merge 3 --squash", False),
        ("GH_TOKEN=$(gh auth token --user me) gh pr merge 3 -R example/other", False),
        ('GH_CONFIG_DIR="/u/gh/me" gh pr merge 3 -R github.com/example/demo --squash', True),
        ("GH_TOKEN=$(gh auth token --hostname ghe.example.org) gh pr merge 3 --squash", False),
    ],
)
def test_a_worker_merges_only_its_own_repository_with_the_authorization(
    tmp_path: Path, command: str, allowed: bool
) -> None:
    """Decision 0002: delivery.merge lets the worker merge its own PR, in its task's repository."""
    home = Home(tmp_path / "home")
    refused = bash_problem(command, home=home, push_allowed=True, pr_allowed=True, worker=True)
    assert refused is not None and "delivery.merge" in refused
    problem = bash_problem(
        command,
        home=home,
        push_allowed=True,
        pr_allowed=True,
        merge_allowed=True,
        merge_repository="example/demo",
        worker=True,
    )
    assert (problem is None) is allowed, problem


@pytest.mark.use_case("UC-MERGE-001")
@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("gh api -X PUT repos/example/demo/pulls/3/merge", False),
        ("gh api --hostname ghe.example.org -X PUT repos/example/demo/pulls/3/merge", True),
        ("gh api --hostname github.com -X PUT repos/example/demo/pulls/3/merge", False),
        ("gh pr merge 3 -R example/demo", False),
        ("gh pr merge 3 -R ghe.example.org/example/demo", True),
        ("gh pr merge https://ghe.example.org/example/demo/pull/3", True),
        ("gh pr merge https://github.com/example/demo/pull/3", False),
        # A GitHub Enterprise Server host's account prefix (gh-account skill, T279).
        (
            'GH_ENTERPRISE_TOKEN="$(gh auth token --hostname ghe.example.org --user me '
            '|| echo none)" gh pr merge 3 -R ghe.example.org/example/demo --squash',
            True,
        ),
        (
            'GH_ENTERPRISE_TOKEN="$(gh auth token --hostname ghe.example.org --user me '
            '|| echo none)" gh pr merge 3 -R example/demo --squash',
            False,
        ),
    ],
)
def test_a_repository_on_another_host_must_be_named_with_its_host(
    tmp_path: Path, command: str, allowed: bool
) -> None:
    home = Home(tmp_path / "home")
    problem = bash_problem(
        command,
        home=home,
        push_allowed=True,
        pr_allowed=True,
        merge_allowed=True,
        merge_repository="example/demo",
        merge_host="ghe.example.org",
        worker=True,
    )
    assert (problem is None) is allowed, problem


def test_the_worker_instructions_copy_is_tsw_owned(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    copy = home.root / "roles" / "worker.md"
    assert tsw_owned_path(home, copy)
    reason = owned_reason(owned_top(home, copy) or "")
    assert "plugin/roles/" in reason and "tsw host render" in reason
    problem = bash_problem(
        f'echo x > "{copy}"', home=home, push_allowed=False, pr_allowed=False, worker=True
    )
    assert problem is not None and "roles/" in problem


@pytest.mark.use_case("UC-HOST-007")
def test_the_worker_start_text_is_only_about_the_session(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    root = tmp_path / "home" / "work" / "T001" / "a1"
    who = Identity(
        role="worker",
        session_id="w-1",
        cwd=root,
        task_id="T001",
        attempt="a1",
        phase="live",
        brief_path="runs/T001/a1/brief.md",
        writable_roots=(root,),
    )
    text = worker_start_text(who, home)
    assert "T001" in text and "runs/T001/a1/brief.md" in text and str(root) in text
    assert "提示语开头" in text and "roles" in text and "待命中" not in text
    assert "待命中" in worker_start_text(replace(who, phase="standby"), home)
    waiting = worker_start_text(replace(who, brief_path=None), home)
    assert "还没有任务书" in waiting and "不要开始工作" in waiting
