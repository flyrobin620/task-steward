"""Where a worker works on a repository task and what its brief tells it about the repository.

A task whose repository is configured with a local git checkout (``repositories.<id>.path``)
gets a git worktree of that checkout inside its execution directory,
``<workdir>/<repository id>``, and its session is started in that worktree. The host then loads
the repository's own configuration: skills, ``.mcp.json`` servers, project plugins, hooks,
settings and ``CLAUDE.md`` (CC006 §8). Two things it does not do, so the brief does them:

- it does not read the repository's ``AGENTS.md`` (the data directory above holds a
  ``CLAUDE.md``), so the brief lists the repository's instruction files
  (``repositories.<id>.instructions``) for the worker to read and follow;
- it does not load the data directory's skills (skill discovery stops at the repository root),
  so the brief lists them with their paths.

A repository without a usable checkout (no ``path``, a missing directory, not a git
repository) gets no worktree: the session starts in the execution directory as before.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tsw.config import EffectiveConfig, resolve_path, resolve_repository
from tsw.errors import rejected
from tsw.home import Home, workdir_path

DEFAULT_INSTRUCTIONS = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
# Where the work starts when the repository names no base_ref (the first that resolves).
BASE_CANDIDATES = ("{remote}/HEAD", "{remote}/main", "{remote}/master", "main", "master", "HEAD")
# The data directory's skills for the main session only (tsw host render writes them).
ORCHESTRATOR_SKILLS = ("orchestrate", "handle-input", "task-intake", "teams-chat")
# Where the host integration may render the skill on choosing a repository's gh account, relative
# to the data directory (Claude Code, CC006 §10.1). The brief points at it when it is there.
GH_ACCOUNT_SKILL = Path(".claude") / "skills" / "gh-account" / "SKILL.md"
GIT_TIMEOUT = 60


@dataclass(frozen=True)
class Workspace:
    """The repository part of a task's brief, and the worktree when there is one."""

    repo_id: str
    instructions: tuple[str, ...]
    checkout: Path | None  # the configured local checkout, when it is a git repository
    worktree: Path | None  # <workdir>/<repo_id>, when there is a checkout
    branch: str | None
    remote: str
    base_ref: str | None  # as configured; None: the first of BASE_CANDIDATES that resolves
    missing: str | None  # why there is no worktree
    # The repository's GitHub account (DN003 §3.4, ``auth_profile`` → ``accounts``):
    # (account, host, profile) when configured; ``github`` says whether the repository is on
    # GitHub at all, so that a brief can still say how to find the account.
    gh_account: tuple[str, str, str] | None = None
    github: bool = False


def plan(
    home: Home, config: EffectiveConfig, row: Any, attempt_id: str, workdir: str
) -> Workspace | None:
    """The workspace of the task in ``row``; None when its repository is not configured."""
    repo_id = resolve_repository(config, str(row["repo"] or ""))
    if repo_id is None:
        return None
    # Indexed by the ID itself: an ID may contain dots (``my.repo``), which config.get would
    # read as nesting.
    repositories: dict[str, Any] = config.get("repositories", {}) or {}
    repo: dict[str, Any] = repositories.get(repo_id) or {}
    instructions = tuple(str(p) for p in (repo.get("instructions") or DEFAULT_INSTRUCTIONS))
    remote = str(repo.get("remote") or "origin")
    base_ref = str(repo["base_ref"]) if repo.get("base_ref") else None
    raw = str(repo.get("path") or "")
    checkout = resolve_path(home, raw) if raw else None  # relative: to the data directory
    task_id = str(row["task_id"])
    branch = str(config.get("worktrees.branch_pattern")).format(
        task_id_lower=task_id.lower(), task_id=task_id, attempt=attempt_id
    )
    directory = workdir_path(home, workdir)
    worktree = directory / repo_id
    missing = None
    if checkout is None:
        missing = "配置中没有仓库路径（repositories.<id>.path）"
    elif not _plain_name(repo_id) or not _same(worktree.parent, directory):
        # The worktree must be a direct child of the execution directory, never elsewhere.
        missing = f"仓库 id {repo_id!r} 不能用作执行目录下的目录名"
    elif not checkout.is_dir():
        missing = f"仓库路径 {checkout} 不存在"
    elif _git(checkout, "rev-parse", "--git-dir")[0] != 0:
        missing = f"仓库路径 {checkout} 不是 git 仓库"
    elif _git(checkout, "check-ref-format", "--branch", branch)[0] != 0 or branch.startswith("-"):
        missing = f"分支名 {branch!r}（worktrees.branch_pattern）不是合法的分支名"
    account, github = _gh_account(config, repo)
    if missing is not None:
        return Workspace(
            repo_id, instructions, None, None, None, remote, base_ref, missing, account, github
        )
    return Workspace(
        repo_id, instructions, checkout, worktree, branch, remote, base_ref, None, account, github
    )


