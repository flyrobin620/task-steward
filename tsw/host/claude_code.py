"""The ``claude-code`` host integration (DN006 §6.3): generated files and the install hint.

The plugin in ``plugin/`` carries what every session needs wherever it runs: the MCP
registration and the hooks. It is enabled at user scope, so it carries no orchestrator skill.
This module generates the orchestrator's instructions into the data directory: ``CLAUDE.md`` and
the project skills ``.claude/skills/orchestrate/SKILL.md`` and ``handle-input`` (questions handed
over by the routine session, CC006 §6.4). Sessions whose directory is under the data directory
and not in a git repository (the routine session, a worker without a repository worktree) also
discover them; a worker in a repository worktree does not (CC006 §8). It also generates the
permission rules for the actions tsw guards itself (CC008 §6): ``.claude/settings.json`` in
the data directory for the orchestrator. A worker's execution directory gets no settings file:
a folder that brings its own permission rules asks for trust again even under a trusted data
directory, so a worker relies on the user's auto-mode rules instead (decision 0003). It
explains how to install the plugin and never changes global settings by itself.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from tsw.home import Home
from tsw.storage.fs import atomic_write

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugin"
ORCHESTRATE_SKILL = Path(__file__).resolve().parent / "orchestrate_skill.md"
SKILL_PATH = Path(".claude") / "skills" / "orchestrate" / "SKILL.md"
HANDLE_INPUT_SKILL = Path(__file__).resolve().parent / "handle_input_skill.md"
HANDLE_INPUT_PATH = Path(".claude") / "skills" / "handle-input" / "SKILL.md"
SETTINGS_PATH = Path(".claude") / "settings.json"
WORKER_SETTINGS_PATH = Path(".claude") / "settings.local.json"
# The plugin ``task-steward`` registers the MCP server ``tsw`` (plugin/.mcp.json).
TSW_TOOLS = "mcp__plugin_task-steward_tsw"
SEND_MESSAGE = ("SendMessage", "mcp__ccd_session_mgmt__send_message")
# Actions tsw guards itself, allowed without the host's own permission check (DN006 §6.3).
ORCHESTRATOR_ALLOW = (
    TSW_TOOLS,
    "mcp__ccd_session__start_session",
    *SEND_MESSAGE,
    "mcp__ccd_session_mgmt__stop_session",
    "mcp__ccd_session_mgmt__get_session",
    "mcp__ccd_session_mgmt__list_sessions",
    "mcp__ccd_session_mgmt__archive_session",
)
# What tsw wrote into a worker's ``.claude/settings.local.json`` before decision 0003; a file
# holding nothing else is tsw's own and is removed (``clear_worker_settings``).
WORKER_ALLOW = (TSW_TOOLS, *SEND_MESSAGE)
WORKER_MERGE_ALLOW = ("Bash(gh pr merge:*)",)
CAPABILITIES = {
    "name": "claude-code",
    "enforcement": "enforced",
    "covers": ["write_scope", "push", "pr", "ledger", "hashing", "session_start"],
}

CLAUDE_MD = """# TaskSteward 数据目录

这里是 TaskSteward 的数据目录，在这里打开的会话是主会话。执行目录在本目录之下的
worker 子会话与例行会话（`routine/`）也会读到本文件与项目 skill：`whoami` 的角色不是
orchestrator 时，忽略二者：worker 按任务书工作，例行会话按会话开始时的说明运行例行表。

- 用户要求执行、继续、恢复或调整待办时，按 `orchestrate` skill 工作；
  普通解释与维护不开始执行真实待办。
- 所有状态变更都经 `tsw` MCP 工具写入账本。不要直接编辑 `ledger/`、`runs/`、
  `todo/`、`config/`、`views/` 等目录（hooks 会拒绝）。
