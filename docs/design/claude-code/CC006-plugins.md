# CC006 Claude Code 插件

- 状态：设计草案（T242 r2 自 DN006 拆出；T243 增加 §6 例行会话与 §7 `teams-chat` 渠道；T250 例行会话启动后分离，R1 结论更正；T251 增加 §8 会话能用到的 skill、MCP 与插件、仓库任务的 worktree）
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
| 例行会话空跑不唤醒主会话 | 主会话启动后 `detach_session`（§6.1） | P2（`detach` 能力） |
| 控制根 = 私有 Git 仓库 | ledger、runs 随仓库提交 | P4 + 数据目录 |

## 2. 宿主集成 `claude-code`

`claude-code` 的插件按用户级启用，任何目录的会话（主会话、`work/<任务>/<attempt>` 或其中仓库 worktree 中的 worker、无关会话）都会加载它，所以插件只携带每个会话都需要的 MCP 注册与 hooks。主会话的流程说明不放进插件：`render_instructions` 把 `CLAUDE.md` 和 `orchestrate` skill 写进数据目录，后者是 Claude Code 的项目 skill（`<cwd>/.claude/skills/`）。Claude Code 也从上级目录加载 `CLAUDE.md` 与项目 skill（skill 的查找在 git 仓库根停下，§8），执行目录在数据目录之下的例行会话与 worker 同样会看到二者（仓库 worktree 中的 worker 只看到 `CLAUDE.md`），所以二者都写明：`whoami` 的角色不是 orchestrator 时忽略。skill 的来源在产品代码中（`tsw/host/orchestrate_skill.md`），产品更新后重新运行 `tsw host render`。

插件由 Claude Code 复制到插件缓存运行，缓存里没有 `tsw` 运行时。启动器按顺序查找：`TSW_REPO` 环境变量、插件所在的产品仓库（源码树或原地使用的本地市场）、`tsw init` 写入数据目录标记文件的 `product_repo`；都找不到时 MCP server 报错退出，hooks 静默放行。

缓存副本只在插件的版本变化时才被替换（Claude Code 按 `plugin.json` 的 `version`、市场条目的 `version`、来源的提交 SHA 的顺序计算版本）。开发期 `plugin.json` 不写版本，版本取提交，产品仓库更新后 `claude plugin update task-steward@task-steward` 就会换上新副本；固定的版本字符串会让副本一直停在第一次安装时（T247 实测：app 运行的副本停在 2026-11-06 的 hooks 与启动器）。

- `tsw host render`（DN006 §6.2 的 `update_plugin`）在写完数据目录的文件后运行 `claude plugin update task-steward@task-steward`，幂等：副本已是最新时不变。它按 Claude Code 的安装记录（`installed_plugins.json`）比较更新前后每个 task-steward 安装与 `plugin/` 的文件，输出带 `plugin`：更新前（`before`）与更新后（`installed`）每个安装是否一致、不一致的文件、更新命令的输出，以及之后要做的事（重启会话；更新后仍不一致时先卸载再安装）。没有安装记录时不运行（安装由用户按 install-hint 做），找不到 `claude` 命令时只报告。
- 主会话的 SessionStart 比较 Claude Code 给 hooks 的 `CLAUDE_PLUGIN_ROOT` 与产品仓库的 `plugin/`，内容不一致时在注入的上下文中写明不一致的文件，并让主会话告诉用户运行 `tsw host render`；原地加载（二者是同一目录）或一致时不提示。只读。

已在运行的会话重启后才用上新副本与新的 tsw 运行时（MCP server 在会话开始时载入）。

规则执行：hooks 调用 `tsw guard`，按 [CC008](CC008-guardrails.md) 的 block list 拦截已知的错误动作；tsw 已把关的动作在主会话由数据目录中生成的允许规则放行，在 worker 由用户设置中的 auto 模式规则放行（执行目录不带权限规则，CC008 §6）。

## 3. 会话适配器

