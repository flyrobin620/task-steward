# CC004 Claude Code 评审器

- 状态：设计草案（T242 r2 自 DN004 拆出；T325 改为子代理评审器；T331 改回由 tsw 启动的 `claude -p`，删除子代理评审器；T330 §8 列出可用模型，待用户确认）
- 日期：2026-11-21
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md) · 能力声明：[DN006 §7.3](../DN006-plugin-layer.md)

## 1. 范围

本文是 DN004 评审器在 Claude Code 上的实现：会话适配器 `claude-headless`，同步方式（DN004 §3），tsw 启动 `claude -p` 非交互进程、等它结束、从 stream-json 输出中读回最终消息并记账。评审包、输出契约、处置规则与轮次计数见 DN004。

用户的选择（T331，2026-11-21，主会话）：“评审器由 tsw 自己启动并读回结果，然后记账，结果不经过 worker 的手。>> 核心点在这里。reviewer是由tsw启动的。所以codex和copilot-cli就都是对的。所以正确的改法其实是换回claude-headless， 由tsw启动，就行了？”，随后“F-T325-3 转任务，删掉 claude-subagent”。T325 的子代理评审器（`claude-subagent`）因此删除，存档于附录 A。

## 2. 实测（T331，Claude Code 2.1.302，Windows）

在执行目录下的临时 git 仓库中实测（模型 `sonnet` / `haiku` / `claude-sonnet-5-5`）：

| 项 | 结果 |
| --- | --- |
| 非交互与输出 | `-p <提示> --output-format stream-json --verbose` 每行一个事件：`system/init` 带 `session_id`、`model`、`tools`、`mcp_servers`、`permissionMode`；`assistant` 事件的 `message.model` 是产生这条回复的模型；最后一行 `result` 带 `session_id`、`is_error`、`result`（最终回复文字）与 `modelUsage` |
| 只读 | `--tools Read,Grep,Glob` 后 `init.tools` 只有这三个；`--permission-mode dontAsk` 下读取工作目录与 `--add-dir` 之外的文件被拒绝（`permission_denials` 记下，模型收到拒绝），目录中没有新文件 |
| hooks 与插件 | 不加 `--safe-mode` 时用户安装的插件（含 tsw 插件）的 `SessionStart`、`UserPromptSubmit`、`Stop` hooks 都在评审进程里运行；加上后不再运行（`--include-hook-events` 中没有 hook 事件），认证与模型选择照常 |
| MCP | `--strict-mcp-config` 且不给 `--mcp-config` 时 `init.mcp_servers` 为空 |
| 会话 ID 与续用 | `--session-id <UUID>` 为新会话指定 ID，`result.session_id` 与之一致；`--resume <UUID>` 续用同一会话，模型记得上一轮的内容，ID 不变 |
| 模型 | `--model sonnet` 时 `init.model` 为 `claude-sonnet-5-5[1m]`，`assistant.message.model` 为 `claude-sonnet-5.5`；`--model claude-sonnet-5-5` 时 `init.model` 为 `claude-sonnet-5-5`。不存在的模型：退出码 1，`result.is_error` 为 true，回复是 `<synthetic>` 模型的“API Error: 400 Unknown model” |
| 认证 | 清除继承的 `CLAUDE*`、`ANTHROPIC*`、`TSW*` 环境变量、`--setting-sources user` 后，命令行用用户自己的登录或用户设置中的网关配置，正常完成 |
| 信任 | `-p` 跳过目录信任确认（命令行帮助写明），不需要在任务 worktree 预先信任 |

## 3. 调用约定

```text
claude -p <提示> --output-format stream-json --verbose
       --session-id <UUID>                     # 增量轮次改为 --resume <上一轮的 UUID>
       --model <roles.reviewer.model> --effort <roles.reviewer.effort>
       --tools Read,Grep,Glob
       --disallowedTools Bash,PowerShell,Edit,Write,NotebookEdit,Agent,Skill,Workflow,WebFetch,WebSearch
       --permission-mode dontAsk --strict-mcp-config --safe-mode --setting-sources user
       --add-dir <runs/Txxx/aN/review> --add-dir <work/Txxx/aN>
```

