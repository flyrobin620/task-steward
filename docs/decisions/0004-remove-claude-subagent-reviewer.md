# 0004 删除子代理评审器 `claude-subagent` 与 `review_run` 的结果交回参数

- 状态：已接受（2026-11-21，以用户确认 T331 的设计为准）
- 适用：TaskSteward 的评审器（DN004 §3、CC004）与 `review_run` / `tsw review run` 的对外表面

## 背景

T325（PR #74）把没有 Codex 时的 Claude 评审器做成 worker 会话中的只读子代理 `claude-subagent`：`review_run` 只开轮并返回启动指示，worker 用 Agent 工具启动插件子代理 `task-steward:reviewer`，再带 `round`、`result`（或 `error`）与 `agent_id` 调用 `review_run` 交回结果。用户审视 F-T325-3（用 hook 核对 worker 确实启动了这个子代理）时指出，worker 有意操纵时可以改写或伪造交回的结果；`codex-app-server` 与 `copilot-cli` 由 tsw 启动并读回结果、记账，结果不经过 worker。

## 用户原话（2026-11-21，主会话）

- “评审器由 tsw 自己启动并读回结果，然后记账，结果不经过 worker 的手。>> 核心点在这里。reviewer是由tsw启动的。所以codex和copilot-cli就都是对的。所以正确的改法其实是换回claude-headless， 由tsw启动，就行了？”
- “F-T325-3 转任务，删掉 claude-subagent”

这取代了 T308 会话中的选择（M-T308-10：“claude 自己的reviewer用subagent做就可以了，不用专门用claude headless或者cli”）。

## 决定

1. Claude 评审器改为由 tsw 启动的 `claude -p`（会话适配器 `claude-headless`，CC004）。所有评审器都由 tsw 启动，结果由 tsw 读回并记账（DN004 §3）。
2. 删除 `claude-subagent`：会话适配器、插件子代理 `plugin/agents/reviewer.md`、worker 说明中的相关步骤、能力声明中的 `oneshot_via` / `worker_agent`。
3. 删除 `review_run` 的结果交回参数 `round`、`result`、`error`、`agent_id`，以及 `tsw review run` 的 `--round`、`--result`、`--result-file`、`--error`、`--agent-id`。这是对外契约的删除（[compatibility.md](../development/compatibility.md)），按该文“必须删除或不兼容变更时”处理：

| 项 | 内容 |
| --- | --- |
| 受影响的读取方 | 按 T325 版本 worker 说明交回子代理结果的 worker；本仓库的 live 场景 `reviewer`。这些参数在 T325 合并当天引入，只在本机使用过 |
| 迁移 | 配置中出现 `claude-subagent`（`roles.reviewer.session`、`fallback_session` 或 `plugins.sessions`）时配置校验失败，错误信息写明改为 `claude-headless`（模型别名可以沿用）；`tsw setup` 重跑时，早先保存的答案 `claude-subagent` 改为 `claude-headless` 并给出警告。T325 版本开启、仍在等子代理结果的轮次，重试时改用当前配置的评审器（DN004 §3.2） |
| 执行顺序 | 同一个 PR 中先改设计，再改代码、worker 说明与插件；合并后重新生成宿主文件（插件更新）才对 worker 生效 |
| 回退 | revert 该 PR：恢复 T325 的子代理方式与参数；账本事件没有变化（评审仍记录为 `review_start` / `review_complete` / `review_fail`），回退不需要迁移数据 |

## 后果

- DN004 §3、CC004、DN006 §7、DN003 的评审器配置说明随之修改；`claude-subagent` 的做法与删除理由存档于 CC004 附录 A，DN004 附录 A 记录取舍。
- [testing-layers.md](../development/testing-layers.md) 的 live 场景 `reviewer` 覆盖 `codex-app-server`、`claude-headless`、`copilot-cli`。
- worker 仍能影响评审的输入（范围起点、`notes`、代码中的注释），本决定不处理这一点。
