# TaskSteward（任务管家）

一个可插拔宿主的任务编排器，默认宿主是 **Claude Code 桌面 app**，也可以经插件层接入 Codex、GitHub Copilot 或自建的远程会话中枢。

- 主会话（orchestrator）是 app 中的一个会话。
- 每个任务由主会话启动的关联子会话（worker）执行。
- worker 完成后自己调用评审器（默认 Codex gpt-6-astra · medium）评审成果，并决定是否迭代；评审预算用完或遇到问题时，先由主会话判断，确需人类时再经渠道请求。
- worker 定稿后由主会话技术验收，最后交用户人工验收。
- 状态由事件账本驱动，可导入 orchestrator-agent 的全部历史。
- 运行数据（配置、任务书、账本、报告）放在与本 repo 分离的数据目录 `TSW_HOME`，不提交到本 repo；备份该目录即可。

当前阶段：**产品设计**（T233）。本仓库目前只有设计文档，尚未实现运行代码。

## 设计文档

每份文档都有 Markdown 与 HTML 两个版本，内容一致；HTML 可离线在浏览器打开并打印。

| 文档 | Markdown | HTML | 内容 |
| --- | --- | --- | --- |
| DN001 总体架构 | [md](docs/design/DN001-architecture.md) | [html](docs/design/DN001-architecture.html) | 目标、组件、会话模型与平台约束、工具面、hooks、存储、关键决策、待 PoC 验证的假设 |
| DN002 任务状态机 | [md](docs/design/DN002-task-state-machine.md) | [html](docs/design/DN002-task-state-machine.html) | 按主控视角划分状态的单张状态机（含 worker 内部评审子状态）与导入映射；完整迁移表、守卫条件、attempt 阶段、事件类型、不变量、异常恢复 |
| DN003 统一配置 | [md](docs/design/DN003-unified-config.md) | [html](docs/design/DN003-unified-config.html) | `config/task-steward.json` 完整示例、字段说明、校验与生效规则 |
| DN004 worker 自评审 | [md](docs/design/DN004-worker-self-review.md) | [html](docs/design/DN004-worker-self-review.html) | Codex Astra 评审调用、输出契约、处置规范、迭代决策、主控复核 |
| DN005 历史导入与 resume | [md](docs/design/DN005-history-import-and-resume.md) | [html](docs/design/DN005-history-import-and-resume.html) | 导入来源、分类规则、导入步骤、resume 资料包、验证与回退 |
| DN006 插件层 | [md](docs/design/DN006-plugin-layer.md) | [html](docs/design/DN006-plugin-layer.html) | 宿主集成、会话适配器（含一次性评审）、人类渠道、存储后端四类插件的职责与接口；委托操作；主会话判断；数据目录；组合示例 |
| DN007 账本与上下文检索 | [md](docs/design/DN007-ledger-and-context.md) | [html](docs/design/DN007-ledger-and-context.html) | 三层存储、写读路径与重建；任务卡片、检索工具、用户决定与偏好；主会话与 worker 的历史读取方式 |

图的结构来源是 `docs/design/diagrams/*.mmd`（状态、迁移编号与标签的权威来源），文档引用同名 `.svg`：

- 彩色 SVG 由 `docs/design/diagrams/draw.py` 按手工布局生成（读取 `.mmd` 的标签），运行 `python -X utf8 docs/design/diagrams/draw.py`；
- `docs/design/diagrams/check.py` 校验 XML 合法性，以及每条迁移、节点、编号是否都在 SVG 中可见，结果写入 `validation.json`；
- `docs/design/diagrams/auto/` 下是 Mermaid 自动布局的全量拓扑图，仅作结构核对。

修改 `.mmd` 后需要同步调整 `draw.py` 中的布局坐标，再运行两个脚本。

## 仓库布局（规划）

```text
task-steward/
  docs/design/            设计文档（md + html）与图
  plugin/                 Claude Code 插件：skills、hooks、MCP 配置（实现阶段）
  tsw/                    核心库与 CLI：账本、配置、门禁、渲染、导入、Codex 评审适配器（实现阶段）
  tests/                  使用临时仓库与模拟平台响应的测试（实现阶段）
```

## 许可

[MIT](LICENSE)
