# CC001 Claude Code 上的会话与 hooks

- 状态：设计草案（T242 r2 自 DN001 拆出；T243 增加例行会话；T251 仓库任务的会话目录为执行目录下的 worktree；T262 worker 会话中的用户消息都记录；T276 worker 会话的 prompt 以 worker 说明开头，SessionStart 只给会话特有信息）
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
| `cwd` | `attempt_start` 返回的 `cwd`：仓库任务为执行目录下的仓库 worktree `<执行目录>/<仓库 id>`，其他任务为执行目录 | worktree 由 tsw 在 `attempt_start` 中创建（[CC006 §8](CC006-plugins.md)），不让 app 另建；执行目录不存在时 `attempt_start` 先创建（app 不接受不存在的目录，报 “folder does not exist”） |
| `use_worktree` | `false` | 在已有目录中工作；app 归档会话时不会删除该目录（§7 假设 A7） |
| `name` | `txxx-aN-gK` | K 为创建代次（DN002 §5.4），每次请求创建加 1；用于对账，在 linked 会话中唯一 |
| `model` / `effort` | `roles.worker.model` / `roles.worker.effort` | 如实记录请求值；实际值以 `get_session` 回执为准 |
| `permission_mode` | `roles.worker.permission_mode` | 不能高于主会话自身模式（平台限制） |
| `prompt` | `attempt_start` 返回的 `prompt`，原样传入：worker 说明（数据目录 `roles/worker.md`）全文，后接待命说明（预启动）或 worker 启动指令（直接启动） | tsw 生成，主会话不拼接、不改写（DN006 §6.2，T276）；待命说明要求先调用 whoami、可以先读任务书、等待派发；启动指令为“先调用 whoami，再按任务书 runs/Txxx/aN/brief.md 执行”；任务书正文不进 prompt；任务书 `runs/Txxx/aN/brief.md` 在预启动时已写出，待命会话可以先阅读（SessionStart 提示它不要开始工作），派发时按最新 revision 重新生成，再用 `SendMessage` 通知会话开始；需求变更后主会话同样用 `SendMessage` 通知会话重读；worker 自己登记的变更（DN002 §5.1a）不需要通知 |
| `background` | 任务目标摘要（≥80 字） | 平台对 fresh 会话的要求 |

先写 `attempt.start_requested` 事件，再调用工具。结果未知时按会话名对账（`list_sessions {linked:true}`），不再次创建（DN002 §6 `starting` 阶段）。

新目录第一次被用作会话目录时，app 可能要求用户确认信任该目录；信任确认只能在用户正看着主会话时弹出，否则创建直接失败。预启动发生在用户本人的 turn 中，正好满足这个条件。已信任的数据目录之下的执行目录不再单独要求信任，除非该目录带有自己的设置文件：桌面 app 对带 `.claude/settings.local.json` 的文件夹会再次要求信任，即使文件中只有 `env` 与 `hooks`（2026-11-09 live 实测，确认框写明 “Execution allowed by: .claude/settings.local.json”）。因此 tsw 不在执行目录中写任何设置（[CC008](CC008-guardrails.md) §6）；需要让会话使用另一个数据目录时（如 live 运行），用会话所在的数据目录声明 `claim_sessions`（DN006 §9.1），不用设置文件。数据目录之外的执行目录仍须用户确认一次。仓库任务的会话目录是执行目录下的仓库 worktree，它是嵌在数据目录中的 git 仓库，数据目录的信任不覆盖它，按其主检出的信任计算：主检出已信任时不弹确认（2026-11-09 用户实测，task-steward 的 worktree），从未信任过的仓库第一次可能弹出（[CC006 §8.1](CC006-plugins.md)）。

## 4. 平台约束

以下约束经 2026-11-05 的 PoC 实测（§7）：

1. **创建会话的时机**：只有在用户本人消息所在的 turn 中，主会话才能用 `start_session` 在任意受信任目录创建会话。由其他会话的消息触发的 turn 中完全不能创建；紧跟用户 turn 的 `side-sessions-event` 唤醒 turn 中只能在本项目创建、不能指定目录。提案卡片同样不能指定目录。因此 worker 会话（必须在任务 worktree 中）只能在用户 turn 中创建。
2. **投递不受限**：任何 turn（包括自动唤醒与消息触发的 turn）都可以给已存在的子会话发消息，消息到达后子会话立即开始执行。
3. **唤醒**：子会话用 `mcp__ccd_session_mgmt__send_message` 给主会话发消息（`SendMessage(to="parent")` 不可用），对方忙时排队，空闲后作为新 turn 送达。`session_id` 可以是 `"parent"`，也可以是任何会话的 ID，与父子关系无关（分离后的例行会话即如此，CC006 §6.1）。app 不能更换已有会话的父会话，主会话交接（DN001 §6.4）后 `"parent"` 仍指向原主会话，所以 worker 发给 tsw 给出的当前持租主会话（工具结果的 `wake.session_id`、`whoami` 的 `orchestrator_session`），不发给 `"parent"`。只有当主会话启动的子会话**全部**不在工作、且主会话空闲时，app 才用 `side-sessions-event` 唤醒主会话，并只引用子会话最新回复的前 400 字。
4. 连续多次被自动唤醒而用户没有发消息后，后续通知会等到用户的下一个 turn（上限尚未实测）。
5. 新目录需要用户确认信任（§3）；子会话的权限模式不能高于主会话。

## 5. 角色解析依赖的宿主机制

tsw MCP server 由用户级插件安装，每个会话启动一个进程。PoC 实测：MCP server 进程与 hooks 的环境变量都带有调用会话在 app 中的会话 ID（`CLAUDE_CODE_HOST_SESSION_ID`），进程 cwd 与 `CLAUDE_PROJECT_DIR` 都是会话目录。因此角色按会话 ID 精确解析：