- **工作目录**：任务的 worktree（与其他评审器相同的执行目录）。`--add-dir` 加入评审包目录与 attempt 的执行目录（报告草稿在 worktree 之外）。文件工具只能访问这几处。
- **只读**：`--tools` 只把三个只读工具交给模型；`--disallowedTools` 再排除命令、编辑、子代理、技能、工作流与联网工具，避免宿主以后新增的默认工具间接写入；`dontAsk` 拒绝一切需要批准的操作。不使用 plan 模式：它不写目标文件，但会把计划文件写入用户目录。
- **隔离**：`--safe-mode` 不加载用户安装的插件、hooks、技能、CLAUDE.md 与输出风格（tsw 插件的 hooks 不会在评审进程里把它当作某个会话处理）；`--strict-mcp-config` 不启动任何 MCP server；`--setting-sources user` 只读用户设置（认证、网关），不读被评审仓库的 `.claude/settings*.json`（其中的 `apiKeyHelper`、`env` 等不由被评审的代码决定）。仓库约定（AGENTS.md、CLAUDE.md）由评审请求指明，评审者自己只读查阅。
- **环境变量**：启动前删除继承的 `CLAUDE*`、`ANTHROPIC*`、`TSW*` 变量。tsw 在桌面 app 的会话里运行时，进程继承了宿主的会话、认证与网关变量，命令行会因此以为认证由宿主提供而失败，或连到宿主自己的会话通道；删除后由用户设置中的认证或网关配置决定（T327 用 `claude --bg` 实测过同一做法）。
- **提示**：只指向评审请求文件（`rK-request.md`），不把请求正文放在命令行上，避免 Windows 命令行长度限制；评审器不能运行命令，本轮 diff 由 tsw 写入 `rK-diff.patch`（DN004 §3.1）。
- **会话 ID**：首轮由 tsw 生成 UUID，记入 `rK-run.json` 的 `thread_id`；`review.incremental` 为 `resume_thread` 时下一轮 `--resume` 同一 ID，只审增量。
- **显式模型**：总是传 `--model`，不依赖用户的默认模型；不传 `--fallback-model`。
- **可执行文件**：`roles.reviewer.executable`（本机覆盖，DN003 §5），缺省在 `PATH` 中找 `claude`。
- **进程**：与 Codex、Copilot 相同，用 `tsw/review/process.py` 的进程树启动，`timeout_seconds` 到期时结束整个进程树并判为本轮失败。stdin 关闭，不交互。

## 4. 结果判定

本轮失败（`review_fail`，保存原始输出）的情形：

- 进程启动失败、超时、退出码不为 0，输出中没有 `result` 事件，或 `result.is_error` 为 true；
- **模型无法核实或不一致**：输出中没有 `init.model`，也没有任何 `assistant` 事件的 `message.model`（`<synthetic>` 除外）；或其中任何一个与 `roles.reviewer.model` 不一致：**不回退**（DN004 §3.1），错误信息写明实际模型。比较前规范化：去掉 `[1m]` 之类的后缀、小写、`.` 换成 `-`（宿主在不同事件中写 `claude-sonnet-5.5` 与 `claude-sonnet-5-5`）。配置为完整模型 ID（如 `claude-sonnet-5-5`）时须相同；配置为别名（`sonnet`、`opus`、`haiku`、`fable`）时须是该家族的模型（`claude-sonnet-*`），别名对应哪个版本由命令行决定。`result.modelUsage` 不参与比较（宿主可能为辅助用途调用其他模型）；
- 最终回复（`result.result`，没有时取最后一条 `assistant` 事件的文字）不是 JSON 或不满足契约；
- `result.session_id` 与请求的会话 ID 不同。

`rK-run.json` 记录实际模型（`model_used`，规范化后的 `init.model` 与回复的 `model`）、会话 ID 与是否续用。

## 5. 能力声明

| 能力 | 值 | 说明 |
| --- | --- | --- |
| `execution` | `direct` | tsw 直接启动进程 |
| `oneshot` | 是 | 一轮一次 `claude -p` |
| `read_only_enforced` | 是 | `--tools`、`--disallowedTools`、`dontAsk` |
| `incremental` | 是 | `--resume` |
| `interactive` | 否 | 不承载 worker（待验证，DN006 §7.3） |
| 其他 | 用户不能直接对话，无唤醒 | — |

与 worker 在同一宿主、可能同一账号；配置校验在评审模型与 worker 模型相同（含别名对应的家族）时警告。

## 6. 已知限制

- 会话记录写在用户的 Claude Code 目录（`~/.claude/projects/` 下按工作目录命名的子目录），与 Codex 线程、Copilot 会话记录相同，tsw 不清理。
- 评审器不能运行命令：不跑测试，不运行 `git`，靠 `rK-diff.patch` 与读文件评审（DN004 §3.1）。
- 认证只来自用户自己的 Claude Code 登录或用户设置：只在宿主（桌面 app）里登录、命令行从未登录的机器上每轮失败，错误信息提示在终端运行一次 `claude` 登录。用户在系统环境中设置的 `ANTHROPIC_*`、`CLAUDE_*` 变量同样被清除，要写进用户设置的 `env`。
- 管理策略（managed settings）仍然生效：它固定了别的模型时，每轮按模型不一致失败，用户把 `roles.reviewer.model` 改为策略允许的模型。

