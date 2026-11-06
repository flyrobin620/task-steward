---
name: orchestrate
description: "TaskSteward 主会话流程：受理、规划、预启动与派发 worker 子会话、处理回传与判断、核验与验收。只用于在数据目录打开的主会话：用户要求执行、继续、恢复或调整待办时使用。worker 子会话不使用。"
---

# TaskSteward 主会话

本 skill 只适用于主会话。执行目录位于数据目录之下（如 `work/<任务>/<attempt>`）的 worker 也可能看到它：`whoami` 的角色不是 orchestrator 时，忽略本 skill，按任务书工作。

你在数据目录中运行，是 TaskSteward 的主会话。所有状态变更都经 `tsw` 工具（MCP server `tsw`）写入账本；不要直接编辑数据目录中的文件。工具的拒绝结果带 `reason`，按原因处理，不要换个方式绕过。

## 每次开始

1. `whoami` 确认角色；`lease_status` 确认持有调度租约（SessionStart 已尝试领取；没有租约时只读运行，不派发、不验收）。
2. `inbox` 处理未读回传，处理完 `inbox_ack`；`attention` 查看待判断、待用户答复、需对账、需停止、stale、待启动、待核验与暂停。
3. 只处理用户要求的事。普通解释与维护不开始执行真实待办。

## 受理与规划

- 新任务：`task_register`（仓库、类型、目标、验收）。目标、范围或验收有疑问时 `intake_question`，用户答复后 `input_resolve`（附用户原话的 `user_source`，`request_id` 指向该问题）。
- 规划：`task_plan`（依赖、独占资源、理由）。依赖未满足的任务进入等待依赖；上游完成后 `sync` 会让它就绪。
- 用户实质修改需求：`task_update`（附用户来源）。

## 预启动与派发（Claude app）

只有用户本人的 turn 能在任务目录创建会话；任何 turn 都能给已有会话发消息。

1. 用户要求执行一批任务时，在这个 turn 里为就绪的任务和排在前面的等待依赖任务预启动：`attempt_start`（`purpose: prestart`，`user_turn: true`，`workdir` 为会话将要创建在的执行目录，不给时为 `work/<任务>/<attempt>`；之后派发要沿用同一目录），再调用 `mcp__ccd_session__start_session`：`initiation: user_asked`、`context: fresh`、`cwd` 为执行目录、`use_worktree: false`、`name` 为返回的 `session_name`，`model` / `effort` / `permission_mode` 取配置的 `roles.worker`，`prompt` 为待命说明。随后 `attempt_reconcile`（`outcome: created`，`session_ref` 为返回的会话 ID）。
2. 任务可以派发时：`task_dispatch`。有待命会话就 `attempt_deliver`，并用消息把任务书（`runs/<任务>/<attempt>/brief.md`）发给它；没有待命会话且是用户的 turn，就 `attempt_start`（`purpose: dispatch`）→ `start_session` → `attempt_reconcile`（`brief_delivered: true`）。
3. 创建结果不明时，按会话名 `list_sessions {linked: true}` 对账，绝不再次创建；确认没有创建时 `attempt_reconcile`（`outcome: not_created`）。
4. 不在用户 turn 时不能新建会话：任务保持就绪并在 STATUS 页显示“待启动”。

## 回传与判断

worker 提问、评审预算用完、阻断项延后请求、评审器不可用都会出现在 `inbox` 与 `attention`。用 `judge` 处理：

- 唯一的硬边界是不越过已有授权：凭据、用户验收与退回、配置 `policies.reserved_for_user` 中的决定、超出任务书的范围，都交给用户（`decision: escalate`）。
- 其余情况综合依据、可逆性、影响范围、把握、worker 是否要求人类决定和等待成本，自主回答、追加轮次、认定不成立、批准延后或调整方向，并写明理由与依据。主会话的决定不是用户批准。

## 核验与验收

- worker 提交后（`inbox` 中的 `worker.submitted`）核对报告、评审证据、Git 状态与产出：`output_link` 登记已核实的 PR 与产出，必要时 `integrate`；有剩余工作就 `feedback`。
- 全部满足时 `task_accept`：逐项列出验收要求的结论与证据。之后告诉用户任务完成、等待人工验收。
- 用户明确验收或退回时 `user_accept` / `user_reject`，附用户原话与消息引用；不能以 worker 回执、自评或超时代替。

## 配置与维护

- 修改统一配置只用 `config_change`（`ops` + `reason`），出错时 `config_revert` 回到某次修改之前；`config_history` 查看记录。不要直接编辑 `config/`。
- 改动权限类键（仓库交付授权与身份、`accounts`、`policies`、角色的权限模式与会话、评审器、`plugins`、`input.triage` / `channels`、评审门槛，以及新增或删除仓库）时附 `user_source`：用户在本会话中的原话与消息引用，`target` 为 `["config", <config_show 给出的 file_sha256>, "authorize"]`。没有原话就先问用户，不要代为授权。
- 维护：`home_backup`（目标在数据目录之外）、`ledger_audit`、`ledger_rebuild`、`import_orchestrator_agent`（先 `dry_run`；正式导入附用户原话，`target` 为 `["import", "orchestrator-agent", "authorize"]`）。恢复、静止与 `host render` 由用户在终端运行。
