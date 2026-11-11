# TaskSteward（任务管家）

一个可插拔宿主的任务编排器，默认宿主是 **Claude Code 桌面 app**，也可以经插件层接入 Codex、GitHub Copilot 或自建的远程会话中枢。

- 主会话（orchestrator）是 app 中的一个会话。
- 每个任务由主会话启动的关联子会话（worker）执行。
- worker 完成后自己调用评审器（默认 Codex gpt-6-astra · medium）评审成果，并决定是否迭代；评审预算用完或遇到问题时，先由主会话判断，确需人类时再经渠道请求。
- worker 定稿后由主会话技术验收，最后交用户人工验收。
- 状态由事件账本驱动，可导入 orchestrator-agent 的全部历史。
- 运行数据（配置、任务书、账本、报告）放在与本 repo 分离的数据目录 `TSW_HOME`，不提交到本 repo；备份该目录即可。

当前阶段：**v1 开发中**。已实现数据目录、`fs` 存储、事件账本与投影、统一配置、调度租约、完整的任务状态机（DN002 的全部迁移与守卫，经 `tsw tool run` 调用）与 `tsw` 命令行；宿主集成（MCP、hooks、插件）、会话适配器与评审器、上下文检索按里程碑实现中。开发流程与规范见下文“参与开发”。

## 设计文档

每份文档都有 Markdown 与 HTML 两个版本，内容一致；HTML 可离线在浏览器打开并打印。

| 文档 | Markdown | HTML | 内容 |
| --- | --- | --- | --- |
| DN001 总体架构 | [md](docs/design/DN001-architecture.md) | [html](docs/design/DN001-architecture.html) | 目标、组件、会话模型与平台约束、工具面、hooks、存储、关键决策、待 PoC 验证的假设 |
| DN002 任务状态机 | [md](docs/design/DN002-task-state-machine.md) | [html](docs/design/DN002-task-state-machine.html) | 按主控视角划分状态的单张状态机（含 worker 内部评审子状态）与导入映射；完整迁移表、守卫条件、attempt 阶段、事件类型、不变量、异常恢复 |
| DN003 统一配置 | [md](docs/design/DN003-unified-config.md) | [html](docs/design/DN003-unified-config.html) | `config/task-steward.json` 完整示例、字段说明、校验与生效规则 |
| DN004 worker 自评审 | [md](docs/design/DN004-worker-self-review.md) | [html](docs/design/DN004-worker-self-review.html) | 评审器调用（实现见 CX004、CC004）、输出契约、处置规范、迭代决策、主控复核 |
| DN005 历史导入与 resume | [md](docs/design/DN005-history-import-and-resume.md) | [html](docs/design/DN005-history-import-and-resume.html) | 导入来源、分类规则、导入步骤、resume 资料包、验证与回退 |
| DN006 插件层 | [md](docs/design/DN006-plugin-layer.md) | [html](docs/design/DN006-plugin-layer.html) | 宿主集成、会话适配器（含一次性评审）、人类渠道、存储后端四类插件的职责与接口；委托操作；主会话判断；数据目录；组合示例 |
| DN007 账本与上下文检索 | [md](docs/design/DN007-ledger-and-context.md) | [html](docs/design/DN007-ledger-and-context.html) | 三层存储、写读路径与重建；任务卡片、检索工具、用户决定与偏好；主会话与 worker 的历史读取方式 |
| DN008 校验原则 | [md](docs/design/DN008-guardrails.md) | [html](docs/design/DN008-guardrails.html) | 安全边界与信任假设；护栏只防诚实 agent 出错：确定性死规则、易用性优先、block list、escape |
| CC001 Claude Code 上的会话与 hooks | [md](docs/design/claude-code/CC001-sessions-and-hooks.md) | [html](docs/design/claude-code/CC001-sessions-and-hooks.html) | DN001 在 Claude Code 上的实现：主会话指令、会话创建参数与平台约束、hooks、平台 PoC |
| CC004 Claude Code 评审器 | [md](docs/design/claude-code/CC004-reviewer.md) | [html](docs/design/claude-code/CC004-reviewer.html) | `claude-headless` 只读评审的调用约定 |
| CC006 Claude Code 插件 | [md](docs/design/claude-code/CC006-plugins.md) | [html](docs/design/claude-code/CC006-plugins.html) | 宿主集成、会话适配器与 in-session 渠道在 Claude Code 上的做法 |
| CC008 Claude Code 上的护栏 | [md](docs/design/claude-code/CC008-guardrails.md) | [html](docs/design/claude-code/CC008-guardrails.html) | block list 规则与 escape、现有校验逐项归类、允许规则 |
| CX004 Codex 评审器 | [md](docs/design/codex/CX004-reviewer.md) | [html](docs/design/codex/CX004-reviewer.html) | `codex-app-server` 一次性线程与 `codex-exec` 回退 |

