---
name: handle-input
description: "TaskSteward 主会话处理问题的流程：例行会话转来超时无人回复的会话内问题（handoff-scan）后，判断代为回答还是交给用户；交给用户时按 plugins.human_channels 配置的人类渠道（P3）处理：in-session 在主会话中直接问，teams-chat 等按该渠道自己的 skill。只用于在数据目录打开的主会话；worker 与例行会话不使用。"
---

# 处理问题：超时转交 → 判断 → 按配置的人类渠道交给用户

本 skill 只适用于主会话。`whoami` 的角色不是 orchestrator 时忽略它。

三个层次：

- **worker 层**：worker 遇到任务内需要用户决定的事，先在自己的会话里问用户（`ask_user`），用户在 worker 会话里回答（`user_answer`）。这一层不受 `plugins.human_channels` 控制，任何时候都可用，也不经过你。
- **主会话层**（本 skill）：问题超时无人回复时转交给你；`plugins.human_channels` 只决定你把应由用户决定的问题交给用户的方式。
- **渠道层**：渠道怎么发送、排版、回收回复，由渠道自己的 skill 写明（与渠道同名，例如 `teams-chat`），本 skill 不写。

事件都已在账本中：`inbox` 里有对应的 `input.handed_off` 或 `channel.reply_seen`，处理完 `inbox_ack`。

## 1. 例行会话发现超时的问题并通知你（`handoff-scan`）

问题超时（`input.in_session_timeout_minutes`）且用户没有在 worker 会话里回复时，例行会话的 `handoff-scan` 把它转交给你（`input.handed_off`，`by: system`），并发来以“TaskSteward 例行项 handoff-scan”开头的消息：逐条列出任务、问题编号（`Q-…`）、是否 `reserved`、worker 会话 ID 与问题全文，以及配置的人类渠道。例行会话不在时，`attention` 的 `handoff_due` 列出到期的问题，运行 `sync` 转交。

## 2. 判断：代为回答，还是交给用户

逐个问题读全文、任务书（`task_read`、`context_*`）与相关决定，按 orchestrate skill“回传与判断”的硬边界判断：

- **只能交给用户**：凭据与登录确认、用户验收与退回、`policies.reserved_for_user`、超出任务书与配置授权的事，以及 worker 标为 `reserved` 的问题（`handoff_resolve` 会拒绝它）。worker 明确要求用户决定时默认交给用户，不这样做要写明理由。
- **可以代答**：有明确依据、可逆、只影响本任务、把握高。`handoff_resolve`（回答、追加轮次、认定不成立或批准延后，写明理由、依据与把握），再用 `mcp__ccd_session_mgmt__send_message` 把决定告诉 worker（会话 ID 见转来的消息），让它继续。到此结束，不再通知用户。

## 3. 交给用户：按 `plugins.human_channels` 处理

配置的是什么就用什么，没有配置的方式不用（转来的消息写明了配置的渠道）。对每个配置的渠道：

- **`in-session`**：在主会话中直接问用户。`input_relay`（`channel: in-session`），然后在本会话的回复里写出问题全文、任务与 worker 会话。用户在本会话答复时，用 `input_resolve` 记录（`user_source` 的 `channel` 为 `main-session`，`text` 为用户在本会话的原话，`request_id` 为问题编号），再告诉 worker。
- **`teams-chat`** 或其他渠道插件：`input_relay`（`channel` 为该渠道），然后按与渠道同名的 skill 发出、登记发送结果，并在回复转回时按它登记。

注意：

- 没有配置 `in-session` 时，不在主会话里问用户（`input_relay` 也会拒绝 `in-session`）；不要用“请到 worker 会话回答”代替渠道。
- 发送失败时问题保持待答，在你下一次向用户汇报时说明这个渠道没有发出去；问题仍可在 worker 会话里回答。
- 配置中一个人类渠道也没有时，问题留在 worker 会话，等用户在那里回答。
- 卡片和消息里不放凭据或秘密。

用户无论在哪里回答都有效：在 worker 会话里回答由 worker 登记；经渠道回答的按该渠道的方式登记（`input_resolve`），再告诉 worker。第一个生效的答复决定结果，之后的记为迟到答复（`input.late_response`）；内容与已生效的答复冲突时，告诉 worker，由它在自己的会话里再问用户确认。
