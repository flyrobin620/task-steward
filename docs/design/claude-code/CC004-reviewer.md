# CC004 Claude Code 评审器

- 状态：设计草案（T242 r2 自 DN004 拆出；T325 改为子代理评审器，待用户确认）
- 日期：2026-11-21
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md)

## 1. 范围

本文是 DN004 评审器在 Claude Code 上的实现：会话适配器 `claude-subagent`，用 worker 会话中的只读子代理评审（DN004 §3 的 worker 子代理方式）。评审包、输出契约、处置规则与轮次计数见 DN004。用户的选择：“claude 自己的reviewer用subagent做就可以了，不用专门用claude headless或者cli”（T308 会话，M-T308-10）。此前的 `claude -p` 方案存档于附录 A。

## 2. 子代理定义

tsw 插件提供子代理 `agents/reviewer.md`，宿主中的名称为 `task-steward:reviewer`：

```markdown
---
name: reviewer
description: TaskSteward 自评审的只读评审子代理。只在 review_run 给出启动指示时使用。
tools: Read, Grep, Glob
disallowedTools: Bash, PowerShell, Edit, Write, NotebookEdit, Agent, Skill, Workflow
---
（正文：只读评审者的守则：读取提示中给出的评审请求并照做；只读查阅执行目录与评审包；
最后一条回复只输出满足 DN004 §4 的 JSON。）
```

- **只读由工具白名单强制**：`tools` 给出时子代理只有列出的工具，不继承 MCP 工具；`disallowedTools` 先于 `tools` 生效，再排除命令、编辑、子代理与工作流工具，避免宿主以后新增的默认工具间接写入。插件子代理不支持 `hooks`、`mcpServers`、`permissionMode`（宿主出于安全原因忽略），本定义不使用它们。
- **不写 `model`**：模型由每次启动时的参数决定（§3），定义中不固定。
- 正文只写守则，不写评审要点：要点与输出契约在每轮的评审请求里，随产品更新立即生效；子代理定义随插件更新，需要宿主重新加载插件后才生效（CC006）。

## 3. 一轮评审的步骤

1. worker 调用 `review_run`。tsw 开轮、写 `review/rK-request.md` 与 `rK-diff.patch`，返回：

   ```json
   {"ok": true, "round": 2, "status": "awaiting_agent",
    "agent": {"subagent_type": "task-steward:reviewer", "model": "sonnet", "effort": "high",
              "description": "自评审第 2 轮",
              "prompt": "你是 TaskSteward 任务 T001 的只读评审子代理。完整读取评审请求 <数据目录>/runs/T001/a1/review/r2-request.md 并照做……最后只输出 JSON。"}}
   ```

2. worker 用 `Agent` 工具启动子代理：`subagent_type`、`model`、`effort`、`description`、`prompt` 原样使用，前台运行（不设后台），不改写提示。提示只指向请求文件，请求正文不经过 worker 的上下文，worker 也无从删改。
3. 子代理结束后，worker 调用 `review_run`：`round` 为第 1 步的轮次，`result` 为子代理最终回复的原文（含 JSON 的整段文字，tsw 自己提取 JSON），`agent_id` 为宿主返回的子代理 ID。子代理报错、被中断、宿主找不到 `task-steward:reviewer`（插件未更新）时，改带 `error` 写明原因。
4. tsw 解析、校验、核对 `reviewed_head`，写 `rK.json`、`rK-run.json`（记 `agent_id` 与请求的模型），记录 `review_complete`；不满足契约时写 `rK-raw.txt` 并记录 `review_fail`（E02，之后不带 `result` 再调用 `review_run` 即重试）。

worker 的流程说明（`plugin/roles/worker.md`）写明这几步；`review_run` 的返回结果也带同样的简短说明，worker 不必记住格式。

## 4. 模型、effort 与独立性

