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
from typing import Any

from tsw.config import EffectiveConfig, resolve_repository
from tsw.errors import rejected
from tsw.home import Home, workdir_path

DEFAULT_INSTRUCTIONS = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
# Where the work starts when the repository names no base_ref (the first that resolves).
BASE_CANDIDATES = ("{remote}/HEAD", "{remote}/main", "{remote}/master", "main", "master", "HEAD")
# The data directory's skills for the main session only (tsw host render writes them).
ORCHESTRATOR_SKILLS = ("orchestrate", "handle-input")
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


def plan(
    home: Home, config: EffectiveConfig, row: Any, attempt_id: str, workdir: str
) -> Workspace | None:
    """The workspace of the task in ``row``; None when its repository is not configured."""
    repo_id = resolve_repository(config, str(row["repo"] or ""))
    if repo_id is None:
        return None
    repo: dict[str, Any] = config.get(f"repositories.{repo_id}", {}) or {}
    instructions = tuple(str(p) for p in (repo.get("instructions") or DEFAULT_INSTRUCTIONS))
    remote = str(repo.get("remote") or "origin")
    base_ref = str(repo["base_ref"]) if repo.get("base_ref") else None
    raw = str(repo.get("path") or "")
    checkout = Path(raw).expanduser() if raw else None
    missing = None
    if checkout is None:
        missing = "配置中没有仓库路径（repositories.<id>.path）"
    elif not checkout.is_dir():
        missing = f"仓库路径 {checkout} 不存在"
    elif _git(checkout, "rev-parse", "--git-dir")[0] != 0:
        missing = f"仓库路径 {checkout} 不是 git 仓库"
    if missing is not None:
        return Workspace(repo_id, instructions, None, None, None, remote, base_ref, missing)
    task_id = str(row["task_id"])
    branch = str(config.get("worktrees.branch_pattern")).format(
        task_id_lower=task_id.lower(), task_id=task_id, attempt=attempt_id
    )
    worktree = workdir_path(home, workdir) / repo_id
    return Workspace(repo_id, instructions, checkout, worktree, branch, remote, base_ref, None)


def ensure(ws: Workspace) -> dict[str, Any]:
    """Create the worktree (idempotent) and return what is recorded about it.

    An existing worktree on the task's branch is used as it is (a later generation of the same
    attempt). Anything else at that path is refused, as is a git command that fails: the
    caller records nothing then.
    """
    assert ws.checkout is not None and ws.worktree is not None and ws.branch is not None
    path, branch = ws.worktree, ws.branch
    if path.exists():
        code, head, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        top = _git(path, "rev-parse", "--show-toplevel")[1]
        if code == 0 and head == branch and _same(Path(top), path):
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
    assert ws.checkout is not None
    if ws.base_ref:
        return ws.base_ref
    for candidate in BASE_CANDIDATES:
        ref = candidate.format(remote=ws.remote)
        if _git(ws.checkout, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0:
            return ref
    raise rejected("找不到 worktree 的基线", reason="worktree_unavailable", repo=ws.repo_id)


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


def brief_section(ws: Workspace, skills: list[tuple[str, str, Path]], workdir: str) -> str:
    """The brief's "仓库与 worktree" section."""
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
    if ws.worktree is not None and skills:
        lines.append(
            "- 数据目录的 skill：会话目录在 git 仓库中时，宿主不加载数据目录 `.claude/skills/` 中的"
            " skill。任务需要时直接读对应的 SKILL.md 并照做："
        )
        lines += [f"  - `{name}`：{desc}（`{path}`）" for name, desc, path in skills]
    return "\n".join(lines) + "\n\n"