| 适配器 | 用途 | 做法 |
| --- | --- | --- |
| `claude-app` | 主会话与 worker（interactive、delegated） | 创建、投递、停止、观察、对账分别用 app 的 `start_session`、`SendMessage`、`stop_session`、`get_session`、`list_sessions {linked:true}`；创建参数与平台约束见 [CC001 §3–§4](CC001-sessions-and-hooks.md)；中性权限映射 ask→default、edits→acceptEdits、auto→auto、full→bypassPermissions |
| `claude-headless` | 无 Codex 时的评审器（oneshot、direct） | `claude -p` 严格只读，调用约定见 [CC004](CC004-reviewer.md) |

`claude-app` 的 interactive 动作：

| 动作 | `claude-app` |
| --- | --- |
| create | `start_session`（fresh、cwd 为 `attempt_start` 返回的执行目录或其中的仓库 worktree（§8）、use_worktree=false；预启动时 prompt 为待命说明） |
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
      "cwd": "<数据目录>/work/T241/a1/example-app",
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

1. `routine_start` 返回会话名 `routine-g<K>`、执行目录（数据目录下的 `routine/`，磁盘上的实际写法）与 `start_session` 参数：`initiation: user_asked`、`context: fresh`、`use_worktree: false`、`roles.routine` 的 `model` / `effort`，以及按 §3 映射为宿主名称的权限模式（事件中的 `host_permission_mode`）；`prompt` 只写短指令（“你是 TaskSteward 的例行会话。调用 `whoami`，再调用 `routine_board`，逐行执行。”），`background` 写明例行会话的职责（≥80 字）。
2. 主会话调用 `start_session`，再用返回的会话 ID 调用 `routine_bind`。结果未知时按会话名用 `list_sessions {linked:true}` 对账。
3. `claude-app` 声明 `detach`，`routine_start` 返回 `detach: true`：主会话随即调用 `mcp__ccd_session_mgmt__detach_session(session_id)`，成功后 `routine_detached`（`generation`），再用 `SendMessage` 告诉例行会话“已绑定，调用 routine_board 开始”。原因：app 在主会话启动的会话（linked side session）每结束一轮、主会话空闲时，都用 side-sessions-event 继续主会话一轮（`start_session` 的 `wake_when_done` 说明：“whether or not this is set”），不区分这一轮是不是定时任务触发、有没有转发内容；`detach_session` 的说明：“it stops nesting under the session that started it, is no longer archived with it, and that session is no longer told when its turns end”。分离后：
   - 例行会话转发用的 `send_message` 按会话 ID 发给主会话，与是否 linked 无关；主会话也按会话 ID 给它发消息（`stale` 时）。
   - 它不再出现在主会话的 `list_sessions {linked:true}` 与 linked 列表中，按会话名对账只在分离之前进行。
   - 主会话被归档时不再连带归档它；它继续把结果转给下一个持有调度租约的主会话。关闭例行会话（`routine.enabled=false`）时主会话停止、归档它，`stop_session` 与 `archive_session` 对非本会话启动的会话可能要用户确认（auto 模式由分类器判断）。
   - `detach_session` 不能撤销，没有确认卡片，不受派发窗口限制（不创建会话）。分离失败或没有记录时 `attention` 的 `routine` 为 `attached`，主会话的 Stop hook 在任何一轮提醒补做；本版本之前启动的例行会话也这样迁移。
4. 新目录第一次使用时 app 要求用户确认信任，确认只能在用户看着主会话时弹出（CC001 §3），所以启动只在用户 turn 中进行。

数据目录的 `CLAUDE.md` 与 `orchestrate` skill 也会被 `routine/` 中的会话读到；二者已写明角色不是 orchestrator 时忽略（§2）。例行会话的指令由 SessionStart hook 注入（CC001 §6）。

### 6.2 每次运行

定时提示（由 `routine_board` 的 `arm` 动作给出）：`<scheduled-task>调用 routine_board，逐行执行例行项，原样转发结果；没有例行项就结束这一轮。</scheduled-task>`。提示自身就足够完成一次运行，所以清空上下文后定时任务继续有效时，下一次运行不需要任何记忆。

