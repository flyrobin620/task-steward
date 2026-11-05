# AGENTS.md

TaskSteward（任务管家）：可插拔宿主的任务编排器。核心库与 CLI `tsw` 用 Python 3.10+ 标准库实现，
默认宿主是 Claude Code 桌面 app。设计见 [docs/design/](docs/design/)（DN001–DN007）。

本文件对所有任务生效。专题规则在第 2 节列出的文档里，开始某类工作前先读对应文档。规则变化时在规则
所在位置修改，并记入 [docs/development/instructions-log.md](docs/development/instructions-log.md)；
不要在这里追加带日期的历史。

## 1. 命令

| 用途 | 命令 |
| --- | --- |
| 本地门禁（lint、类型、文档、目录检查、全部 CI 测试层级） | `python tools/gate.py` |
| 只跑某个层级 | `python tools/gate.py --tier unit`（`component` / `surface` / `conformance`） |
| live 层级（真实宿主与评审器，只在本机） | `python tools/live.py <scenario>` |
| 生成与检查文档 HTML | `python tools/docs.py render` / `python tools/docs.py check` |
| 状态机图 | `python -X utf8 docs/design/diagrams/draw.py`，再运行同目录 `check.py` |

总是通过这些包装器运行，不要直接调用测试运行器。包装器尚不存在时，报告为“未验证（NOT VERIFIED）”，
不能报告为通过。

## 2. 开始以下工作前，先读…

链接不会自动加载文档，开始前自己读。

| 工作 | 先读 |
| --- | --- |
| 从改代码到合并的完整流程、每步门禁与失败处理 | [docs/development/workflow.md](docs/development/workflow.md) |
| 写或改测试；决定某个行为需要哪些测试层级 | [docs/development/testing-layers.md](docs/development/testing-layers.md) |
| 测试用到的数据目录、git 仓库、子进程环境；live 层级的会话与清理 | [docs/development/test-environments.md](docs/development/test-environments.md) |
| 新增或改变行为、CLI 命令、MCP 工具、hooks、状态迁移 | [docs/development/use-cases.md](docs/development/use-cases.md) |
| 改 `.github/workflows/**`、`tools/gate.py` 或其他 CI 包装器 | [docs/development/ci.md](docs/development/ci.md) |
| 开 PR、评审、回复评审意见、合并 | [docs/development/review.md](docs/development/review.md) |
| 改 lint / 类型检查配置、依赖、锁定版本 | [docs/development/code-quality.md](docs/development/code-quality.md) |
| 改账本事件、配置、数据目录、MCP 工具、CLI 输出、插件接口 | [docs/development/compatibility.md](docs/development/compatibility.md) |
| 改设计文档、图、README，或任何公开文档 | [docs/development/docs.md](docs/development/docs.md) |
| 版本号、打包、插件发布 | [docs/development/release.md](docs/development/release.md) |
| 改本文件或任何规则文档 | [docs/development/instructions-log.md](docs/development/instructions-log.md) |
| 记录或依赖一项决定 | [docs/decisions/README.md](docs/decisions/README.md) |

## 3. 工作约定

- 改动请求的含义是改动已合并且已验证，不只是写完。按第 4 节走完整个循环，中途不停下来问是否推送或
  开 PR。
- 只在以下情况停下来问：请求有多种合理理解且理解错代价高；下一步是任务没有要求的不可逆动作；缺少的
  信息只有人能提供；或改动属于第 4 节需要用户确认合并的范围。CI 变红、评审意见、合并冲突都属于循环
  本身，不是停下的理由。
- 不可逆动作包括删除数据、改写已发布的历史、改动仓库设置与保护规则。
- 方案不确定或会改变设计中已定义的行为时，先改设计：在 `docs/design/` 新增或更新设计文档并取得用户
  确认。设计批准不等于实现批准。
- 已确认的决定记在 `docs/decisions/`；提议与未决问题留在设计文档。

## 4. 开发循环

1. 改代码，同一改动里新增或更新测试；行为变化时同步更新 use-case 目录与状态迁移清单。
1. 运行本地门禁。门禁红时绝不推送，哪怕只改一行。
1. 改动触及宿主集成、会话适配器或评审协议时，在本机运行对应的 live 场景
   （[testing-layers.md](docs/development/testing-layers.md)）。
1. 按模板开 PR，同时启动独立评审（与 CI 并行）。
1. 修完已知问题，重新运行本地门禁，然后一次推送。
1. 合并条件：`Gate` 在最终 head 上为绿，评审在最终 head 上通过，每条评审意见都已回复并解决；
   由编排器派发的任务还需编排者验收。满足后由 agent 合并，以下改动须用户明确确认后才能合并：
   `.github/**`、`tools/` 中的门禁与 CI 包装器、`AGENTS.md`、`CLAUDE.md`、`docs/development/**`、
   `docs/decisions/**`、`docs/design/**`、发布与版本配置、权限与身份校验相关代码（角色解析、hooks 守卫、
   MCP 写工具授权）、`LICENSE`。
1. 完成的含义：合并提交在 `main` 上的 CI 通过，验收标准逐项核实，要求的 live 场景已在最终 head 上运行
   通过。“测试通过”“CI 绿”“已合并”都不等于完成。

每一步的门禁、证据和失败处理见 [docs/development/workflow.md](docs/development/workflow.md)。

## 5. 汇报

- 写明运行了哪些测试层级（数量与结果），以及哪些没有运行、为什么。真实、模拟（stub / 录制回放）和
  故障注入的证据分开写，不能把一种说成另一种。
- 不能运行的门禁报告为“未验证”，不能报告为通过。附上运行链接或结果文件路径。

## 6. 评审

- 评审意见是假设。先核实它能否经受支持的入口触发、影响多大、证据是什么，然后修复，或说明理由拒绝。
  每条意见都要回复。
- 把意图、必须保持的不变量和刻意的取舍告诉评审者，避免它们被当作问题反复提出。

## 7. 不变量（细节见链接文档）

- `tsw/` 运行时只依赖 Python 标准库；开发工具可以有依赖，但不进入运行时。
- 所有状态变更都经 tsw 写入账本；账本事件只追加，不改写、不删除。
- 测试与工具绝不读写用户真实的数据目录、`~/.claude`、`~/.codex` 或真实业务仓库，也不调用模型；
  调用真实宿主或模型的只有 live 层级，且只在本机运行。
- 秘密与令牌绝不出现在代码、日志、命令行参数、URL、命令输出、测试报告、CI 产物或提交中。
- 别人依赖的契约只增不删（字段、事件类型、工具、命令、选项）；不兼容变更按
  [compatibility.md](docs/development/compatibility.md) 走设计与版本流程。
- 不为测试另写一套生产行为的实现；测试真实组件，只替换本仓库之外的平台。
- 警告即错误。不要为了变绿而压制 lint、类型检查或测试警告。

## 8. 仓库约定

- 文档与规则用中文；代码、标识符、commit message、PR 标题用英文。PR 标题格式为
  `<area>: <summary>`，squash 合并。
- 本仓库公开：不提交工作身份、雇主内部信息、内部主机名、本机路径等私有上下文（检查方法见
  [docs.md](docs/development/docs.md)）。提交用公开的 GitHub noreply 身份。
- 生成文件与私有文件（`.local/`、测试结果、运行清单）不进入版本库；生成的东西不放在仓库根目录。
- 提交前检查暂存路径，运行 `git diff --cached --check`，并扫描凭据。
- 文档与行为在同一个 PR 里保持一致；不要写会过时的硬编码数量。
- 子目录中的 `AGENTS.md` 为该子树追加规则。