- **模型**：`roles.reviewer.model` 取 `Agent` 工具接受的别名之一：`sonnet`、`opus`、`haiku`、`fable`（配置校验拒绝其他值）。别名由宿主解析为当前版本，tsw 拿不到子代理实际使用的模型，`rK-run.json` 只记录请求的别名。宿主的强制设置（例如强制所有子代理用同一模型的环境变量）可能覆盖它，这一点 tsw 无法发现，写在已知限制中。
- **effort**：`roles.reviewer.effort` 原样作为 `Agent` 工具的 `effort` 参数（`low`…`max`）；这是 tsw 流程说明明确要求的取值，worker 不自行改动。
- **默认**：安装时选择 `claude-subagent` 写入 `model: "sonnet"`、`effort: "high"`，与 worker 默认的 `claude-opus-5-5` 不同。配置校验在评审器与 worker 属于同一模型家族（别名 `opus` 对 `claude-opus-*`）时警告。
- **独立性**：子代理有自己的上下文，只读评审请求与它自己读到的文件，不继承 worker 的对话；它与 worker 在同一宿主、同一账号，独立程度低于 Codex 或 Copilot CLI。
- **信任边界**：结果经 worker 转交（DN004 §3.3）。

## 5. 能力声明

| 能力 | 值 | 说明 |
| --- | --- | --- |
| `execution` | `delegated` | 由 worker 的模型启动，tsw 不能直接调用 |
| `oneshot` / `oneshot_via` | 是 / `worker_agent` | 一轮一次子代理，结果经 worker 交回（DN006 §7.1） |
| `read_only_enforced` | 是 | 子代理定义的工具白名单 |
| `incremental` | 否 | 每轮新子代理：全量范围，附上一轮意见、处置与增量 diff。宿主虽能按子代理 ID 续用，但跨 worker 上下文压缩与会话重启是否可靠未验证，不采用 |
| 其他 | 不承载 worker，用户不能直接对话，无唤醒 | — |

配置校验：`claude-subagent` 要求 worker 的会话适配器在同一宿主（`roles.worker.session` 为 `claude-app`）。评审请求在数据目录的 `runs/` 下、执行目录之外：子代理继承 worker 的权限模式（tsw 建的 worker 为 `auto`），读取它不需要逐次批准。

## 6. 验证

- surface：MCP `review_run` 两次调用的完整往返（启动指示、带 `result` 的完成、带 `error` 的失败、不合契约时的失败与重试），插件包含 `agents/reviewer.md` 且工具白名单如上。
- live（`python tools/live.py reviewer --reviewer claude-subagent`）：在临时数据目录与临时仓库中，以 `claude -p --plugin-dir <检出>/plugin` 充当 worker，按 `review_run` 的指示启动真实子代理，完成一次全量评审与一次后续轮次，核对只读（执行目录与 `.git` 不变）与输出契约。这里的 `claude -p` 只是 live 场景里代替桌面 app worker 的宿主，评审本身由子代理完成。

## 附录 A：`claude-headless` 调用约定（存档，未实现）

PoC 2026-11-05 实测的 `claude -p` 一次性评审约定，T325 起不采用（DN004 附录 A）：

- **清除继承的宿主环境变量**：tsw 在桌面 app 的会话里运行，进程继承了 `CLAUDE_*` / `ANTHROPIC_*` 等宿主变量，命令行会因此以为认证由宿主提供而报“未登录”。启动评审进程前删除这些变量，让命令行读取用户设置中的认证或网关配置。
- **严格只读**：`--allowedTools Read,Grep,Glob`，`--disallowedTools Write,Edit,Bash,NotebookEdit,ExitPlanMode,Agent,Workflow,Skill`（子代理与工作流工具可能间接写入，也要禁用），`--permission-mode dontAsk`。不使用 plan 模式：它不写目标文件，但会把计划文件写入用户目录。
- **设置来源**：`--setting-sources user`，以便读取用户的认证或网关配置；这也会加载用户级 hooks，评审结果只以 JSON 输出为准，hooks 的报错不影响评审。
- **显式模型**：总是传 `--model roles.reviewer.model`，不依赖用户的默认模型。
- **输出与续用**：`--output-format json`，从 `result` 字段解析评审 JSON；增量评审用 `--resume <session_id>` 续用同一会话。
