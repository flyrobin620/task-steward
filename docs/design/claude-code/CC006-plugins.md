# CC006 Claude Code 插件

- 状态：设计草案（T242 r2 自 DN006 拆出；T243 增加 §6 例行会话与 §7 `teams-chat` 渠道）
- 日期：2026-11-08
- 实现：[DN006 插件层](../DN006-plugin-layer.md)
- 相关：[CC001 Claude Code 上的会话与 hooks](CC001-sessions-and-hooks.md) · [CC004 Claude Code 评审器](CC004-reviewer.md) · [CC008 Claude Code 上的护栏](CC008-guardrails.md)

## 1. 范围

本文是 DN006 插件层在 Claude Code 上的实现：宿主集成 `claude-code`、会话适配器 `claude-app` 与 `claude-headless`、`in-session` 渠道的记录方式、例行会话（DN006 §8.9）与 `teams-chat` 渠道。插件接口与能力声明见 DN006。


与 Claude Code 耦合的设计元素及其归属的插件类型：

| 设计元素 | Claude 实现 | 归属 |
| --- | --- | --- |
| 主会话与 worker 的指令、skill、工具注册 | `CLAUDE.md`、`.claude-plugin`、skills、`.mcp.json` | P1 |
| 写入范围、危险命令拦截、结束前检查 | Claude Code hooks | P1 |
| 创建、续用、停止、观察 worker 会话 | `start_session`、`SendMessage`、`list_sessions`、`get_session`、`stop_session` | P2 |
| 唤醒主会话 | worker→父会话 `send_message(session_id="parent")`、side-sessions-event | P2（唤醒能力） |
| 派发窗口与预启动（G-WINDOW、attempt `standby`） | 只有用户本人的 turn 能在任务目录创建会话；任何 turn 都能给已有会话发消息 | P2（能力声明） |
| 权限模式名称 | `default` / `acceptEdits` / `auto` / `bypassPermissions` | P2（映射表） |
| 评审器 | Codex App Server 一次性线程 | P2 的 oneshot 能力 + `roles.reviewer.session` |
| 待答问题的投递与回收 | 用户在子会话直接答复；转交后经 Teams 自聊（`teams-chat`，§7） | P3 |
| 例行会话的定时运行 | 会话内定时任务 `CronCreate` / `CronDelete`，`clear_session` 清空上下文（§6） | P2（`timer` 能力） |
| 控制根 = 私有 Git 仓库 | ledger、runs 随仓库提交 | P4 + 数据目录 |

## 2. 宿主集成 `claude-code`

`claude-code` 的插件按用户级启用，任何目录的会话（主会话、`~/tsw-worktrees` 或 `work/<任务>/<attempt>` 中的 worker、无关会话）都会加载它，所以插件只携带每个会话都需要的 MCP 注册与 hooks。主会话的流程说明不放进插件：`render_instructions` 把 `CLAUDE.md` 和 `orchestrate` skill 写进数据目录，后者是 Claude Code 的项目 skill（`<cwd>/.claude/skills/`），只有以数据目录为工作目录的会话会发现它。Claude Code 也从上级目录加载 `CLAUDE.md` 与项目 skill，执行目录在数据目录之下（`work/<任务>/<attempt>`）的 worker 同样会看到二者，所以二者都写明：`whoami` 的角色不是 orchestrator 时忽略。skill 的来源在产品代码中（`tsw/host/orchestrate_skill.md`），产品更新后重新运行 `tsw host render`。

插件由 Claude Code 复制到插件缓存运行，缓存里没有 `tsw` 运行时。启动器按顺序查找：`TSW_REPO` 环境变量、插件所在的产品仓库（源码树或原地使用的本地市场）、`tsw init` 写入数据目录标记文件的 `product_repo`；都找不到时 MCP server 报错退出，hooks 静默放行。

规则执行：hooks 调用 `tsw guard`，按 [CC008](CC008-guardrails.md) 的 block list 拦截已知的错误动作；tsw 已把关的动作在主会话由数据目录中生成的允许规则放行，在 worker 由用户设置中的 auto 模式规则放行（执行目录不带权限规则，CC008 §6）。

## 3. 会话适配器

| 适配器 | 用途 | 做法 |
| --- | --- | --- |
| `claude-app` | 主会话与 worker（interactive、delegated） | 创建、投递、停止、观察、对账分别用 app 的 `start_session`、`SendMessage`、`stop_session`、`get_session`、`list_sessions {linked:true}`；创建参数与平台约束见 [CC001 §3–§4](CC001-sessions-and-hooks.md)；中性权限映射 ask→default、edits→acceptEdits、auto→auto、full→bypassPermissions |
| `claude-headless` | 无 Codex 时的评审器（oneshot、direct） | `claude -p` 严格只读，调用约定见 [CC004](CC004-reviewer.md) |

