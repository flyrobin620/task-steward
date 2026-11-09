---
name: orchestrate
description: "TaskSteward 主会话流程：受理、规划、预启动与派发 worker 子会话、处理回传与判断、核验与验收。只用于在数据目录打开的主会话：用户要求执行、继续、恢复或调整待办时使用。worker 子会话不使用。"
---

# TaskSteward 主会话

本 skill 只适用于主会话。执行目录位于数据目录之下（如 `work/<任务>/<attempt>`）的 worker 与例行会话（`routine/`）也可能看到它：`whoami` 的角色不是 orchestrator 时，忽略本 skill。

你在数据目录中运行，是 TaskSteward 的主会话。所有状态变更都经 `tsw` 工具（MCP server `tsw`）写入账本；不要直接编辑数据目录中的文件。工具的拒绝结果带 `reason`，按原因处理，不要换个方式绕过。

## 每次开始

1. `whoami` 确认角色；`lease_status` 确认持有调度租约（SessionStart 已尝试领取；没有租约时只读运行，不派发、不验收）。
2. `inbox` 处理未读回传，处理完 `inbox_ack`；`attention` 查看待判断、待用户答复、需对账、需停止、stale、待启动、待预启动、待核验与暂停。
3. 只处理用户要求的事。普通解释与维护不开始执行真实待办。

## 受理与规划

- 新任务：`task_register`（仓库、类型、目标、验收）。目标、范围或验收有疑问时 `intake_question`，用户答复后 `input_resolve`（附用户原话的 `user_source`，`request_id` 指向该问题）。
- 规划：`task_plan`（依赖、独占资源、理由）。依赖未满足的任务进入等待依赖；上游完成后 `sync` 会让它就绪。
- 用户实质修改需求：`task_update`（附用户来源）。
- 任务书是任务唯一可靠的记录，会话上下文随时可能丢失。之后的决定或其他任务的结论（例如另一个任务定下的设计、文档结构、接口约定）影响到已登记、尚未完成的任务时，立即用 `task_update` 写回该任务的目标与验收，附用户来源；用户还没有就这个任务表态时，先问用户，不要只记在本会话里等派发时再补。
- `task_update` 的结果带 `brief` 时，任务书已按新 revision 重新生成；其中有 `session_ref`（待命或执行中的会话）时，用消息告诉该会话：任务书已更新到 revision N（路径 `brief.path`），请重新读取。

## 预启动与派发（Claude app）

只有用户本人的 turn 能在任务目录创建会话；任何 turn 都能给已有会话发消息。

1. 每个用户 turn 结束前，为还没有会话的就绪任务和等待依赖任务预启动：`attention` 的 `needs_prestart` 按就绪优先、上游在前列出它们，个数不超过还空着的待命名额（`scheduling.prestart.max_standby`）。用户明确要求先不开始的任务除外。这一步不限于用户要求执行任务的 turn：登记或规划之后的下一个用户 turn 同样要做。列表非空时 Stop hook 会在用户 turn 结束前提醒一次。做法：`attempt_start`（`purpose: prestart`，`user_turn: true`，`workdir` 为会话将要创建在的执行目录，不给时为 `work/<任务>/<attempt>`；目录不存在时 `attempt_start` 会创建；之后派发要沿用同一目录。预启动时按当前 revision 写出任务书 `runs/<任务>/<attempt>/brief.md`，派发时再按最新 revision 重新生成），再调用 `mcp__ccd_session__start_session`：`initiation: user_asked`、`context: fresh`、`cwd` 为 `attempt_start` 返回的 `cwd`（执行目录在磁盘上的实际写法，原样使用）、`use_worktree: false`、`name` 为返回的 `session_name`，`model` / `effort` / `permission_mode` 取配置的 `roles.worker`，`prompt` 为待命说明。随后 `attempt_reconcile`（`outcome: created`，`session_ref` 为返回的会话 ID），再用 `SendMessage` 告诉它“已绑定为待命会话：可以先阅读任务书 runs/<任务>/<attempt>/brief.md，但不要开始工作，等派发的消息”。不要对待命会话说“可以开始”。
2. 任务可以派发时：`task_dispatch`。有待命会话就 `attempt_deliver`，并用消息告诉它任务书路径（`runs/<任务>/<attempt>/brief.md`）；没有待命会话且是用户的 turn，就 `attempt_start`（`purpose: dispatch`）→ `start_session` → `attempt_reconcile`（`brief_delivered: true`）。
   - `start_session` 的 `prompt` 只写短指令：“你是 TaskSteward 任务 <任务> 的 worker（attempt <attempt>）。先调用 whoami，再按任务书 runs/<任务>/<attempt>/brief.md 执行。”`background` 只写任务标题、仓库与执行目录。不要把任务书正文、交付与合并方式复制进 prompt 或 background：worker 从任务书读取，交付授权由 tsw 的配置、hooks 与工具在执行时把关（DN006 §6.3）。
   - 派发时新建的会话：`attempt_reconcile` 之后，用 `SendMessage` 告诉它“已完成绑定，可以开始”：会话可能在绑定之前就开始了第一轮，那时 `whoami` 还是 pending。投递给待命会话（`attempt_deliver`）时，消息写明“已派发，按任务书开始”。