- 新会话的 MCP server 在主会话绑定之前就已列出工具。实测 Claude Code 不会因 `notifications/tools/list_changed` 重新获取列表，所以待绑定会话的第一次列表就是 worker 的（DN001 §7.1）。
- worker 绑定后 MCP server 发出 `notifications/tools/list_changed`（客户端空闲时也由后台每 2 秒检查一次），客户端据此刷新工具列表。
- hooks 拿到同一个会话 ID（环境变量）以及 payload 中的内部会话 ID、cwd、权限模式。
- 例行会话（DN006 §8.9）同样按会话 ID 解析：会话 ID 等于当前代次已绑定的例行会话 → 例行会话角色；等于旧代次的例行会话 → 已撤销。尚未绑定、但会话目录是数据目录下的 `routine/` 且有一个正在启动的代次时，与待绑定的 worker 一样，第一次工具列表就按例行会话列出，除 `whoami` 外的调用等绑定后才接受。

## 6. hooks

| hook | 角色 | 行为 |
| --- | --- | --- |
| SessionStart | 全部 | 按会话 ID 与目录识别角色；主会话尝试领取租约（让出过租约的会话不领取，只注入只读说明，DN001 §6.4），并注入未读 inbox 数与 attention 摘要；worker 只注入本会话特有的信息：任务 ID、attempt、brief 路径、可写路径，并指向会话提示语开头的 worker 说明与数据目录副本 `roles/worker.md`（DN006 §6.2），不重复其中的规则（T276 前这里另有一份精简规则，与 `plugin/roles/worker.md` 各自漂移）；会话得到的说明版本与当前副本不同（或会话创建时还没有附上说明）时，提示完整重读副本，每个版本一次（UserPromptSubmit 同样检查）；待命会话还提示可以先读任务书、等派发后再开始，任务书更新时重读；还没有任务书时只提示等待投递；待绑定会话提示等待绑定，绑定后 `phase` 为 `standby` 时只读任务书、等派发，为 `live` 时开始 |
| PreToolUse | 已登记会话 | block list 护栏，见 [CC008 §3](CC008-guardrails.md) |
| UserPromptSubmit | worker | 检测 `[report]`，注入回传协议提示；live worker 的任务不是 `accepted` / `cancelled` / `archived` 时，把用户消息原文记为 `input.session_message` 并告诉 worker 消息编号：回答了待答问题时用 `user_answer` 登记，修改了任务需求时用 `requirement_change` 登记（注入的其他会话消息不记，DN006 §8.1a、DN002 §5.1a；T262 前只在有未获用户答复的会话内问题时记录） |
| SessionStart | routine | 注入例行会话的指令：调用 `routine_board`，逐行执行例行项并原样转发结果，不作判断，不做其他事（CC006 §6） |
| UserPromptSubmit | orchestrator | 记下这一轮是不是用户本人的 turn（[CC008](CC008-guardrails.md) CC-S6）：去掉宿主前置的 `<system-reminder>` 后还有内容，且不以注入消息的标记开头时是用户的 turn |
| Stop | orchestrator | 运行只读 `attention`；有未处理 reportable 事件，有已超时未转交的会话内问题（提示运行 `sync`），例行会话失效（提示给它发消息重新设定），或这一轮是用户的 turn 且 `needs_prestart` 非空或例行会话需要启动（提示预启动、启动例行会话）时阻止一次结束（`stop_hook_active` 防循环），几条原因合在一条提示里 |
| Stop | worker | 记录 `worker.turn_ended` 观察事件（不改状态）；若处于 `reviewing`，提示完成这一轮再结束 |

hooks 是防错护栏，不是安全边界（DN008）；skill 和 CLAUDE.md 只写流程与判断准则。

## 7. 平台假设的 PoC 结果

2026-11-05 在 Claude Code 桌面 app 中实测（父会话启动探测子会话）：

| 编号 | 假设 | 结果 | 设计中的处理 |
| --- | --- | --- | --- |
| A1 | `start_session` 可用 `cwd` 打开 tsw 创建的目录（`use_worktree:false`），model / effort / permission_mode 生效 | 成立。新目录须先由用户确认信任，确认只能在用户看着主会话时弹出 | 预启动在用户 turn 中进行（§3、§4） |
| A2 | 自动 turn 中不能创建会话，提案卡片可由用户点击启动 | 成立且更严格：消息触发的 turn 完全不能创建；唤醒 turn 只能在本项目创建；提案卡片不能指定目录。任何 turn 都能给已有会话发消息 | 预启动 + 后投递；取消 worker 的提案路径（§4、DN002 §5.6） |
| A3 | MCP server 在子会话中以子会话 cwd 启动 | 成立，且环境里有 app 会话 ID | 角色按会话 ID 解析（§5） |
| A4 | 子会话能给父会话发消息，主会话忙时排队 | 成立，须用 `mcp__ccd_session_mgmt__send_message(session_id="parent")` | worker 指令固定该工具（§4）；T254 起按会话 ID 发给当前持租主会话 |
| A5 | hooks 在子会话中生效，可拿到会话 ID 与 cwd | 成立 | 防错护栏与角色识别用 hooks（§6） |
| A6 | Codex App Server 一次性线程可用 `gpt-6-astra` / `medium` / read-only | 成立（设计评审中多次使用） | 默认评审器（[CX004](../codex/CX004-reviewer.md)） |
| A7 | `archive_session` 不删除 `use_worktree:false` 会话的目录 | 成立 | 归档会话不影响执行目录与证据 |
| A8 | `get_session` 返回子会话实际 model / effort / 权限模式 | 成立；模型 ID 可能带上下文后缀（如 `[1m]`） | 比较请求值与回执时规范化（DN003 §1） |
