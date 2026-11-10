---
name: handle-input
description: "TaskSteward 主会话处理问题的流程：例行会话转来超时无人回复的会话内问题（handoff-scan）后，判断代为回答还是交给用户；交给用户时按配置的人类渠道（P3，如 teams-chat）通知用户，渠道的发送与回复登记按该渠道的 skill。只用于在数据目录打开的主会话；worker 与例行会话不使用。"
---

# 处理问题：超时转交 → 判断 → 按配置的人类渠道通知用户

本 skill 只适用于主会话。`whoami` 的角色不是 orchestrator 时忽略它。

worker 遇到任务内需要用户决定的事，先在自己的会话里问用户（`ask_user`）。之后分三步，前一步由系统完成，后两步由你完成。事件都已在账本中：`inbox` 里有对应的 `input.handed_off` 或 `channel.reply_seen`，处理完 `inbox_ack`。

## 1. 例行会话发现超时的问题并通知你（`handoff-scan`）

问题超时（`input.in_session_timeout_minutes`）且用户没有在 worker 会话里回复时，例行会话的 `handoff-scan` 把它转交给你（`input.handed_off`，`by: system`），并发来以“TaskSteward 例行项 handoff-scan”开头的消息：逐条列出任务、问题编号（`Q-…`）、是否 `reserved`、worker 会话 ID 与问题全文，以及配置的人类渠道。例行会话不在时，`attention` 的 `handoff_due` 列出到期的问题，运行 `sync` 转交。

## 2. 判断：代为回答，还是交给用户

逐个问题读全文、任务书（`task_read`、`context_*`）与相关决定，按 orchestrate skill“回传与判断”的硬边界判断：

- **只能交给用户**：凭据与登录确认、用户验收与退回、`policies.reserved_for_user`、超出任务书与配置授权的事，以及 worker 标为 `reserved` 的问题（`handoff_resolve` 会拒绝它）。worker 明确要求用户决定时默认交给用户，不这样做要写明理由。
- **可以代答**：有明确依据、可逆、只影响本任务、把握高。`handoff_resolve`（回答、追加轮次、认定不成立或批准延后，写明理由、依据与把握），再用 `mcp__ccd_session_mgmt__send_message` 把决定告诉 worker（会话 ID 见转来的消息），让它继续。到此结束，不再通知用户。

## 3. 交给用户：按配置的人类渠道通知

渠道就是配置 `input.channels` 中 `in-session` 以外的渠道（P3 人类渠道插件，须在 `plugins.human_channels` 中加载），转来的消息写明了它们；配置的是什么就用什么。逐个渠道：

1. `input_relay`（`task_id`、`request_id`、`channel` 为该渠道），记录 `input.relayed`。
2. 按**该渠道的 skill** 发出（与渠道同名，例如 `teams-chat`）：它写明用什么工具、发什么格式、怎样登记发送结果、回复怎样取回与登记。本 skill 不写这些细节。
3. 发送失败时问题保持待答；告诉用户这个渠道没有发出去，以及问题可以直接在 worker 会话里回答。

不要做的事：

- 不在主会话里重复提问，也不只是“等待”：配置了会话之外的渠道时，就经它通知用户。
- 只有配置中没有 `in-session` 以外的渠道时（转来的消息会写明），才 `input_relay`（`channel: main-session`），在本会话中把问题与 worker 会话告诉用户；用户在本会话答复时用 `input_resolve` 记录（`user_source` 为用户在本会话的原话），再告诉 worker。
- 卡片和消息里不放凭据或秘密。

用户之后无论在哪里回答都有效：在 worker 会话里回答由 worker 登记；经渠道回答的，渠道的回复转回你（例如 Teams 的 `teams-replies`），按该渠道的 skill 判断并用 `input_resolve` 登记，再告诉 worker。第一个生效的答复决定结果，之后的记为迟到答复（`input.late_response`）；内容与已生效的答复冲突时，告诉 worker，由它在自己的会话里再问用户确认。