| 例行表给出的内容 | 例行会话调用的工具 |
| --- | --- |
| 例行项没有 `fetch` | `routine_run`，参数为该行的 `run`（没有时为 `job_id`） |
| 例行项的 `fetch` 为 `teams.read_chat` | `mcp__teams__ListChatMessages`（`chatId`，`top`），把返回值原样（只这一页）与该行的 `run`（`job_id` 与 `chat_id`）一起交给 `routine_run`。返回值是 JSON 后接一行 `CorrelationId: …`，脚本只读开头的 JSON |
| `routine_run` 返回 `next` | 按 `next.tool` 与 `next.arguments`（`chatId`、`nextLink`）取下一页，再用 `next.run` 加上这一页调用 `routine_run`；直到不再返回 `next`。不合并页，不自行截断。`fetch.pages` 已弃用，只给本版本之前启动的例行会话用 |
| `routine_run` 返回非空的 `forward` | `mcp__ccd_session_mgmt__send_message`，`session_id` 为 `forward` 中的主会话 ID，正文原样 |
| 维护动作 `arm` | 有旧任务引用时先 `CronDelete`；再 `CronCreate`（`recurring: true`，`durable: false`，`cron` 与 `prompt` 原样）；然后 `routine_armed` 登记返回的任务引用 |
| 维护动作 `clear_context` | 本轮最后调用 `mcp__ccd_session_mgmt__clear_session(session_id="self")`：app 在这一轮结束、会话空闲后清空 |

间隔 N 分钟的 cron 表达式：N 整除 60 时为 `*/N * * * *`，N 为 60 的倍数时为 `0 */(N/60) * * *`（DN003 §3.1b）。`CronCreate` 的循环任务 7 天后到期，所以 `routine.renew_after_days` 默认 6。

转发的消息以 `<cross-session-message` 到达主会话，不是用户本人的 turn（CC008 的 CC-S6）；到达 worker 时也不会被记成用户答复（CC-S2）。

### 6.3 宿主侧护栏与允许规则

例行会话角色见 CC001 §5；hooks 对它执行 CC008 的 CC-B8、CC-B10（已撤销、待绑定）与 CC-B11（不编辑、不运行命令、不建会话）。与 worker 的执行目录一样，`routine/` 不带权限规则（带规则的目录会再次要求信任，决定 0003）：例行会话的定时任务、取数、转发与清空上下文由用户设置中的 auto 模式规则“TaskSteward Routine”放行（`tsw host install-hint` 给出，CC008 §6），创建它的 `start_session` 与随后的 `detach_session` 由“TaskSteward Worker Dispatch”覆盖。

### 6.4 主会话的“处理转交问题” skill

`render_instructions` 在数据目录生成第二个项目 skill `.claude/skills/handle-input/SKILL.md`（来源 `tsw/host/handle_input_skill.md`）。例行会话转来的消息要求主会话按它处理：

1. 读问题与任务书，按 DN006 §8.2 的授权边界与因素判断；`reserved` 的问题只能转达给用户；
2. 能代答时 `handoff_resolve`，再用 `send_message` 把决定告诉 worker，让它继续；
3. 需要用户时 `input_relay`：用户正在主会话中时转达到 `main-session`；否则按配置的渠道（如 `teams-chat`，§7）发出；
4. Teams 中转来的消息：判断它是否回答了问题，是答复时 `input_resolve`（来源为渠道 `teams-chat` 与消息 ID），再告诉 worker；不是答复时不登记。

### 6.5 待验证（T243、T250 live）

