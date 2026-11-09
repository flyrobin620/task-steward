---
name: handle-input
description: "TaskSteward 主会话处理转交来的问题：例行会话转来的超时会话内问题（handoff-scan）与 Teams 中的回复（teams-replies）。决定代为回答，还是经主会话或 Teams 问用户。只用于在数据目录打开的主会话；worker 与例行会话不使用。"
---

# 处理转交来的问题

本 skill 只适用于主会话。`whoami` 的角色不是 orchestrator 时忽略它。

例行会话按例行表运行脚本，把结果原样发给你。消息以“TaskSteward 例行项 …”开头，列出问题编号（`Q-…`）。事件本身已在账本中：`inbox` 里有对应的 `input.handed_off` 或 `channel.reply_seen`，处理完 `inbox_ack`。

## 超时转交的问题（`handoff-scan`）

worker 在自己的会话里问用户，超时无人回复，系统已把问题转交给你（`input.handed_off`，`by: system`）。逐个问题：

1. 读问题全文、任务书（`task_read`、`context_*`）与相关决定，按 orchestrate skill“回传与判断”的硬边界判断：凭据、用户验收与退回、`policies.reserved_for_user`、超出任务书与配置授权的事，以及 worker 标为 `reserved` 的问题，都只能交给用户。
2. **能代答**（有明确依据、可逆、只影响本任务、把握高）：`handoff_resolve`（回答、追加轮次、认定不成立或批准延后，写明理由、依据与把握），再用 `mcp__ccd_session_mgmt__send_message` 把决定告诉 worker（会话 ID 见转来的消息），让它继续。
3. **需要用户**：
   - 用户正在本会话中与你对话时：`input_relay`（`channel: main-session`），在本会话中问用户；答复用 `input_resolve` 记录（`user_source` 为用户在本会话的原话），再告诉 worker。
   - 否则，配置加载了 `teams-chat` 时转达到 Teams：`input_relay`（`channel: teams-chat`）。结果中的 `card` 是卡片正文：用 `mcp__ccd_session_mgmt__get_session` 取 worker 会话的 `link`，替换卡片中的 `{worker_link}`；`chat` 为 `self` 时用 `mcp__teams__SendMessageToSelf` 发送，否则用 `mcp__teams__SendMessageToChat`（`chatId` 为 `chat`）。发送后调用 `channel_sent`，登记返回的 chat ID、消息 ID 与发送者 ID（返回中有时）。例行表随之加入 Teams 例行项，回复会再转给你。
   - 都不合适时继续等待：问题保持待答，用户之后在 worker 会话里的回复仍然有效。
4. 卡片和消息里不要放凭据或秘密。

## Teams 中的回复（`teams-replies`）

例行表取出了 Teams chat 中以等待中任务 ID 开头的消息（`channel.reply_seen`）。脚本不判断内容，由你判断：

1. 消息回答了该任务待答的问题：`input_resolve`，`user_source` 为 `{"channel": "teams-chat", "principal": "teams", "message_ref": <消息 ID>, "text": <消息原文>, "request_id": <问题编号>, "target": [<任务 ID>, <revision>, "answer"]}`，`answer` 按原文整理；可同时登记用户批准的追加轮次（`extend_rounds`）。然后用 `send_message` 告诉 worker 用户的答复与问题编号，让它继续。
2. 不是答复（例如“稍后回复”“看一下”）：不登记，问题保持待答。
3. 问题已由别处答复、已取消或已过时：`input_resolve` 会记为迟到答复（`input.late_response`）；内容与已生效的答复冲突时，告诉 worker，由它在自己的会话里再问用户确认。