def _gh_account(
    config: EffectiveConfig, repo: dict[str, Any]
) -> tuple[tuple[str, str, str] | None, bool]:
    """The repository's GitHub account from its ``auth_profile``, and whether it is on GitHub
    (``provider: github``, or no provider but an ``owner/name``)."""
    provider = str(repo.get("provider") or "").lower()
    github = provider == "github" or (not provider and bool(repo.get("repository")))
    profile = str(repo.get("auth_profile") or "")
    accounts: dict[str, Any] = config.get("accounts", {}) or {}
    found: Any = accounts.get(profile) if profile else None
    if not isinstance(found, dict):
        return None, github
    entry = cast("dict[str, Any]", found)
    entry_provider = str(entry.get("provider") or "").lower()
    name = str(entry.get("account") or "")
    if entry_provider and entry_provider != "github":
        # The account names the provider explicitly: the repository is not on GitHub, whatever
        # its owner/name looks like.
        return None, False
    if entry_provider != "github" or not name:
        return None, github
    host = str(entry.get("host") or repo.get("host") or "github.com")
    return (name, host, profile), True


def _plain_name(name: str) -> bool:
    """A single path component that names the same directory on every platform."""
    bad = set('/\\:*?"<>|') | {chr(c) for c in range(32)}
    return (
        bool(name)
        and name not in (".", "..")
        and not (set(name) & bad)
        and name == name.strip(" .")
    )


