# CX009 Codex 上的分发

- 状态：设计草案（T247，待用户确认）
- 日期：2026-11-09
- 实现：[DN009 分发](../DN009-distribution.md)
- 相关：[CX004 Codex 评审器](CX004-reviewer.md)

## 1. 范围

本文是 DN009 在 Codex 上的做法。Codex 的宿主集成（DN006 §6.3 的 `codex`）尚未实现，本文先确定分发部分，供实现时使用。出处：[Build plugins](https://developers.openai.com/codex/plugins/build)、[Build skills](https://learn.chatgpt.com/docs/build-skills)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)，以及 codex-cli 0.163.0 的命令帮助。

## 2. 插件与市场

| 项 | Codex 的做法 |
| --- | --- |
| 插件格式 | 推荐根目录 `plugin.json`、`mcp.json`、`skills/`；也接受 Claude 兼容的清单，`.codex-plugin/plugin.json` 为兼容回退 |
| 市场清单位置 | 仓库：`$REPO_ROOT/.agents/plugins/marketplace.json`，或兼容的 `$REPO_ROOT/.claude-plugin/marketplace.json`；用户：`~/.agents/plugins/marketplace.json`。`source.path` 从市场根目录解析 |
| 添加市场 | `codex plugin marketplace add <本地路径 \| owner/repo[@ref] \| git URL>`，可加 `--ref`、`--sparse`；没有安装范围参数，结果写进用户级 `~/.codex/config.toml` 的 `[marketplaces.<名>]` |
| 启用插件 | `codex plugin add`，或 `config.toml` 中 `[plugins."<插件>@<市场>"] enabled = true`；受信任项目的 `.codex/config.toml` 也可以写 |
| 更新 | `codex plugin marketplace upgrade [名]`；git 来源的条目可用 `ref` 或 `sha` 固定；缓存在 `~/.codex/plugins/cache/<市场>/<插件>/<版本>/` |
| hooks | 插件带的 hooks 要用户审阅并信任后才运行 |
| 环境变量 | `PLUGIN_ROOT`、`PLUGIN_DATA`；为兼容 Claude 插件的 hooks，也设置 `CLAUDE_PLUGIN_ROOT`、`CLAUDE_PLUGIN_DATA` |

**本机证据（2026-11-09）**：用户级 `config.toml` 中登记了一个 git 来源的市场（`source_type = "git"`，稀疏检出含 `.claude-plugin`），并启用了其中两个插件；缓存目录中只有 Claude 格式的文件（`.claude-plugin/plugin.json`、`.mcp.json`、`skills/`）。所以同一个 Claude 格式的市场仓库已经同时被两个宿主安装。其中的 MCP server 在 Codex 中能否运行没有核实（DN009 V5）。

## 3. 各类会话能看到什么

| 机制 | 依据 | 主会话（数据目录） | `work/`、`routine/` | worktree |
| --- | --- | --- | --- | --- |
| 用户级市场与插件（`~/.codex/config.toml`） | 所有会话共用这份配置 | 能用 | 能用 | 能用 |
| 仓库级市场与 `.codex/config.toml` | 按仓库根定位；项目配置只对受信任项目生效 | 数据目录不是 git 仓库，未验证（DN009 V2） | 同左 | 属于业务仓库，不由 TaskSteward 管理 |
| `.agents/skills` | 从当前目录向上直到仓库根查找；用户级 `~/.agents/skills` | 未验证（同上） | 未验证 | 只有业务仓库自己的 |

结论：Codex 上给 TaskSteward 会话用的插件只用用户级安装；仓库级的入口留给业务仓库自己。

## 4. 与 Claude Code 的差别

- **参数**：Codex 文档没有与 `userConfig` 对应的启用时参数，跨宿主的插件按 DN009 §5.1 从配置文件读取个人数据（DN009 V3）。
- **MCP 声明**：Codex 自己的格式是根目录 `mcp.json`，Claude 格式是 `.mcp.json`；在 V5 验证前，跨宿主的插件两份都放，内容相同。
- **skill 位置**：Claude Code 读 `.claude/skills` 与 `~/.claude/skills`，Codex 读 `.agents/skills` 与 `~/.agents/skills`；经插件分发可以避开两套路径。

## 5. 安装命令（Codex 宿主集成实现后由 install-hint 给出）

```text
codex plugin marketplace add <owner/repo[@ref] 或本地目录>
codex plugin add <插件>@<市场>
```

tsw 不改写 `~/.codex/config.toml`；需要信任的 hooks 由用户在 Codex 中确认。
