# CC009 Claude Code 上的分发

- 状态：设计草案（T247，待用户确认）
- 日期：2026-11-09
- 实现：[DN009 分发](../DN009-distribution.md)
- 相关：[CC006 Claude Code 插件](CC006-plugins.md) · [CC008 Claude Code 上的护栏](CC008-guardrails.md)

## 1. 范围

本文是 DN009 在 Claude Code（CLI 与桌面 app）上的做法：市场清单、TaskSteward 插件的安装与更新、各类会话能看到哪些插件与 skill、打包检查、私有仓库的凭据。出处为 Claude Code 官方文档：[Marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference.md)、[Host and maintain a marketplace](https://code.claude.com/docs/en/plugins/host-marketplace.md)、[Plugin manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference.md)、[Plugin loading reference](https://code.claude.com/docs/en/plugins/loading.md)。

## 2. TaskSteward 插件

| 项 | 做法 |
| --- | --- |
| 市场清单 | 仓库根目录 `.claude-plugin/marketplace.json`：`name`、`owner`、`plugins` 必填；条目 `task-steward` 的来源为相对路径 `./plugin`，从市场根目录解析 |
| 方式 A（推荐） | 检出标签后，在 Claude Code 中以检出目录添加本地市场（`/plugin marketplace add <检出目录>`），再 `/plugin install task-steward@task-steward` |
| 方式 B | `/plugin marketplace add flyrobin620/task-steward#vX.Y.Z`，再安装同一插件；插件被复制进缓存 `~/.claude/plugins/cache/<市场>/<插件>/<版本>/`，缓存中没有插件目录之外的文件，所以启动器按 `TSW_REPO` 或数据目录记录的 `product_repo` 找运行时（CC006 §2） |
| 版本 | 宿主按“`plugin.json` 的 `version` → 条目的 `version` → 来源”计算版本：git 市场中的相对路径插件不写版本时取安装目录的提交 SHA；`plugin.json` 与条目不能同时写 |
| 更新 | `/plugin marketplace update task-steward` 或 `claude plugin update task-steward@task-steward`；计算出的版本没变时不替换副本（“already at the latest version”）；之后 `/reload-plugins` 或重启。自动更新对非官方市场默认关闭 |

**本机实测（2026-11-09）**：插件以本地目录市场登记，`plugin.json` 的 `version` 为 `0.1.0-dev`。正在运行的 tsw MCP server 的命令行是缓存目录 `…/cache/task-steward/task-steward/0.1.0-dev/bin/tsw_launch.py`，缓存内容停在 2026-11-06：

- `hooks/hooks.json` 的 PreToolUse matcher 没有 `CronCreate|ScheduleWakeup`（T243 加入）；
- `bin/tsw_launch.py` 是旧版，不按 `claim_sessions` 找数据目录（T245 加入）；
- 还有已从产品中删除的 `skills/orchestrate`，会话的 skill 列表因此多出 `task-steward:orchestrate`。

文档说本地目录市场中的相对路径插件“原地加载”，与实测不符；不论原因，固定的版本字符串让 `claude plugin update` 不会替换这份副本。所以 DN009 §4.1 规定开发期不写固定版本，并由启动器提示过期副本：

- SessionStart 时，启动器比较自己所在目录（`${CLAUDE_PLUGIN_ROOT}`）与产品仓库 `plugin/` 下的文件摘要；不一致且自己不在产品仓库中时，在 hook 的上下文输出中写一次提示与更新命令；
- 不阻断，不改任何设置。

## 3. 各类会话能看到什么

TaskSteward 的会话目录见 DN009 §5.4。Claude Code 的规则：

| 机制 | 依据 | 主会话（数据目录） | `work/`、`routine/` | worktree |
| --- | --- | --- | --- | --- |
| 用户范围的插件（`~/.claude/settings.json` 的 `enabledPlugins`） | 对用户的所有项目生效 | 能用 | 能用 | 能用 |
| 项目范围的插件（数据目录 `.claude/settings.json` 的 `enabledPlugins` 与 `extraKnownMarketplaces`） | 宿主只读会话主工作目录的共享设置；项目插件要求信任该文件夹本身，信任上级目录不算；它声明的 MCP server 逐个批准 | 能用 | 不能用 | 不能用 |
| 数据目录 `.claude/skills/<名>/SKILL.md` 中的普通 skill | 普通 skill 会从上级目录被发现 | 能用 | 能用 | 不能用 |
| 数据目录 `.claude/skills/` 中带 `.claude-plugin/plugin.json` 的插件（`<名>@skills-dir`） | 只读主工作目录的 `.claude/skills/`，不向上级目录查找插件 | 能用 | 不能用 | 不能用 |
| `~/.claude/skills/` 中的普通 skill 或插件 | 用户级，不需要信任确认 | 能用 | 能用 | 能用 |

补充：

- 只在项目设置中启用、来源不是相对路径的插件，宿主不会替没安装的机器下载（文档：“enabled in project settings but isn't installed here”）。
- 在执行目录写 `.claude/settings.local.json` 来启用插件，会让 app 对该目录再次要求信任（决定记录 0003），所以不采用。
- `tsw host render` 只改数据目录 `.claude/settings.json` 中的 `permissions.allow`，其他键保持不变（`allow_actions`），用户按项目范围安装的插件不会被渲染冲掉。

结论同 DN009 §5.4：给 worker 用的插件装在用户范围；只含 skill、只给主会话与 `work/` 中会话用的，可以放数据目录 `.claude/skills/`。

## 4. 打包检查

| 检查 | 做法 |
| --- | --- |
| 清单与插件校验 | `claude plugin validate <市场根目录或插件目录>`，CI 中加 `--strict` 把警告当错误 |
| 名称 | 市场名与插件名只用字母、数字、`.`、`_`、`-`，以字母或数字开头；不能用保留名，也不能以 `claude-`、`anthropic-` 开头 |
| 路径 | 组件路径以 `./` 开头，不能越出插件根目录；同一市场内指向其他插件的符号链接在复制时被解引用，指向市场之外的被跳过 |
| 参数 | `userConfig` 的值以 `${user_config.KEY}` 在 MCP 配置、exec 形式的 hook 参数与 skill 正文中替换；敏感值进系统凭据库，在 skill 正文中只是占位符 |
| 可执行文件 | 顶层 `bin/` 中的文件进入 Bash 工具的 `PATH`；但经 claude.ai 组织同步分发的插件不能有顶层 `bin/`。TaskSteward 插件的启动器在 `bin/`，将来要走组织同步时移到 `scripts/` |
| 版本 | 按 DN009 §7；`tsw dist check` 在内容变化而版本没变时报错 |

## 5. 私有市场的凭据

- 宿主在用户机器上以非交互方式运行 `git`，没有自己的令牌，清单也没有凭据字段。
- GitHub：`owner/repo` 形式先试 SSH，失败时改用 HTTPS；没有 SSH key 的机器设 `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`。HTTPS 依赖已存好的凭据，例如 `gh auth login` 加 `gh auth setup-git`。
- 后台自动更新拿不到凭据时静默失败，保留现有副本。

## 6. 安装命令（由 install-hint 给出）

用户范围：

```text
/plugin marketplace add <市场：owner/repo、owner/repo#ref 或本地目录>
/plugin install <插件>@<市场>
```

shell 中的等价命令为 `claude plugin marketplace add …` 与 `claude plugin install …`。tsw 只输出这些命令，不替用户执行，也不写用户设置（DN009 §3 第 4 条）。