`claude-app` 的 interactive 动作：

| 动作 | `claude-app` |
| --- | --- |
| create | `start_session`（fresh、cwd、use_worktree=false；预启动时 prompt 为待命说明） |
| deliver_brief | `SendMessage`（任何 turn 都可以；直接启动时由 `start_session` 的短指令 prompt 指向任务书路径，两阶段合一；正文不放进 prompt，见 [CC008 §6](CC008-guardrails.md)） |
| send | `SendMessage` |
| stop | `stop_session` |
| observe | `get_session` |
| find | `list_sessions {linked:true}` 按名称 |
| 唤醒主会话 | push：worker `mcp__ccd_session_mgmt__send_message(session_id="parent")`；兜底 side-sessions-event |

## 4. `in-session` 渠道

worker 有待答问题时，`UserPromptSubmit` hook 把用户消息记为 `input.session_message`（DN006 §8.1a）。哪些消息记录、哪些作为注入消息跳过，见 [CC008 §4](CC008-guardrails.md) 的 CC-S2。

## 5. 委托操作示例

派发 T241 的 worker（Claude app 宿主），DN006 §4 的委托操作：

```json
{
  "operation_id": "op-T241-a1-g1-start",
  "kind": "session.start",
  "task_id": "T241",
  "attempt": "a1",
  "generation": 1,
  "action": {
    "neutral": "start_worker_session",
    "tool": "mcp__ccd_session__start_session",
    "params": {
      "initiation": "user_asked",
      "context": "fresh",
      "cwd": "~/tsw-worktrees/example-app/T241-a1",
      "use_worktree": false,
      "name": "t241-a1-g1",
      "model": "claude-opus-5-5",
      "effort": "high",
      "permission_mode": "auto",
      "prompt": "按 runs/T241/a1/brief.md 执行。工具、评审与回传协议见 brief 第 1 节。",
      "background": "为 example-app 的报表页增加 CSV 导出：在任务 worktree 中实现服务端流式导出接口与界面上的导出按钮，支持十万行分页与导出链接过期，补齐单元测试与端到端测试，完成后用配置的评审器自评审，再按 brief 提交报告。"
    }
  },
  "expects_receipt": {"fields": ["session_id"], "on_unknown": "reconcile_by_name"},
  "expires_at": "2026-11-05T12:00:00Z"
}
```

回执：

```json
{"operation_id": "op-T241-a1-g1-start", "outcome": "ok", "result": {"session_id": "local_6c1f…"}, "evidence": "tool result"}
```

## 6. 例行会话

DN006 §8.9 在 Claude app 上的做法。Claude app 托管的会话不能由 tsw 进程唤醒，所以例行表的执行者是一个子会话：`claude-app` 声明 `timer`，会话内定时任务（`CronCreate`）由 app 在会话进程中执行，只在 app 打开时运行。

### 6.1 启动

主会话在用户第一次要求执行待办的那一轮（用户本人的 turn，CC001 §4）启动：

1. `routine_start` 返回会话名 `routine-g<K>`、执行目录（数据目录下的 `routine/`，磁盘上的实际写法）与 `start_session` 参数：`initiation: user_asked`、`context: fresh`、`use_worktree: false`、`roles.routine` 的 `model` / `effort` / `permission_mode`；`prompt` 只写短指令（“你是 TaskSteward 的例行会话。调用 `whoami`，再调用 `routine_board`，逐行执行。”），`background` 写明例行会话的职责（≥80 字）。
2. 主会话调用 `start_session`，再用返回的会话 ID 调用 `routine_bind`。结果未知时按会话名用 `list_sessions {linked:true}` 对账。
3. 新目录第一次使用时 app 要求用户确认信任，确认只能在用户看着主会话时弹出（CC001 §3），所以启动只在用户 turn 中进行。

数据目录的 `CLAUDE.md` 与 `orchestrate` skill 也会被 `routine/` 中的会话读到；二者已写明角色不是 orchestrator 时忽略（§2）。例行会话的指令由 SessionStart hook 注入（CC001 §6）。

### 6.2 每次运行

定时提示（由 `routine_board` 的 `arm` 动作给出）：`<scheduled-task>调用 routine_board，逐行执行例行项，原样转发结果；没有例行项就结束这一轮。</scheduled-task>`。提示自身就足够完成一次运行，所以清空上下文后定时任务继续有效时，下一次运行不需要任何记忆。

