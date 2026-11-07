"""The ``claude-code`` host integration (DN006 §6.3): generated files and the install hint.

The plugin in ``plugin/`` carries what every session needs wherever it runs: the MCP
registration and the hooks. It is enabled at user scope, so it carries no orchestrator skill.
This module generates the orchestrator's instructions into the data directory: ``CLAUDE.md`` and
the project skill ``.claude/skills/orchestrate/SKILL.md``, which only a session opened in the
data directory discovers. It also generates the permission rules for the actions tsw guards
itself (DN006 §6.3): ``.claude/settings.json`` in the data directory for the orchestrator and
``.claude/settings.local.json`` in a worker's execution directory. It explains how to install
the plugin and never changes global settings by itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from tsw.home import Home
from tsw.storage.fs import atomic_write

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugin"
ORCHESTRATE_SKILL = Path(__file__).resolve().parent / "orchestrate_skill.md"
SKILL_PATH = Path(".claude") / "skills" / "orchestrate" / "SKILL.md"
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
WORKER_ALLOW = (TSW_TOOLS, *SEND_MESSAGE)
WORKER_MERGE_ALLOW = ("Bash(gh pr merge:*)",)
CAPABILITIES = {
    "name": "claude-code",
    "enforcement": "enforced",
    "covers": ["write_scope", "push", "pr", "ledger", "hashing", "session_start"],
}

CLAUDE_MD = """# TaskSteward 数据目录

这里是 TaskSteward 的数据目录，在这里打开的会话是主会话。执行目录在本目录之下的
worker 子会话也会读到本文件与 `orchestrate` skill：`whoami` 的角色不是 orchestrator 时，
忽略二者，按任务书工作。

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
    settings = home.root / SETTINGS_PATH
    allow_actions(settings, ORCHESTRATOR_ALLOW)
    return [claude_md, skill, settings]


def render_worker_settings(workdir: Path, *, merge: bool) -> Path:
    """The permission rules of a worker's session, written before the session is created."""
    settings = workdir / WORKER_SETTINGS_PATH
    allow_actions(settings, (*WORKER_ALLOW, *(WORKER_MERGE_ALLOW if merge else ())))
    return settings


def allow_actions(path: Path, rules: Iterable[str]) -> None:
    """Add ``rules`` to ``permissions.allow`` of a Claude Code settings file, keeping the rest.

    A merge rule that ``rules`` does not contain is removed: an attempt that may not merge
    never inherits it from an earlier one in the same directory.
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


def install_hint(home: Home) -> str:
    plugin = PLUGIN_DIR.parent
    return "\n".join(
        [
            "TaskSteward 的 Claude Code 插件安装步骤（只需一次）：",
            f"1. 在 Claude Code 中添加本地插件市场：/plugin marketplace add {plugin}",
            "2. 安装插件：/plugin install task-steward@task-steward",
            f"3. 设置环境变量 TSW_HOME={home.root}（或在仓库的 .local/tsw-home 中写入该路径），",
            "   让 worktree 中的 worker 会话也能找到数据目录。",
            "4. 运行 tsw host render，在数据目录生成主会话的 CLAUDE.md 与",
            "   .claude/skills/orchestrate/SKILL.md（产品更新后重新运行）。",
            f"5. 在数据目录 {home.root} 中打开一个会话作为主会话。",
            "插件只提供 tsw MCP server 与 hooks，在任何目录的会话里都生效；orchestrate skill",
            "是数据目录的项目 skill，只有在数据目录打开的会话会加载。tsw 不修改任何全局设置。",
        ]
    )
