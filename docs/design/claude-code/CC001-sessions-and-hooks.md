# CC001 Claude Code 上的会话与 hooks

- 状态：设计草案（T242 r2 自 DN001 拆出，待用户确认）
- 日期：2026-11-08
- 实现：[DN001 总体架构](../DN001-architecture.md)
- 相关：[CC006 Claude Code 插件](CC006-plugins.md) · [CC008 Claude Code 上的护栏](CC008-guardrails.md)

## 1. 范围

本文是 DN001 在默认宿主 Claude Code（桌面 app 与命令行）上的实现：主会话如何加载指令、worker 会话如何创建与唤醒、角色解析依赖的宿主机制、hooks，以及 2026-11-05 的平台 PoC。与宿主无关的会话模型、工具面与角色规则见 DN001。

## 2. 主会话

主会话是在数据目录打开的普通 Claude app 会话，加载数据目录的 `CLAUDE.md` 和项目 skill `orchestrate`（`.claude/skills/orchestrate/SKILL.md`）。两者都由宿主集成生成（`tsw host render`）。插件按用户级启用，任何目录的会话都会加载它，所以插件不携带主会话 skill。用户要求执行、继续、恢复或调整待办时，主会话按 skill 流程工作。普通仓库维护、解释问题不会开始执行真实待办。

## 3. worker 会话的创建

创建时调用 app 的 `start_session`，参数全部来自统一配置的 `roles.worker`，不继承主会话：

| start_session 参数 | 取值 | 说明 |
| --- | --- | --- |
| `initiation` | `user_asked` | 用户已要求执行该任务 |
| `context` | `fresh` | 不 fork 主会话的长上下文；任务所需背景全部在 brief 里 |
| `cwd` | 执行目录（worktree 或研究目录） | worktree 由 tsw 创建，不让 app 另建 |
| `use_worktree` | `false` | 在已有目录中工作；app 归档会话时不会删除该目录（§7 假设 A7） |
| `name` | `txxx-aN-gK` | K 为创建代次（DN002 §5.4），每次请求创建加 1；用于对账，在 linked 会话中唯一 |
| `model` / `effort` | `roles.worker.model` / `roles.worker.effort` | 如实记录请求值；实际值以 `get_session` 回执为准 |
| `permission_mode` | `roles.worker.permission_mode` | 不能高于主会话自身模式（平台限制） |
| `prompt` | 待命说明（预启动）或 worker 启动指令（直接启动） | 待命说明只介绍角色与回传协议，要求等待任务书；任务书 `runs/Txxx/aN/brief.md` 在派发时用 `SendMessage` 投递 |
| `background` | 任务目标摘要（≥80 字） | 平台对 fresh 会话的要求 |

先写 `attempt.start_requested` 事件，再调用工具。结果未知时按会话名对账（`list_sessions {linked:true}`），不再次创建（DN002 §6 `starting` 阶段）。

新目录第一次被用作会话目录时，app 要求用户确认信任该目录；信任确认只能在用户正看着主会话时弹出，否则创建直接失败。预启动发生在用户本人的 turn 中，正好满足这个条件。

## 4. 平台约束

以下约束经 2026-11-05 的 PoC 实测（§7）：

1. **创建会话的时机**：只有在用户本人消息所在的 turn 中，主会话才能用 `start_session` 在任意受信任目录创建会话。由其他会话的消息触发的 turn 中完全不能创建；紧跟用户 turn 的 `side-sessions-event` 唤醒 turn 中只能在本项目创建、不能指定目录。提案卡片同样不能指定目录。因此 worker 会话（必须在任务 worktree 中）只能在用户 turn 中创建。
2. **投递不受限**：任何 turn（包括自动唤醒与消息触发的 turn）都可以给已存在的子会话发消息，消息到达后子会话立即开始执行。
3. **唤醒**：子会话用 `mcp__ccd_session_mgmt__send_message(session_id="parent")` 给父会话发消息（`SendMessage(to="parent")` 不可用），父会话忙时排队，空闲后作为新 turn 送达。只有当主会话启动的子会话**全部**不在工作、且主会话空闲时，app 才用 `side-sessions-event` 唤醒主会话，并只引用子会话最新回复的前 400 字。
4. 连续多次被自动唤醒而用户没有发消息后，后续通知会等到用户的下一个 turn（上限尚未实测）。
5. 新目录需要用户确认信任（§3）；子会话的权限模式不能高于主会话。