| 编号 | 假设 | 不成立时 |
| --- | --- | --- |
| R1 | 由定时任务触发的一轮结束时，app 不用 `side-sessions-event` 唤醒主会话（只在子会话的第一轮或主会话发起的一轮结束时唤醒） | **不成立**（T250）：T243 live 自己的 hook 记录显示例行会话每轮结束（包括空跑）都唤醒主会话，当时的检查 O 只取操作者观察而漏判。已改为启动后 `detach_session`（§6.1），live 检查改为从 hook 记录判定（R5） |
| R2 | `clear_session("self")` 之后，会话内的定时任务仍然有效 | 清空后定时任务失效，按恢复规则（DN006 §8.9.6）由主会话发消息让例行会话重新设定；或把 `context_reset_hours` 设得很大，只依赖宿主的自动压缩 |
| R3 | 例行会话能按会话 ID 给主会话发消息 | 改用 `SendMessage`（`to` 为 `local_…` 会话 ID） |
| R4 | `SendMessageToSelf` 返回消息 ID、chat ID 与发送者 ID；`ListChatMessages` 能读回用户在自聊中的消息与发送者 | 发送者取自读回的卡片消息 |
| R5 | 分离后例行会话空跑结束时主会话不再收到 side-sessions-event；例行会话的转发仍以 `<cross-session-message` 送达主会话，没有确认卡片（T250 live routine-e2e 的检查 W，从主会话的 hook 记录判定） | 转发改用 `SendMessage`（`to` 为主会话 ID）；仍需确认时把放行写进 auto 规则 |

## 7. `teams-chat` 渠道

DN006 §8.6–§8.7 在 Claude Code 上的做法。Teams 工具（`mcp__teams__*`）由用户安装，主会话与例行会话都能调用；tsw 进程不能调用，所以发送与取数都由会话执行。

| 步骤 | 谁 | 工具 |
| --- | --- | --- |
| 发送问题卡片 | 主会话 | `input_relay`（`channel: teams-chat`）返回卡片正文与目标 chat；主会话用 `get_session` 取 worker 会话的 `link` 填入卡片；目标为 `self` 时调用 `mcp__teams__SendMessageToSelf`，否则 `mcp__teams__SendMessageToChat`；再用 `channel_sent` 登记返回的 chat ID、消息 ID 与发送者 |
| 取出答复 | 例行会话 | Teams 例行项：`mcp__teams__ListChatMessages` 取数，`routine_run` 运行脚本，结果转给主会话 |
| 登记答复、告诉 worker | 主会话 | `input_resolve`、`send_message`（§6.4 第 4 步） |

消息正文是 HTML，脚本去掉标签与首尾空白后再看是否以任务 ID 开头。

## 8. 会话能用到的 skill、MCP 与插件；仓库任务的 worktree