| 例行表给出的内容 | 例行会话调用的工具 |
| --- | --- |
| 例行项没有 `fetch` | `routine_run(job_id)` |
| 例行项的 `fetch` 为 `teams.read_chat` | `mcp__teams__ListChatMessages`（`chatId`，`top`），把返回的消息原样交给 `routine_run(job_id, fetched)` |
| `routine_run` 返回非空的 `forward` | `mcp__ccd_session_mgmt__send_message`，`session_id` 为 `forward` 中的主会话 ID，正文原样 |
| 维护动作 `arm` | 有旧任务引用时先 `CronDelete`；再 `CronCreate`（`recurring: true`，`durable: false`，`cron` 与 `prompt` 原样）；然后 `routine_armed` 登记返回的任务引用 |
| 维护动作 `clear_context` | 本轮最后调用 `mcp__ccd_session_mgmt__clear_session(session_id="self")`：app 在这一轮结束、会话空闲后清空 |

间隔 N 分钟的 cron 表达式：N 整除 60 时为 `*/N * * * *`，N 为 60 的倍数时为 `0 */(N/60) * * *`（DN003 §3.1b）。`CronCreate` 的循环任务 7 天后到期，所以 `routine.renew_after_days` 默认 6。

转发的消息以 `<cross-session-message` 到达主会话，不是用户本人的 turn（CC008 的 CC-S6）；到达 worker 时也不会被记成用户答复（CC-S2）。

### 6.3 宿主侧护栏与允许规则

例行会话角色见 CC001 §5；hooks 对它执行 CC008 的 CC-B8、CC-B10（已撤销、待绑定）与 CC-B11（不编辑、不运行命令、不建会话）。`routine_start` 在 `routine/` 下写入 `.claude/settings.local.json`，允许 tsw MCP 工具、`send_message`、`clear_session`、`CronCreate` / `CronDelete` 与 `mcp__teams__ListChatMessages`，避免 auto 分类器把例行动作当作未经授权而拦下（CC008 §6）。

### 6.4 主会话的“处理转交问题” skill

`render_instructions` 在数据目录生成第二个项目 skill `.claude/skills/handle-input/SKILL.md`（来源 `tsw/host/handle_input_skill.md`）。例行会话转来的消息要求主会话按它处理：

1. 读问题与任务书，按 DN006 §8.2 的授权边界与因素判断；`reserved` 的问题只能转达给用户；
2. 能代答时 `handoff_resolve`，再用 `send_message` 把决定告诉 worker，让它继续；
3. 需要用户时 `input_relay`：用户正在主会话中时转达到 `main-session`；否则按配置的渠道（如 `teams-chat`，§7）发出；
4. Teams 中转来的消息：判断它是否回答了问题，是答复时 `input_resolve`（来源为渠道 `teams-chat` 与消息 ID），再告诉 worker；不是答复时不登记。

### 6.5 待验证（T243 live）

| 编号 | 假设 | 不成立时 |
| --- | --- | --- |
| R1 | 由定时任务触发的一轮结束时，app 不用 `side-sessions-event` 唤醒主会话（只在子会话的第一轮或主会话发起的一轮结束时唤醒） | 主会话创建后对例行会话调用 `detach_session`，此后按会话 ID 发消息 |
| R2 | `clear_session("self")` 之后，会话内的定时任务仍然有效 | 清空后定时任务失效，按恢复规则（DN006 §8.9.6）由主会话发消息让例行会话重新设定；或把 `context_reset_hours` 设得很大，只依赖宿主的自动压缩 |
| R3 | 例行会话能按会话 ID 给主会话发消息 | 改用 `SendMessage`（`to` 为 `local_…` 会话 ID） |
| R4 | `SendMessageToSelf` 返回消息 ID、chat ID 与发送者 ID；`ListChatMessages` 能读回用户在自聊中的消息与发送者 | 发送者取自读回的卡片消息 |

## 7. `teams-chat` 渠道

DN006 §8.6–§8.7 在 Claude Code 上的做法。Teams 工具（`mcp__teams__*`）由用户安装，主会话与例行会话都能调用；tsw 进程不能调用，所以发送与取数都由会话执行。

| 步骤 | 谁 | 工具 |
| --- | --- | --- |
| 发送问题卡片 | 主会话 | `input_relay`（`channel: teams-chat`）返回卡片正文与目标 chat；主会话用 `get_session` 取 worker 会话的 `link` 填入卡片；目标为 `self` 时调用 `mcp__teams__SendMessageToSelf`，否则 `mcp__teams__SendMessageToChat`；再用 `channel_sent` 登记返回的 chat ID、消息 ID 与发送者 |
| 取出答复 | 例行会话 | Teams 例行项：`mcp__teams__ListChatMessages` 取数，`routine_run` 运行脚本，结果转给主会话 |
| 登记答复、告诉 worker | 主会话 | `input_resolve`、`send_message`（§6.4 第 4 步） |

消息正文是 HTML，脚本去掉标签与首尾空白后再看是否以任务 ID 开头。