图的结构来源是 `docs/design/diagrams/*.mmd`（状态、迁移编号与标签的权威来源），文档引用同名 `.svg`：

- 彩色 SVG 由 `docs/design/diagrams/draw.py` 按手工布局生成（读取 `.mmd` 的标签），运行 `python -X utf8 docs/design/diagrams/draw.py`；
- `docs/design/diagrams/check.py` 校验 XML 合法性，以及每条迁移、节点、编号是否都在 SVG 中可见，结果写入 `validation.json`；
- `docs/design/diagrams/auto/` 下是 Mermaid 自动布局的全量拓扑图，仅作结构核对。

修改 `.mmd` 后需要同步调整 `draw.py` 中的布局坐标，再运行两个脚本。

## 参与开发

所有改动遵循 [AGENTS.md](AGENTS.md)：开始前先读它，再按其中的表读对应的专题文档。

| 文档 | 内容 |
| --- | --- |
| [开发流程](docs/development/workflow.md) | 从改代码到合并的每一步：门禁、证据、失败处理 |
| [测试层级](docs/development/testing-layers.md) | unit、component、surface、conformance、live 五个层级与 live 场景 |
| [测试环境](docs/development/test-environments.md) | 临时数据目录、子进程环境隔离、live 运行清单与清理 |
| [use-case 目录](docs/development/use-cases.md) | use case 与状态迁移清单、测试标记、目录检查 |
| [CI](docs/development/ci.md) | GitHub Actions（Windows）、阶段与汇总检查 `Gate` |
| [评审与合并](docs/development/review.md) | 独立评审、PR 模板、合并条件与需要用户确认的范围 |
| [代码质量](docs/development/code-quality.md) | 警告即错误、只依赖标准库、开发依赖 |
| [兼容性](docs/development/compatibility.md) | 账本、配置、插件接口等契约的版本规则 |
| [文档](docs/development/docs.md) | 语言与格式、HTML 生成、设计文档、公开仓库卫生 |
| [发布](docs/development/release.md) | 版本与发布（v1 开发期间不发布） |
| [决定记录](docs/decisions/README.md) | 已确认的决定 |

## 开始使用（开发中）

需要 Python 3.10+ 与 git。运行时只依赖标准库。

### 安装 `tsw` 命令（只需一次）

在本仓库根目录运行：

```text
python -m pip install -e .
tsw init --home <数据目录> --remember
```

第一行把 `tsw` 装进 Python 的 `Scripts` 目录（Windows 的 PowerShell、cmd 与 Git Bash 都能直接运行 `tsw`）。这是可编辑安装，运行的就是本仓库当前的代码，`git pull` 后不用重装；请保留 `-e`：`host render` 等宿主命令要用仓库中的 `plugin/`，它不在 Python 包里。第二行初始化（或打开已有的）数据目录，并把它记在本仓库的 `.local/tsw-home` 中，之后终端不需要设置 `TSW_HOME`。不想安装时，在仓库根目录用 `python -m tsw` 代替 `tsw`。

### 哪些命令在哪里运行