- 只对任务启动、任务完成或需要用户补充与决定的事项汇报；普通进度留在账本。
- 回复用户使用中文。
"""


def render_instructions(home: Home) -> list[Path]:
    """Write the orchestrator's CLAUDE.md, its ``orchestrate`` skill and its permission rules
    into the data directory."""
    claude_md = home.root / "CLAUDE.md"
    atomic_write(claude_md, CLAUDE_MD.encode("utf-8"))
    skill = home.root / SKILL_PATH
    atomic_write(skill, ORCHESTRATE_SKILL.read_bytes())
    handle_input = home.root / HANDLE_INPUT_PATH
    atomic_write(handle_input, HANDLE_INPUT_SKILL.read_bytes())
    settings = home.root / SETTINGS_PATH
    allow_actions(settings, ORCHESTRATOR_ALLOW)
    return [claude_md, skill, handle_input, settings]


def clear_worker_settings(workdir: Path) -> bool:
    """Remove the permission rules tsw once generated in a worker's execution directory.

    Only a file that is exactly what tsw wrote goes (and ``.claude/`` when it is left empty):
    the worker rules, with or without the merge rule, and nothing else. A file with anything
    more or less, such as rules the user allowed in that session, may be the user's and stays
    as it is. A link anywhere on the way (``.claude`` or the file itself) may lead outside the
    execution directory, to a file tsw never wrote, so it stays too. Returns whether a file was
    removed.
    """
    settings = workdir / WORKER_SETTINGS_PATH
    if settings.parent.is_symlink() or settings.is_symlink() or not settings.is_file():
        return False
    if settings.resolve().parent != (workdir.resolve() / WORKER_SETTINGS_PATH.parent):
        return False
    try:
        loaded: Any = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(loaded, dict) or set(cast("dict[str, Any]", loaded)) != {"permissions"}:
        return False
    permissions: Any = cast("dict[str, Any]", loaded)["permissions"]
    if not isinstance(permissions, dict) or set(cast("dict[str, Any]", permissions)) != {"allow"}:
        return False
    rules: Any = cast("dict[str, Any]", permissions)["allow"]
    if not isinstance(rules, list):
        return False
    found = cast("list[Any]", rules)
    written = ({*WORKER_ALLOW}, {*WORKER_ALLOW, *WORKER_MERGE_ALLOW})
    if len(found) != len(set(map(str, found))) or set(map(str, found)) not in written:
        return False
    settings.unlink()
    with contextlib.suppress(OSError):
        settings.parent.rmdir()  # only when empty
    return True


def allow_actions(path: Path, rules: Iterable[str]) -> None:
    """Add ``rules`` to ``permissions.allow`` of a Claude Code settings file, keeping the rest.

    A worker merge rule that ``rules`` does not contain is removed: tsw no longer generates it
    (decision 0003).
    """
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            data = cast("dict[str, Any]", loaded)
    found: Any = data.get("permissions")
    permissions = cast("dict[str, Any]", found) if isinstance(found, dict) else {}
    current: Any = permissions.get("allow")
    existing = [str(r) for r in cast("list[Any]", current)] if isinstance(current, list) else []
    wanted = list(rules)
    kept = [r for r in existing if r not in WORKER_MERGE_ALLOW or r in wanted]
    permissions["allow"] = kept + [r for r in wanted if r not in kept]
    data["permissions"] = permissions
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# A rule for Claude Code's auto-mode classifier (DN006 §6.3). Claude Code reads auto-mode rules
# only from the user's, the command line's and the managed settings, never from a project's, so
# tsw cannot generate it next to the permission rules: the install hint shows it to the user.
AUTO_MODE_WORKER_MERGE = (
    "TaskSteward Worker Merge: In a TaskSteward worker session (its working directory is "
    "work/<task>/<attempt> under a TaskSteward data directory, or the repository worktree tsw "
    "created inside it, and tsw's whoami reports role "
    "worker), running `gh pr merge <number> -R <host>/<owner>/<name> --squash` as a command of "
    "its own for the task's own pull request, after the task's self-review was finalized, is "
    "authorized by the user's standing decision that workers merge their own pull requests "
    "(TaskSteward decision 0002, repositories.<id>.delivery.merge). The pull request was "
    "reviewed by the configured independent reviewer, and tsw's PreToolUse hook refuses the "
    "merge when the task is not authorized or the target is another repository. This is not "
    "Merge Without Review or Self-Approval. It does not cover --admin, bypassing checks or "
    "branch protection, or merging any other pull request."
)

AUTO_MODE_WORKER_DISPATCH = (
    "TaskSteward Worker Dispatch: In a TaskSteward main session (its working directory is a "
    "TaskSteward data directory and tsw's whoami reports role orchestrator), calling "
    "start_session to create a worker session is the user's standing instruction to run their "
    "TaskSteward tasks, when the session's name is the session_name and its cwd the cwd that "
    "tsw's attempt_start just returned, and its prompt only points the worker at its brief "
    "under runs/; the same holds for the routine session, named and placed by tsw's "
    "routine_start, and for detaching that routine session from the main session with "
    "detach_session after routine_bind (or when tsw reports it still attached), so that its turns "
    "stop waking the main session. "
    "The brief's delivery terms, including a worker merging its own pull request "
    "(TaskSteward Worker Merge), were authorized by the user. tsw's PreToolUse hook refuses a "
    "start_session whose name tsw did not record. This is not Create Unsafe Agents, Merge "
    "Without Review or Self-Approval. It does not cover sessions in any other folder or with "
    "any other name."
)
AUTO_MODE_ROUTINE = (
    "TaskSteward Routine: In a TaskSteward routine session (its working directory is routine/ "
    "under a TaskSteward data directory and tsw's whoami reports role routine), doing exactly "
    "what tsw's routine_board returned is the user's standing instruction: arming or renewing "
    "the session's own recurring CronCreate task with the prompt tsw gave (deleting the one it "
    "replaces with CronDelete), reading the Teams chat tsw named with ListChatMessages, sending "
    "the message tsw produced to the TaskSteward main session with send_message, and clearing "
    "its own context with clear_session(self). The main session started this session in the "
    "user's turn (TaskSteward Worker Dispatch covers it). It does not cover any other chat, "
    "message, session or tool."
)
AUTO_MODE_RULES = (AUTO_MODE_WORKER_DISPATCH, AUTO_MODE_WORKER_MERGE, AUTO_MODE_ROUTINE)


def install_hint(home: Home) -> str:
    plugin = PLUGIN_DIR.parent
    auto_mode = json.dumps(
        {"autoMode": {"allow": list(AUTO_MODE_RULES)}}, ensure_ascii=False, indent=2
    )
    return "\n".join(
        [
            "TaskSteward 的 Claude Code 插件安装步骤（只需一次）：",
            f"1. 在 Claude Code 中添加本地插件市场：/plugin marketplace add {plugin}",
            "2. 安装插件：/plugin install task-steward@task-steward",
            "   Claude Code 运行的是插件的缓存副本：产品仓库更新（git pull）后运行",
            "   tsw host render，它会更新插件副本（claude plugin update）并报告是否与产品仓库的",
            "   plugin/ 一致；然后重启会话。",
            f"3. 设置环境变量 TSW_HOME={home.root}（或在仓库的 .local/tsw-home 中写入该路径），",
            "   让 worktree 中的 worker 会话也能找到数据目录。",
            "4. 运行 tsw host render，在数据目录生成主会话的 CLAUDE.md 与项目 skill",
            "   .claude/skills/orchestrate/、handle-input/（产品更新后重新运行）。",
            "   需要经 Teams 问你时，在配置 plugins.human_channels 中加入 teams-chat。",
            f"5. 在数据目录 {home.root} 中打开一个会话作为主会话。",
            "插件只提供 tsw MCP server 与 hooks，在任何目录的会话里都生效。数据目录的",
            "项目 skill 由数据目录及其下不在 git 仓库中的会话加载；仓库任务的 worker 在仓库",
            "worktree 中工作，宿主加载仓库自己的配置，任务书列出数据目录的 skill 供它直接读取。",
            "数据目录的项目插件（.claude/settings.json 的 enabledPlugins）只对主会话生效：",
            "要给所有会话用的插件装在用户范围。tsw 不修改任何全局设置。",
            "6. auto 权限模式下，Claude Code 的分类器会把主会话派发 worker、worker 自行合并 PR、",
            "   例行会话的定时任务与转发当作未经授权。worker 与例行会话的目录不带权限规则",
            "   （带规则的目录会再次要求信任），它们的放行都来自这里。由你在用户设置",
            "   （~/.claude/settings.json，",
            "   与已有的 autoMode 合并）中加入下面的 auto 模式规则（不采用 worker 合并时可省略",
            "   合并那一条）；",
            "   Claude Code 不读取项目设置中的 autoMode，tsw 也不会替你修改用户设置：",
            auto_mode,
        ]
    )
