# CP004 Copilot CLI 评审器

- 状态：设计草案（T325；T330 §8 列出可用模型，待用户确认）
- 日期：2026-11-21
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md) · 能力声明：[DN006 §7.3](../DN006-plugin-layer.md)

## 1. 范围

本文是 DN004 评审器在 GitHub Copilot CLI 上的实现：会话适配器 `copilot-cli`，同步方式（DN004 §3），tsw 启动 `copilot -p` 非交互进程、等它结束、从 JSONL 输出中读回最终消息。它只做评审，不承载 worker（Copilot app 的宿主集成见 T277 的研究，另行设计）。评审包、输出契约、处置规则与轮次计数见 DN004。

## 2. 实测（T325，Copilot CLI 1.0.112，Windows）

在执行目录下的临时 git 仓库中实测 3 次（共 2 次 premium request，模型调用之外的启动失败不计费）：

| 项 | 结果 |
| --- | --- |
| 非交互与输出 | `-p <提示> --output-format json` 输出 JSONL，每行一个事件。`session.start` 带会话 ID 与 git HEAD；每条 `assistant.message` 带 `model` 与 `content`，最终回复的 `phase` 为 `final_answer`；最后一行 `result` 带 `sessionId` 与 `exitCode` |
| 会话 ID | `--session-id <UUID>` 为新会话指定 ID，`result.sessionId` 与之一致，tsw 不需要从输出中找 ID |
| 续用 | `--resume <UUID>` 续用同一会话：`session.resume` 事件出现，模型记得上一轮的结论 |
| 只读 | `--available-tools view grep glob` 只把这三个工具交给模型；另加 `--deny-tool write`、`shell`、`memory`（拒绝规则优先于任何允许规则）。要求它“用任何可用工具创建文件”时，它回答只有只读工具、没有创建，目录中也没有新文件 |
| MCP | 用户级 MCP server 照常启动，但 `--available-tools` 之外的工具都被禁用（`session.info` 列出 “Disabled tools”）；`--disable-builtin-mcps` 去掉内置的 GitHub MCP |
| 模型 | 本机的管理策略把模型固定为某一个（`session.model_change` 的 `source` 为 `managed_settings`，`session.info` 的 `infoType` 为 `managed_model_override`），`--model` 不起作用；不存在的模型在调用前以 `session.error`（“Model … is not available”）失败，`exitCode` 为 1，不计费 |
| 文件范围 | 文件工具只能访问工作目录与系统临时目录，`--add-dir` 增加目录 |

## 3. 调用约定

```text
copilot -p <提示> --output-format json --session-id <UUID>   # 增量轮次改为 --resume <上一轮的 UUID>
        --model <roles.reviewer.model> --reasoning-effort <roles.reviewer.effort>
        --available-tools view grep glob
        --deny-tool write --deny-tool shell --deny-tool memory --deny-tool url
        --add-dir <runs/Txxx/aN/review> --add-dir <执行目录>
        --no-ask-user --disable-builtin-mcps --no-auto-update --disallow-temp-dir
```

- **工作目录**：任务的 worktree（与其他评审器相同的执行目录）。`--add-dir` 加入评审包目录与执行目录（报告草稿在 worktree 之外）。
- **提示**：只指向评审请求文件（`rK-request.md`），不把请求正文放在命令行上，避免 Windows 命令行长度限制；diff 在 `rK-diff.patch`（DN004 §3.1）。
- **会话 ID**：首轮由 tsw 生成 UUID，记入 `rK-run.json` 的 `thread_id`；`review.incremental` 为 `resume_thread` 时下一轮 `--resume` 同一 ID，只审增量。
- **可执行文件**：`roles.reviewer.executable`（本机覆盖，DN003 §5），缺省在 `PATH` 中找 `copilot`。
- **进程**：与 Codex 相同，用 `tsw/review/process.py` 的进程树启动，`timeout_seconds` 到期时结束整个进程树并判为本轮失败。stdin 关闭，不交互。

## 4. 结果判定

本轮失败（`review_fail`，保存原始输出）的情形：

- 进程启动失败、超时，或 `result.exitCode` 不为 0，或输出中有 `session.error`；
- 任何 `assistant.message` 的 `model` 与 `roles.reviewer.model`（规范化后）不同，或 `session.model_change` 显示被管理策略换成了其他模型：**不回退**（DN004 §3.1），错误信息写明实际模型，用户可把 `roles.reviewer.model` 改为策略允许的模型；
- 最终回复（最后一条 `phase` 为 `final_answer` 的 `assistant.message`，没有时取最后一条不带工具请求的 `assistant.message`）不是 JSON 或不满足契约；
- `result.sessionId` 与请求的会话 ID 不同。

