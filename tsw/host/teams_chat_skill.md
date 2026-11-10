---
name: teams-chat
description: "TaskSteward 的 teams-chat 人类渠道（P3）在主会话中的做法：把转交给用户的问题按内容排成富文本卡片，用 WorkIQ 发到配置的 Teams chat 并登记；登记例行会话从 Teams 取回的回复；处理 Teams 读取失败。由 handle-input skill 在交给用户时调用。只用于在数据目录打开的主会话；worker 与例行会话不使用。"
---

# `teams-chat` 渠道

本 skill 只适用于主会话。`whoami` 的角色不是 orchestrator 时忽略它。何时交给用户由 handle-input skill 判断；这里只写 Teams 的发送、回复登记与读取失败。

发送与读取都用 WorkIQ MCP（`mcp__workiq__*`）。不用 `mcp__teams__*` 发送：主会话中它们会返回“Session not found”，例行会话也用 WorkIQ 读取。

## 发送问题卡片

1. `input_relay`（`channel: teams-chat`）。结果事件的 `data` 中：
   - `card_html`：卡片的富文本正文，已按问题内容排好（标题、任务、问题的段落、列表与代码、选项、回复方式、worker 会话链接各自分行），以 `[TaskSteward]` 开头；
   - `card`：同一内容的纯文本，只作参考，不要发送它（Teams 会把纯文本挤成一行）；
   - `chat`：目标 chat，`self` 表示用户本人的自聊。
2. 用 `mcp__ccd_session_mgmt__get_session` 取 worker 会话的 `link`，替换 `card_html` 中所有 `{worker_link}`（链接中的 `&` 写成 `&amp;`）。不要改写卡片的其他部分；不放凭据或秘密。
3. 发送：`mcp__workiq__create_entity`，`parentUrl` 在 `chat` 为 `self` 时是 `/me/chats/48:notes/messages`，否则是 `/me/chats/<chat>/messages`；`jsonBody` 为 `{"body": {"contentType": "html", "content": <替换后的 card_html>}}`。
4. 返回的是创建的消息：调用 `channel_sent`，`chat_id` 取返回的 `chatId`（没有时取 `parentUrl` 中的 chat，自聊为 `48:notes`），`message_id` 取 `id`，`sender_id` 取 `from.user.id`。例行表随之加入这个 chat 的 Teams 例行项（`teams-replies`），回复会转给你。
5. 发送失败（没有返回消息 `id`）时不调用 `channel_sent`，问题保持待答；按 handle-input skill 告诉用户。

## 登记回复（`teams-replies`）

例行会话取出 Teams chat 中以等待中任务 ID 或问题编号开头的消息（`channel.reply_seen`），转来以“TaskSteward 例行项 teams-replies”开头的消息。脚本不判断内容，由你判断：

1. 消息回答了该任务待答的问题：`input_resolve`，`user_source` 为 `{"channel": "teams-chat", "principal": "teams", "message_ref": <消息 ID>, "text": <消息原文>, "request_id": <问题编号>, "target": [<任务 ID>, <revision>, "answer"]}`，`answer` 按原文整理；可同时登记用户批准的追加轮次（`extend_rounds`）。然后用 `mcp__ccd_session_mgmt__send_message` 告诉 worker 用户的答复与问题编号，让它继续。
2. 不是答复（例如“稍后回复”“看一下”）：不登记，问题保持待答。
3. 问题已由别处答复、已取消或已过时：`input_resolve` 会记为迟到答复（`input.late_response`）；内容与已生效的答复冲突时，告诉 worker，由它在自己的会话里再问用户确认。

## 读取失败

例行会话连续几次读不到某个 Teams chat（工具报错、限流或返回错误）时，tsw 告诉你一次：消息写明 chat、连续失败的次数、开始时间、最近一次的原因，以及这个 chat 中等待中的问题；`attention` 的 `routine` 中也有对应的 `fetch_failing`，直到读取恢复。这不是账本事件，不需要 `inbox_ack`。

1. 自己用 `mcp__workiq__fetch` 读一次同一个 chat（`/me/chats/<chat>/messages?$top=5&$select=id,createdDateTime,from,body`，自聊的 chat 为 `48:notes`），区分是这个 chat 读不到，还是例行会话那边的工具出了问题。
2. 你能读到时：例行会话的工具或权限有问题（例如它的 WorkIQ 未连接、auto 模式规则仍是旧版本）。给例行会话发消息“运行 routine_board”，让它再试一次；仍然失败时告诉用户原因与需要用户做的事（例如更新 auto 模式规则后重启例行会话）。
3. 你也读不到时：告诉用户 Teams 读取失败、等待中的问题的回复暂时收不回来，可以直接在对应的 worker 会话里回答（会话内的回复始终有效）。
