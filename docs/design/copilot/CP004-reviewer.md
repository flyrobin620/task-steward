# CP004 Copilot CLI 评审器

- 状态：设计草案（T325，待用户确认）
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
- 管理策略固定的模型只能照用：`roles.reviewer.model` 必须写成策略允许的那一个，否则每轮都失败。
- 用户级 MCP server 仍会启动（只是工具被禁用），首次启动可能要十几秒。

## 7. 验证

- conformance / surface：以录制的 JSONL（来自 §2 的实测，去掉正文与本机路径）回放，覆盖完成、续用、模型被替换、`session.error`、非 JSON、超时。
- live（`python tools/live.py reviewer --reviewer copilot-cli`）：真实 Copilot CLI 在固定样例上做一次全量与一次增量评审，核对只读与输出契约。