T251 依据实测与官方文档（[skills](https://code.claude.com/docs/en/skills)、[memory](https://code.claude.com/docs/en/memory)、[permissions](https://code.claude.com/docs/en/permissions)、[plugins/loading](https://code.claude.com/docs/en/plugins/loading)）确定，用户 2026-11-09 确认。分发（把 skill、MCP、插件放到哪里）由各仓库的 owner 与用户（对数据目录）自己维护，tsw 不做。

### 8.1 宿主的发现规则

| 来源 | 规则 |
| --- | --- |
| 项目 skill（`.claude/skills/`） | 从会话目录逐级向上查找，到 git 仓库根（链接 worktree 为 worktree 根）为止；不在 git 仓库中时一直向上。会话目录之下的子目录中的 skill 在第一次读写其中文件时加载 |
| `CLAUDE.md`、`.claude/rules/` | 从会话目录逐级向上加载，不受 git 仓库根限制；子目录中的在读写其中文件时加载 |
| `AGENTS.md` | 由内置插件 `cc-plugin-agents-md` 读取；默认只在会话目录及上级都没有 `CLAUDE.md` 时读。数据目录有 `CLAUDE.md`，所以它之下的会话都不会自动读仓库的 `AGENTS.md`（仓库 `CLAUDE.md` 用 `@AGENTS.md` 引用时除外） |
| `.mcp.json` | 从会话目录与上级目录发现（实测，不受 git 仓库根限制）；使用前按路径记录批准 |
| 项目设置（`.claude/settings.json`：`enabledPlugins`、hooks、权限规则等） | 只读会话目录本身，没有上级回退 |
| 用户范围的 skill、MCP、插件 | 所有会话 |

信任：不在 git 仓库中的目录，信任覆盖其子目录，但不覆盖嵌在其中的 git 仓库；worktree 的信任按其主检出计算。用户实测：在数据目录下 task-steward 的 worktree 中新开会话，没有弹出信任确认（主检出已信任）。主检出从未信任过的仓库，第一次可能弹出，由用户确认一次（或先在 app 中打开该仓库）。

### 8.2 各会话能用到什么

| 来源 | 主会话（数据目录） | 例行会话、没有 worktree 的 worker（执行目录） | 仓库任务的 worker（会话目录 = worktree） |
| --- | --- | --- | --- |
| 用户范围的 skill、MCP、插件（含 task-steward） | 可用 | 可用 | 可用 |
| 数据目录的 skill | 可用 | 可用 | 宿主不加载；任务书列出名称、说明与路径，worker 直接读 SKILL.md |
| 数据目录的 `CLAUDE.md`、`.mcp.json` | 可用 | 可用 | 可用 |
| 数据目录的项目插件 | 可用 | 不可用 | 不可用 |
| 仓库的 skill、`.mcp.json`、项目插件、hooks 与设置、`CLAUDE.md` | — | — | 可用（按仓库自己的配置） |
| 仓库的 `AGENTS.md` 等指令文件 | — | — | 宿主不自动读；任务书要求读并遵守（§8.3） |

要给所有会话用的插件装在用户范围；数据目录的项目插件只对主会话生效。

### 8.3 tsw 的做法

- **worktree**：任务的仓库在配置中有本机 git 检出（`repositories.<id>.path`）时，`attempt_start`（预启动或派发）在执行目录下创建 `<执行目录>/<仓库 id>` 的 git worktree：分支按 `worktrees.branch_pattern`，基线为 `base_ref`，未配置时取远端默认分支（`<remote>/HEAD`、`<remote>/main` 等第一个存在的）。创建前 `git fetch` 一次（失败不阻止创建，事件记录 `fetched`）。
  - worktree 在记录事件之前创建：路径上已有别的内容（`worktree_conflict`）或 git 失败（`worktree_unavailable`）时拒绝，不记录事件。同一 attempt 的后续代次沿用已有的 worktree；已有同名分支时用它。
  - `attempt.start_requested` 的 `worktree` 字段记录路径、分支、基线、是否新建与是否 fetch 成功。执行目录（`workdir`）仍是 `<执行目录>`，报告草稿与可写范围不变。
  - MCP 返回的 `cwd` 是 worktree，主会话用它创建 worker 会话。
  - 没有可用检出（未配置 `path`、目录不存在、不是 git 仓库）时不建 worktree，会话在执行目录中启动，任务书写明原因。
- **任务书的“仓库与 worktree”一节**（仓库已配置时生成）：
  - 会话目录与分支（或没有 worktree 的原因），不要另建 worktree 或 clone；报告写在执行目录。
  - 仓库指令文件：`repositories.<id>.instructions`（相对仓库根的路径列表），未配置时默认 `AGENTS.md`、`CLAUDE.md`、`.github/copilot-instructions.md`；worker 开始前完整阅读并遵守，改动子目录前也读该子目录中的同名文件，报告写明读了哪些。各仓库的列表由主会话经 `config_change` 维护。
  - 有 worktree 时列出数据目录的 skill（`orchestrate`、`handle-input` 只用于主会话，不列）。
- **评审**：`review_run` 在 worktree 中评审，按其中的提交确定范围。
- 数据目录的 `CLAUDE.md` 不变，不为 `AGENTS.md` 原生加载改写它（用户决定）。
- `worktrees.root` 在 `claude-code` 宿主中不使用（字段保留）；`worktrees.work_dir` 仍是执行目录的默认位置。

### 8.4 Codex（记录，未实现）

TaskSteward 目前只把 Codex 用作评审器。Codex 从项目根（默认以 `.git` 为标记）到会话目录读取 `AGENTS.md`，`.agents/skills` 的查找也在这个范围内；MCP 只来自 `config.toml`（用户级或受信任项目的 `.codex/config.toml`），不读 `.mcp.json`。以后实现 Codex worker 时，会话目录同样应是仓库 worktree；数据目录的 skill 与 `AGENTS.md` 在 worktree 中同样读不到，需要由任务书列出。