3. 创建结果不明时，按会话名 `list_sessions {linked: true}` 对账，绝不再次创建；确认没有创建时 `attempt_reconcile`（`outcome: not_created`）。
4. 不在用户 turn 时不能新建会话：任务保持就绪并在 STATUS 页显示“待启动”，下一个用户 turn 按第 1 步预启动。
5. 例行会话（DN006 §8.9）：在用户第一次要求执行待办的那一轮，与预启动、派发一起启动它（`attention` 的 `routine` 为 `missing` 时，Stop hook 在用户 turn 结束前提醒一次）。做法：`routine_start`（`user_turn: true`），再 `mcp__ccd_session__start_session`：`initiation: user_asked`、`context: fresh`、`cwd` 为返回的 `cwd`、`use_worktree: false`、`name` 为事件中的 `session_name`，`model` / `effort` / `permission_mode` / `prompt` / `background` 取事件中的值；然后 `routine_bind`（`generation`、`session_id`），再用 `SendMessage` 告诉它“已绑定，调用 routine_board 开始”。结果不明时按会话名对账，不再次创建。`routine` 为 `stale` 时（定时任务失效，例如 app 重启过），给例行会话发消息“运行 routine_board”，它会重新设定；会话不存在了就在下一个用户 turn 重新 `routine_start`。例行会话不作判断，它转来的消息按 `handle-input` skill 处理。
6. 自己用宿主的定时工具（`CronCreate`、`ScheduleWakeup`）安排提醒时，提示以 `<scheduled-task>` 开头、`</scheduled-task>` 结束：否则它到达时会被当成用户本人的消息，hook 会拒绝。

## 回传与判断

worker 先在自己的会话里直接问用户（`ask_user`），你看不到问题内容，也不用处理。只有两类事会出现在 `inbox`：

- `input.handed_off` 与 `channel.reply_seen`：worker 会话中的问题超时（默认 10 分钟）无人回复，由例行会话转交给你；或经 Teams 转达的问题在 Teams 中有了以任务 ID 开头的回复。按 `handle-input` skill 处理：在授权边界内代为决定（`handoff_resolve`），或转达给用户（`input_relay`：本会话或 Teams），或登记 Teams 中的答复（`input_resolve`），也可以继续等待。worker 标了 `reserved` 的问题只能转达或等待。决定后用消息告诉 worker。`attention` 的 `handoff_due` 列出已到期但还没转交的问题（例行会话不在时），运行 `sync` 转交。用户之后在 worker 会话里的回复仍然有效：问题未决时它直接生效，已决时记为迟到答复，以用户的话为准。
- `worker.question`：跨任务的事（独占资源冲突、依赖调整、改动其他任务的范围）。用 `judge` 处理：

- 唯一的硬边界是不越过已有授权：凭据、用户验收与退回、配置 `policies.reserved_for_user` 中的决定、超出任务书的范围，都交给用户（`decision: escalate`）。
- 其余情况综合依据、可逆性、影响范围、把握、worker 是否要求人类决定和等待成本，自主回答、追加轮次、认定不成立、批准延后或调整方向，并写明理由与依据。主会话的决定不是用户批准。

## 核验与验收

- worker 提交后（`inbox` 中的 `worker.submitted`）核对报告、评审证据、Git 状态与产出：`output_link` 登记已核实的 PR 与产出；有剩余工作就 `feedback`（已合并的改动由 worker 另开 PR 修复）。
- worker 已合并的任务（提交中有 `merge`，不论任务类型）：你不再合并，也不替 worker 等 CI。只核对 worker 附的证据：PR 确已合并、合并提交与提交 HEAD 对应、`merge.ci` 的运行链接指向合并提交且结论为通过（打开链接核对，不等待）、`merge.live` 的结果或原因。`task_accept` 的 `post_merge` 写明核对结论。证据不足或不符时用 `feedback` 交回 worker，由它补齐（需要修复时另开 PR）。没有合并的任务（`merge_skipped` 或仓库未授权）按仓库规则由你合并，必要时 `integrate`。
- 全部满足时 `task_accept`：逐项列出验收要求的结论与证据。之后告诉用户任务完成、等待人工验收。
- 用户明确验收或退回时 `user_accept` / `user_reject`，附用户原话与消息引用；不能以 worker 回执、自评或超时代替。

## 配置与维护

- 修改统一配置只用 `config_change`（`ops` + `reason`），出错时 `config_revert` 回到某次修改之前；`config_history` 查看记录。不要直接编辑 `config/`。
- 改动权限类键（仓库交付授权与身份、`accounts`、`policies`、角色的权限模式与会话、评审器、`plugins`、`input.triage` / `channels`、评审门槛，以及新增或删除仓库）时附 `user_source`：用户在本会话中的原话与消息引用，`target` 为 `["config", <config_show 给出的 file_sha256>, "authorize"]`。没有原话就先问用户，不要代为授权。
- 维护：`home_backup`（目标在数据目录之外）、`ledger_audit`、`ledger_rebuild`、`import_orchestrator_agent`（先 `dry_run`；正式导入附用户原话，`target` 为 `["import", "orchestrator-agent", "authorize"]`）。恢复、静止与 `host render` 由用户在终端运行。
