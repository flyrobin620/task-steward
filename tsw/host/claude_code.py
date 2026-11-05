"""The ``claude-code`` host integration (DN006 §6.3): generated files and the install hint.

The plugin in ``plugin/`` carries the skill, the MCP registration and the hooks. This module
generates the data directory's ``CLAUDE.md`` for the orchestrator session and explains how to
install the plugin; it never changes global settings by itself.
"""

from __future__ import annotations

from pathlib import Path

from tsw.home import Home
from tsw.storage.fs import atomic_write

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugin"
CAPABILITIES = {
    "name": "claude-code",
    "enforcement": "enforced",
    "covers": ["write_scope", "push", "pr", "ledger", "hashing", "session_start"],
}

CLAUDE_MD = """# TaskSteward 数据目录

这里是 TaskSteward 的数据目录，在这里打开的会话是主会话。

- 用户要求执行、继续、恢复或调整待办时，按 `orchestrate` skill 工作；
  普通解释与维护不开始执行真实待办。
- 所有状态变更都经 `tsw` MCP 工具写入账本。不要直接编辑 `ledger/`、`runs/`、
  `todo/`、`config/`、`views/` 等目录（hooks 会拒绝）。
- 只对任务启动、任务完成或需要用户补充与决定的事项汇报；普通进度留在账本。
- 回复用户使用中文。
"""


def render_instructions(home: Home) -> list[Path]:
    """Write the orchestrator's CLAUDE.md into the data directory."""
    target = home.root / "CLAUDE.md"
    atomic_write(target, CLAUDE_MD.encode("utf-8"))
    return [target]


def install_hint(home: Home) -> str:
    plugin = PLUGIN_DIR.parent
    return "\n".join(
        [
            "TaskSteward 的 Claude Code 插件安装步骤（只需一次）：",
            f"1. 在 Claude Code 中添加本地插件市场：/plugin marketplace add {plugin}",
            "2. 安装插件：/plugin install task-steward@task-steward",
            f"3. 设置环境变量 TSW_HOME={home.root}（或在仓库的 .local/tsw-home 中写入该路径），",
            "   让 worktree 中的 worker 会话也能找到数据目录。",
            f"4. 在数据目录 {home.root} 中打开一个会话作为主会话。",
            "插件提供 orchestrate skill、tsw MCP server 与 hooks；tsw 不修改任何全局设置。",
        ]
    )
