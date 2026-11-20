# CC004 Claude Code 评审器

- 状态：设计草案（T242 r2 自 DN004 拆出，待用户确认）
- 日期：2026-11-08
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md)

## 1. 范围

本文是 DN004 评审器在 Claude Code 上的实现：没有 Codex 时使用的 `claude-headless`。评审包、输出契约、处置规则与轮次计数见 DN004。

## 2. 调用约定

用 Claude Code 命令行一次性评审（PoC 2026-11-05 实测的调用约定）：

- **清除继承的宿主环境变量**：tsw 在桌面 app 的会话里运行，进程继承了 `CLAUDE_*` / `ANTHROPIC_*` 等宿主变量，命令行会因此以为认证由宿主提供而报“未登录”。启动评审进程前删除这些变量，让命令行读取用户设置中的认证或网关配置。
- **严格只读**：`--allowedTools Read,Grep,Glob`，`--disallowedTools Write,Edit,Bash,NotebookEdit,ExitPlanMode,Agent,Workflow,Skill`（子代理与工作流工具可能间接写入，也要禁用），`--permission-mode dontAsk`。不使用 plan 模式：它不写目标文件，但会把计划文件写入用户目录。
- **设置来源**：`--setting-sources user`，以便读取用户的认证或网关配置；这也会加载用户级 hooks，评审结果只以 JSON 输出为准，hooks 的报错不影响评审。
- **显式模型**：总是传 `--model roles.reviewer.model`，不依赖用户的默认模型。
- **输出与续用**：`--output-format json`，从 `result` 字段解析评审 JSON；增量评审用 `--resume <session_id>` 续用同一会话。