## 5. 角色解析依赖的宿主机制

tsw MCP server 由用户级插件安装，每个会话启动一个进程。PoC 实测：MCP server 进程与 hooks 的环境变量都带有调用会话在 app 中的会话 ID（`CLAUDE_CODE_HOST_SESSION_ID`），进程 cwd 与 `CLAUDE_PROJECT_DIR` 都是会话目录。因此角色按会话 ID 精确解析：

- 新会话的 MCP server 在主会话绑定之前就已列出工具。实测 Claude Code 不会因 `notifications/tools/list_changed` 重新获取列表，所以待绑定会话的第一次列表就是 worker 的（DN001 §7.1）。
- worker 绑定后 MCP server 发出 `notifications/tools/list_changed`（客户端空闲时也由后台每 2 秒检查一次），客户端据此刷新工具列表。
- hooks 拿到同一个会话 ID（环境变量）以及 payload 中的内部会话 ID、cwd、权限模式。

## 6. hooks

| hook | 角色 | 行为 |
| --- | --- | --- |
| SessionStart | 全部 | 按会话 ID 与目录识别角色；主会话尝试领取租约，并注入未读 inbox 数与 attention 摘要；worker 注入任务 ID、brief 路径和评审规则；待绑定会话提示等待绑定 |
| PreToolUse | 已登记会话 | block list 护栏，见 [CC008 §3](CC008-guardrails.md) |
| UserPromptSubmit | worker | 检测 `[report]`，注入回传协议提示；worker 有未获用户答复的会话内问题时，把用户消息原文记为 `input.session_message` 并告诉 worker 消息编号（注入的其他会话消息不记，DN006 §8.1a） |
| Stop | orchestrator | 运行只读 `attention`；有未处理 reportable 事件，或有已超时未转交的会话内问题（提示运行 `sync`）时阻止一次结束（`stop_hook_active` 防循环） |
| Stop | worker | 记录 `worker.turn_ended` 观察事件（不改状态）；若处于 `reviewing`，提示完成这一轮再结束 |

hooks 是防错护栏，不是安全边界（DN008）；skill 和 CLAUDE.md 只写流程与判断准则。

## 7. 平台假设的 PoC 结果

2026-11-05 在 Claude Code 桌面 app 中实测（父会话启动探测子会话）：

| 编号 | 假设 | 结果 | 设计中的处理 |
| --- | --- | --- | --- |
| A1 | `start_session` 可用 `cwd` 打开 tsw 创建的目录（`use_worktree:false`），model / effort / permission_mode 生效 | 成立。新目录须先由用户确认信任，确认只能在用户看着主会话时弹出 | 预启动在用户 turn 中进行（§3、§4） |
| A2 | 自动 turn 中不能创建会话，提案卡片可由用户点击启动 | 成立且更严格：消息触发的 turn 完全不能创建；唤醒 turn 只能在本项目创建；提案卡片不能指定目录。任何 turn 都能给已有会话发消息 | 预启动 + 后投递；取消 worker 的提案路径（§4、DN002 §5.6） |
| A3 | MCP server 在子会话中以子会话 cwd 启动 | 成立，且环境里有 app 会话 ID | 角色按会话 ID 解析（§5） |
| A4 | 子会话能给父会话发消息，主会话忙时排队 | 成立，须用 `mcp__ccd_session_mgmt__send_message(session_id="parent")` | worker 指令固定该工具（§4） |
| A5 | hooks 在子会话中生效，可拿到会话 ID 与 cwd | 成立 | 防错护栏与角色识别用 hooks（§6） |
| A6 | Codex App Server 一次性线程可用 `gpt-6-astra` / `medium` / read-only | 成立（设计评审中多次使用） | 默认评审器（[CX004](../codex/CX004-reviewer.md)） |
| A7 | `archive_session` 不删除 `use_worktree:false` 会话的目录 | 成立 | 归档会话不影响执行目录与证据 |
| A8 | `get_session` 返回子会话实际 model / effort / 权限模式 | 成立；模型 ID 可能带上下文后缀（如 `[1m]`） | 比较请求值与回执时规范化（DN003 §1） |