- **运行时**（tsw 的代码）在本仓库：安装后 `tsw` 在任何目录都找得到它。Claude Code 的会话通过插件里的启动器运行 tsw，不需要这一步。
- **数据目录**（账本、任务书、报告）按顺序定位：当前目录所在的、声明了 `claim_sessions` 的数据目录（只用于 live 测试隔离）→ 环境变量 `TSW_HOME` → 本仓库的 `.local/tsw-home` → `~/.tsw`。`tsw home` 显示找到的目录和依据。
- **当前目录**只影响两件事：上面的 `claim_sessions`，以及 `tsw review run`（在 worker 的 worktree 中评审当前 HEAD）。其他命令在哪个目录运行都一样；`backup`、`restore` 的相对路径按当前目录解析。
- 这些命令由你在**会话外的终端**中运行。Claude 会话中只能运行只读命令，写操作经 tsw MCP 工具；验收、退回、取消和 Dashboard 应用在任何会话中都会被拒绝，以免会话替你做决定（[DN006 §8.10](docs/design/DN006-plugin-layer.md)）。

### 需要你运行的命令

```text
tsw home                                     显示数据目录与定位依据
tsw init --home <目录> --remember            初始化数据目录并记为本仓库的数据目录
tsw host install-hint                        显示 Claude Code 插件的安装步骤
tsw host render                              更新主会话的 CLAUDE.md、skill 与插件副本（产品更新后）
tsw dashboard                                打开 TaskSteward Dashboard：浏览器中查看任务，一键验收、退回、取消
                                             （与 tsw status serve 相同）
tsw task accept T255 [--note "看过报告"]     验收待验收的任务（先显示任务并要求确认；--yes 跳过）
tsw task reject T255 --reason "缺少导出"     退回待验收的任务
tsw task cancel T255                         取消任务（有运行中的 worker 时须先由主会话停止）
tsw lease show                               查看调度租约
tsw lease takeover --session <新主会话 ID> --from-session <原主会话 ID>
                                             原主会话已停止时接管租约（另一台机器上的加 --from-machine）
tsw lease release --session <主会话 ID>       释放租约
tsw backup <空目录>  /  tsw restore <备份>   在线备份 / 恢复到空的数据目录
tsw quiesce  /  tsw resume-writes            静止数据目录（之后可直接复制）/ 解除
tsw ledger audit  /  tsw ledger rebuild      审计账本 / 从账本重建投影
tsw config show  /  tsw config validate      查看 / 校验统一配置
```

`tsw dashboard`（即 `tsw status serve`）打开 TaskSteward Dashboard。它只监听本机，每次启动输出一个带一次性令牌的地址并在浏览器中打开；页面与数据目录中的 `views/STATUS.html` 相同，另有按钮，报告与 PR 链接照常可点。页面有三种视图：按状态分组、按时间顺序，以及按依赖关系画出的任务流（最新的任务在左，依赖的任务在右，每个仓库一条泳道）；顶部的计数卡只列出有任务的状态。筛选、视图与滚动位置在自动刷新后保留。每张卡片的「详情」打开该任务的单任务视图（`/tasks/<任务>`），列出需求与历史 revision、每个 attempt 与它的会话、提交与评审轮次、产出、依赖、问题和时间线；有会话的 attempt 带会话链接，点击在 Claude 桌面 app 中打开该会话。按 Ctrl+C 停止。`views/STATUS.html` 仍在每次提交后生成，可以离线打开，但没有按钮。

开发者另需安装开发工具（`pip install -r requirements-dev.txt`），并用 `python tools/gate.py` 运行本地门禁。

## 仓库布局

```text
task-steward/
  docs/design/            设计文档（md + html）与图
  docs/development/       开发规则
  docs/decisions/         决定记录
  docs/use-cases/         use-case 目录与状态迁移清单
  plugin/                 Claude Code 插件：hooks、MCP 配置（主会话 skill 由 tsw host render 生成在数据目录）
  tsw/                    核心库与 CLI：数据目录、存储、账本、投影、任务（逐步扩展）
  tests/                  unit / component / surface / conformance 测试
  tools/                  门禁、文档生成与检查（live 场景在后续里程碑加入）
```

## 许可

[MIT](LICENSE)
