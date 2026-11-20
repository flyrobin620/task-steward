# 0002 由完成改动的 worker 合并 PR

- 状态：已接受（2026-11-06，以用户确认合并本 PR 为准）；合并授权的范围、合并后核对由谁做、须用户确认的改动类别已被 [0003](0003-worker-delivery-loop.md) 取代
- 取代：[0001](0001-development-process.md) 第 7 条
- 适用：本仓库的全部 PR，包括由 TaskSteward 编排器派发的任务

## 背景

0001 第 7 条规定由编排器派发的任务须“编排者验收后由 agent 合并”。实际运行中，这让每个开发任务的合并都要绕经主会话：worker 完成门禁、评审与意见处理后，还要等主会话核对、再由主会话合并，主会话的上下文因此被具体任务占满。用户 2026-11-06 确立了新的原则：每个任务的工作尽量由 worker 在自己的会话中完成，主会话只负责协调与修改 TaskSteward 自身，并保持上下文干净（TaskSteward 设计见 DN002 §3.5）。

## 用户回答（2026-11-06）

| 问题 | 回答 |
| --- | --- |
| 开发任务由谁合并 | 默认由 worker 自己开 PR、自己合并（满足仓库门禁、评审与意见解决后），除非任务书或仓库 `AGENTS.md` 另有说明；本仓库派发的任务也由 worker 自行合并，改掉“编排者验收后合并” |
| 须用户确认才能合并的敏感改动类别是否保留 | 全部保留 |

## 决定

替代 0001 第 7 条：

7. **合并**：squash 合并。`Gate` 在最终 head 上为绿、最终 head 的评审通过、每条评审意见都已回复并解决后，由完成改动的 agent 合并；由编排器派发的任务即由 worker 自己合并，编排者在合并后核对（`main` 上合并提交的 CI、验收逐项），合并不代表用户已验收。以下改动须用户明确确认后才能合并：CI 与门禁（`.github/**`、`tools/` 中的门禁与 CI 包装器）、规则文档与决定记录（`AGENTS.md`、`CLAUDE.md`、`docs/development/**`、`docs/decisions/**`）、设计文档（`docs/design/**`）、发布与版本配置、权限与身份校验相关代码（角色解析、hooks 守卫、MCP 写工具授权）、`LICENSE`。确认须来自用户本人，worker 在自己的会话中直接问用户，并在 PR 中记录来源。

0001 的其他各条不变。

## 后果

- `AGENTS.md` 第 4 节第 6 步、[workflow.md](../development/workflow.md) 与 [review.md](../development/review.md) 的合并条件按本决定修改，并记入 [instructions-log.md](../development/instructions-log.md)。
- 在 TaskSteward 数据目录中，本仓库的 `delivery.merge` 须由主会话附用户原话开启（DN003 §7.2）后，worker 才能实际合并；开启之前，hooks 仍拒绝 worker 的合并命令，PR 由主会话合并。
- 合并后发现的问题以新的 PR 修复或 revert，不改写已合并的历史。