## 5. 能力声明

| 能力 | 值 | 说明 |
| --- | --- | --- |
| `execution` | `direct` | tsw 直接启动进程 |
| `oneshot` / `oneshot_via` | 是 / `process` | — |
| `read_only_enforced` | 是 | `--available-tools` 与 `--deny-tool` |
| `incremental` | 是 | `--resume` |
| 其他 | 不承载 worker，用户不能直接对话，无唤醒 | — |

## 6. 已知限制

- 会话记录写在用户的 Copilot 目录（`~/.copilot/session-state/`），与 Codex 线程留在 Codex 目录相同；tsw 不清理。
- 评审器不能运行命令：不跑测试，不运行 `git`，靠 `rK-diff.patch` 与读文件评审（DN004 §3.1）。
- 管理策略固定的模型只能照用：`roles.reviewer.model` 必须写成策略允许的那一个，否则每轮都失败。T330 实测时本机已没有这项策略（§8）。
- 用户级 MCP server 仍会启动（只是工具被禁用），首次启动可能要十几秒。

## 7. 验证

- conformance / surface：以录制的 JSONL（来自 §2 的实测，去掉正文与本机路径）回放，覆盖完成、续用、模型被替换、`session.error`、非 JSON、超时。
- live（`python tools/live.py reviewer --reviewer copilot-cli`）：真实 Copilot CLI 在固定样例上做一次全量与一次增量评审，核对只读与输出契约。

## 8. 列出可用模型（T330）

DN004 §3.4 的模型列表在 Copilot CLI 上用它的 ACP 服务（Agent Client Protocol，`copilot --acp`，stdio JSON-RPC）：

1. 启动 `copilot --acp --no-auto-update --disable-builtin-mcps`，工作目录为一个空的临时目录。
2. `initialize`（`protocolVersion: 1`，客户端不提供文件与终端能力），再 `session/new`（`cwd` 为同一临时目录，`mcpServers` 为空）。
3. 结果的 `models.availableModels` 是可用模型（`modelId`、`name`、`description`，`_meta.copilotEnablement` 为 `enabled`），`models.currentModelId` 是 Copilot 自己的当前默认；`configOptions` 中 `id` 为 `reasoning_effort` 的选项是可选的推理强度。
4. `session/close`，关闭输入，结束进程树。

列表处理：`modelId` 去重；去掉 `auto`（每次由 Copilot 挑模型，DN004 §3.4）；推理强度对所有模型相同（ACP 只给会话级的选项）。

实测（T330，Copilot CLI 1.0.114，Windows，已登录）：

| 项 | 结果 |
| --- | --- |
| 列表 | `session/new` 返回 17 项（含两个 `auto`），`currentModelId` 为 `gpt-6.2-sol`；`reasoning_effort` 的选项为 `low` 到 `max` 五档。不调用模型 |
| 与实际调用一致 | 用列表中的 `gpt-6.2-sol` 运行 `copilot -p`，`assistant.message` 的 `model` 就是 `gpt-6.2-sol`，没有 `session.model_change`（`managed_settings`）：T325 时把模型固定为 `gpt-6.1-sol` 的管理策略在本机已不存在，这正是 F-T325-2 的情形（写死的模型会过时） |
| 不存在的模型 | `copilot -p --model <不存在>` 以 `session.error`“Model … is not available” 失败，不列出可用模型，所以不能用它取列表 |
| `copilot help config` | 列出 CLI 内置的模型清单，与本机账号可用的模型不同（含本机不可用的 Claude 模型），不采用 |
| 留下的记录 | `session/new` 在 `~/.copilot/session-state/` 留下一个空会话目录，`session/close` 后仍在；与评审会话记录相同，tsw 不清理（§6） |

未能验证：管理策略固定模型时 `availableModels` 是否只剩策略允许的模型（本机已无此策略）。所以列表可能比实际可用的多；选了被策略替换的模型时，评审按 §4 判失败，错误写明实际模型，用户再改。

取不到列表的情形：找不到 `copilot`、未登录（`session/new` 报认证错误）、60 秒内没有结果。按 DN004 §3.4 的退路处理。