def ensure(ws: Workspace) -> dict[str, Any]:
    """Create the worktree (idempotent) and return what is recorded about it.

    An existing worktree on the task's branch is used as it is (a later generation of the same
    attempt). Anything else at that path is refused, as is a git command that fails: the
    caller records nothing then.
    """
    assert ws.checkout is not None and ws.worktree is not None and ws.branch is not None
    path, branch = ws.worktree, ws.branch
    # Whatever is there must be the directory itself: a link or a Windows junction (which
    # is_symlink does not report) would put the worktree outside the execution directory.
    resolved = os.path.normcase(os.path.realpath(path))
    redirected = resolved != os.path.normcase(str(Path(os.path.realpath(path.parent)) / path.name))
    if path.is_symlink() or redirected or (path.exists() and not path.is_dir()):
        raise rejected("worktree 路径已有其他内容", reason="worktree_conflict", path=str(path))
    if path.exists():
        code, head, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        top = _git(path, "rev-parse", "--show-toplevel")[1]
        if (
            code == 0
            and head == branch
            and _same(Path(top), path)
            and _same(_common_dir(path), _common_dir(ws.checkout))  # the configured repository's
        ):
            return _record(ws, base=None, created=False, fetched=False)
        if any(path.iterdir()):
            raise rejected("worktree 路径已有其他内容", reason="worktree_conflict", path=str(path))
    fetched = _git(ws.checkout, "fetch", "--quiet", ws.remote)[0] == 0
    has_branch = (
        _git(ws.checkout, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0
    )
    if has_branch:
        args = ["worktree", "add", str(path), branch]
        base = None
    else:
        base = _base(ws)
        args = ["worktree", "add", "-b", branch, str(path), base]
    code, _, err = _git(ws.checkout, *args)
    if code != 0:
        raise rejected(
            "无法创建 worktree", reason="worktree_unavailable", path=str(path), error=err[-500:]
        )
    return _record(ws, base=base, created=True, fetched=fetched)


def _record(ws: Workspace, *, base: str | None, created: bool, fetched: bool) -> dict[str, Any]:
    assert ws.worktree is not None
    return {
        "repo": ws.repo_id,
        "path": str(ws.worktree),
        "branch": ws.branch,
        "base": base,
        "created": created,
        "fetched": fetched,
    }


def _base(ws: Workspace) -> str:
    """The base the branch starts from; only a name that resolves to a commit reaches git."""
    assert ws.checkout is not None
    # A configured base_ref is a git revision as written (``HEAD^{commit}`` included); only the
    # built-in candidates have a {remote} to fill in.
    candidates = (
        (ws.base_ref,)
        if ws.base_ref
        else tuple(c.format(remote=ws.remote) for c in BASE_CANDIDATES)
    )
    for ref in candidates:
        if not ref.startswith("-") and _commit(ws.checkout, ref):
            return ref
    raise rejected(
        "找不到 worktree 的基线", reason="worktree_unavailable", repo=ws.repo_id, base=ws.base_ref
    )


def _commit(checkout: Path, ref: str) -> bool:
    found = _git(
        checkout, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"
    )
    return found[0] == 0


def _common_dir(directory: Path) -> Path:
    """The repository a checkout or worktree belongs to (its shared ``.git``)."""
    out = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")[1]
    return Path(out) if out else directory / ".nonexistent"


def _same(a: Path, b: Path) -> bool:
    def key(p: Path) -> str:
        return os.path.normcase(os.path.realpath(p))

    return key(a) == key(b)


def _git(directory: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ------------------------------------------------------------------------------------- the brief


def home_skills(home: Home) -> list[tuple[str, str, Path]]:
    """The data directory's skills a worker may use: (name, description, SKILL.md)."""
    found: list[tuple[str, str, Path]] = []
    root = home.root / ".claude" / "skills"
    if not root.is_dir():
        return found
    for skill in sorted(root.glob("*/SKILL.md")):
        name, description = _frontmatter(skill)
        name = name or skill.parent.name
        if name not in ORCHESTRATOR_SKILLS:
            found.append((name, description, skill))
    return found


def _frontmatter(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "", ""
    fields: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep and not line.startswith((" ", "\t")):
                fields[key.strip()] = value.strip().strip("\"'")
    return fields.get("name", ""), fields.get("description", "")


def brief_section(
    ws: Workspace,
    skills: list[tuple[str, str, Path]],
    workdir: str,
    *,
    gh_skill: Path | None = None,
) -> str:
    """The brief's "仓库与 worktree" section; ``gh_skill`` is the rendered gh-account skill."""
    files = "、".join(f"`{p}`" for p in ws.instructions)
    lines = ["## 仓库与 worktree", ""]
    if ws.worktree is not None:
        lines += [
            f"- 会话目录：`{ws.worktree}`，是仓库 {ws.repo_id} 的 git worktree，分支 `{ws.branch}`"
            "（预启动或派发时由 tsw 创建；需要时自己与主分支同步）。会话就在这里启动，宿主按仓库"
            "自己的配置加载它的 skill、`.mcp.json`、项目插件、hooks 与设置、CLAUDE.md。不要另建 "
            "worktree 或 clone。",
            f"- 报告草稿与其他非仓库产物写在执行目录 `{workdir}`（会话目录的上级），不要写进仓库。",
        ]
    else:
        lines += [
            f"- tsw 没有为仓库 {ws.repo_id} 创建 worktree：{ws.missing}。会话在执行目录中启动；"
            "需要仓库时在执行目录下建 worktree。",
        ]
    lines += [
        f"- 仓库指令文件：开始工作前完整阅读并遵守仓库根目录中的 {files}（不存在的跳过）；改动某个"
        "子目录前，也读该子目录中的同名文件。宿主不会自动加载 AGENTS.md。报告中写明读了哪些。",
    ]
    account_line = _account_line(ws, gh_skill)
    if account_line:
        lines.append(account_line)
    if ws.worktree is not None and skills:
        lines.append(
            "- 数据目录的 skill：会话目录在 git 仓库中时，宿主不加载数据目录 `.claude/skills/` 中的"
            " skill。任务需要时直接读对应的 SKILL.md 并照做："
        )
        lines += [f"  - `{name}`：{desc}（`{path}`）" for name, desc, path in skills]
    return "\n".join(lines) + "\n\n"


def _account_line(ws: Workspace, gh_skill: Path | None) -> str:
    """The brief's line on the repository's gh account (CC006 §10.1, T279); empty off GitHub."""
    if not ws.github:
        return ""
    how = (
        f"按 `gh-account` skill（`{gh_skill}`）只对一条命令选择账号"
        if gh_skill is not None
        else "只对一条命令选择账号（`GH_TOKEN` 前缀等）"
    )
    if ws.gh_account is not None:
        name, host, profile = ws.gh_account
        return (
            f"- gh 账号：仓库账号为 `{name}`（{host}，配置 `repositories.{ws.repo_id}.auth_profile`"
            f" 为 `{profile}`）。用 gh 建 PR、合并、查看 CI 或用 git 推送之前，核对 gh 当前账号是否"
            f"就是它；不一致时{how}，不用 `gh auth switch`。"
        )
    find = (
        f"按 `gh-account` skill（`{gh_skill}`）第 1 节确定仓库账号"
        if gh_skill is not None
        else "先确定仓库账号（仓库 owner 对应的已登录账号；确定不了时问用户）"
    )
    return (
        f"- gh 账号：配置中没有为仓库 {ws.repo_id} 指定账号"
        f"（`repositories.{ws.repo_id}.auth_profile`）。"
        f"用 gh 建 PR、合并、查看 CI 或用 git 推送之前，{find}并核对 gh 当前账号；不一致时只对一条"
        "命令选择账号，不用 `gh auth switch`。"
    )