## 7. 验证

- conformance / surface：以录制的 stream-json（来自 §2 的实测，去掉正文与本机路径）回放，覆盖完成、续用、模型不一致（含别名与家族）、`is_error`、非 JSON、会话 ID 不一致、超时，以及命令行参数（只读工具、隔离选项、清除的环境变量）。
- live（`python tools/live.py reviewer --reviewer claude-headless`）：真实 `claude -p` 在固定样例上做一次全量与一次增量评审，核对只读（执行目录与 `.git` 不变）、续用与输出契约。

## 8. 列出可用模型（T330）

DN004 §3.4 的模型列表在 Claude Code 上用 `claude -p` 的控制协议：Agent SDK 的 `supportedModels()` 就是向命令行发控制请求 `initialize`，从回应中取 `models`。

```text
claude -p --input-format stream-json --output-format stream-json --verbose
       --tools Read --permission-mode dontAsk --strict-mcp-config --safe-mode --setting-sources user
```

1. 环境变量与工作目录按 §3 清理与选择（`CLAUDE*`、`ANTHROPIC*`、`TSW*` 删除，用户设置中的认证与网关照常），所以列表就是评审进程能用的模型。
2. 向 stdin 写一行 `{"type": "control_request", "request_id": <ID>, "request": {"subtype": "initialize"}}`，等 `type` 为 `control_response`、`request_id` 相同的一行。
3. `response.response.models` 是模型数组：`value`（`--model` 接受的值）、`resolvedModel`、`displayName`、`description`、`supportedEffortLevels`。写进配置的 ID 是 `value` 去掉 `[1m]` 之类的上下文后缀（配置中的模型 ID 不带后缀，DN003 §1）。
4. 不发送任何用户消息，关闭输入，结束进程树。

列表处理：去掉 `value` 为 `default` 的项（随用户的默认模型变化，§3 总是显式传模型）；别名（`sonnet`、`opus`、`haiku`、`fable`）照列表原样给出，名称旁附 `resolvedModel`，选别名时写别名，结果判定按 §4 的家族规则。

实测（T330，Claude Code 2.1.302，Windows，用户设置中配置了网关）：回应在启动后几秒内到达，`models` 有 8 项：`default`、四个别名（`resolvedModel` 分别为 `claude-opus-5-5[1m]`、`claude-fable-5-1`、`claude-sonnet-5-5[1m]`、`claude-haiku-5-5[1m]`）与三个网关提供的完整 ID；每项都有 `supportedEffortLevels`（`low` 到 `max`）。没有发送消息，不调用模型，也没有在用户的 Claude Code 目录留下会话记录。回应中还有账号信息（`account`），tsw 不读取、不记录。

取不到列表的情形：找不到 `claude`、60 秒内没有回应、回应中没有 `models`。未登录的命令行是否仍返回列表未验证；即使返回，评审时也会按 §6 失败。按 DN004 §3.4 的退路处理。

## 附录 A：`claude-subagent` 存档（T325 实现，T331 删除）

T325 把 Claude 评审器做成 worker 会话中的只读子代理（用户 T308 会话 M-T308-10：“claude 自己的reviewer用subagent做就可以了，不用专门用claude headless或者cli”）。做法：

- 插件提供子代理 `agents/reviewer.md`（宿主中的名称为 `task-steward:reviewer`），`tools: Read, Grep, Glob`，`disallowedTools` 排除命令、编辑、子代理与工作流工具；不写 `model`。
- `review_run` 开轮、写好请求后立即返回启动指示（`status: awaiting_agent`，`agent` 中有子代理类型、模型别名、effort、描述与只指向请求文件的提示）；worker 用 `Agent` 工具原样启动，子代理结束后再带 `round` 与 `result`（子代理最终回复原文）或 `error`、`agent_id` 调用 `review_run`，tsw 按同样的规则解析、校验并记账。
- 模型只能是 `Agent` 工具接受的别名，tsw 拿不到子代理实际使用的模型；每轮新子代理，全量范围并附上一轮处置与增量 diff。

**删除的理由**：结果由 worker 转交。T325 之后用户审视 F-T325-3（用 hook 核对 worker 确实用了 `task-steward:reviewer`）时指出，worker 若有意操纵，可以改写或伪造子代理的回复；`codex-app-server` 与 `copilot-cli` 由 tsw 启动并读回结果、记账，结果不经过 worker，是正确的形态（§1 的用户原话）。Claude 评审器因此改为同一形态的 `claude-headless`，同时补上子代理拿不到的实际模型核实。worker 仍能影响评审的输入（范围起点、`notes`、代码中的注释），这是另一件事，不在本次范围。
