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
   - 否则，配置加载了 `teams-chat` 时转达到 Teams：`input_relay`（`channel: teams-chat`）。结果中的 `card` 是卡片正文：用 `mcp__ccd_session_mgmt__get_session` 取 worker 会话的 `link`，替换卡片中的 `{worker_link}`；用 WorkIQ 发送：`mcp__workiq__create_entity`，`parentUrl` 在 `chat` 为 `self` 时是 `/me/chats/48:notes/messages`（用户本人的自聊），否则是 `/me/chats/<chat>/messages`；`jsonBody` 为 `{"body": {"contentType": "text", "content": <卡片正文>}}`。不用 `mcp__teams__*` 发送（主会话中它们会返回“Session not found”；例行会话也用 WorkIQ 读取）。返回的是创建的消息：发送后调用 `channel_sent`，`chat_id` 取返回的 `chatId`（没有时取 `parentUrl` 中的 chat，自聊为 `48:notes`），`message_id` 取 `id`，`sender_id` 取 `from.user.id`。发送失败（没有返回消息 `id`）时不调用 `channel_sent`，问题保持待答。例行表随之加入 Teams 例行项，回复会再转给你。
   - 都不合适时继续等待：问题保持待答，用户之后在 worker 会话里的回复仍然有效。
4. 卡片和消息里不要放凭据或秘密。

## Teams 中的回复（`teams-replies`）

例行表取出了 Teams chat 中以等待中任务 ID 开头的消息（`channel.reply_seen`）。脚本不判断内容，由你判断：

1. 消息回答了该任务待答的问题：`input_resolve`，`user_source` 为 `{"channel": "teams-chat", "principal": "teams", "message_ref": <消息 ID>, "text": <消息原文>, "request_id": <问题编号>, "target": [<任务 ID>, <revision>, "answer"]}`，`answer` 按原文整理；可同时登记用户批准的追加轮次（`extend_rounds`）。然后用 `send_message` 告诉 worker 用户的答复与问题编号，让它继续。
2. 不是答复（例如“稍后回复”“看一下”）：不登记，问题保持待答。
3. 问题已由别处答复、已取消或已过时：`input_resolve` 会记为迟到答复（`input.late_response`）；内容与已生效的答复冲突时，告诉 worker，由它在自己的会话里再问用户确认。

## Teams 读取失败（`teams-replies`）

例行会话连续几次读不到某个 Teams chat（工具报错、限流或返回错误）时，tsw 告诉你一次：消息写明 chat、连续失败的次数、开始时间、最近一次的原因，以及这个 chat 中等待中的问题；`attention` 的 `routine` 中也有对应的 `fetch_failing`，直到读取恢复。这不是账本事件，不需要 `inbox_ack`。

1. 自己用 `mcp__workiq__fetch` 读一次同一个 chat（`/me/chats/<chat>/messages?$top=5&$select=id,createdDateTime,from,body`，自聊的 chat 为 `48:notes`），区分是这个 chat 读不到，还是例行会话那边的工具出了问题。
2. 你能读到时：例行会话的工具或权限有问题（例如它的 WorkIQ 未连接、auto 模式规则仍是旧版本）。给例行会话发消息“运行 routine_board”，让它再试一次；仍然失败时告诉用户原因与需要用户做的事（例如更新 auto 模式规则后重启例行会话）。
3. 你也读不到时：告诉用户 Teams 读取失败、等待中的问题的回复暂时收不回来，可以直接在对应的 worker 会话里回答（会话内的回复始终有效）。
