# DN004 worker 自评审：评审与迭代决策

- 状态：设计草案（T233 r5，已按 PoC 结果修订）
- 日期：2026-11-05
- 相关：[DN002 任务状态机 §3.3](DN002-task-state-machine.md) · [DN003 统一配置](DN003-unified-config.md) · 实现：[CX004 Codex 评审器](codex/CX004-reviewer.md) · [CC004 Claude Code 评审器](claude-code/CC004-reviewer.md)

## 1. 目标与变化

worker 完成任务成果后，**由 worker 自己**启动 Codex（`gpt-6-astra`，推理强度 `medium`，只读沙箱）评审，逐条处置意见，并自行决定迭代还是定稿。定稿后提交给主会话验收，最后交用户人工验收。

与 orchestrator-agent 相比：

| 项 | orchestrator-agent | 本设计 |
| --- | --- | --- |
| Astra 对照版（另一模型独立出一版成品再比较） | 默认用于面向用户的图、报告正文和 HTML | **删除** |
| 代码审查 reviewer | worker 或主控按需启动 | 所有任务类型**必经**的自评审环节（不可由配置关闭） |
| 是否迭代 | 主控根据审查反馈决定 | worker 决定；主控在验收时复核其处置理由 |
| 证据 | 审查意见写在报告里 | 每轮请求、结构化结果、逐条处置分别落盘并进入账本 |

## 2. 流程

![自评审循环](diagrams/review-loop.svg)

图源：[diagrams/review-loop.mmd](diagrams/review-loop.mmd)

1. **成果就绪**：代码 / 设计任务已在 worktree 提交；研究任务的报告草稿已写入 `work/Txxx/aN/`。
2. **生成评审包**：worker 调用 `review_run`，tsw 按任务类型渲染 `review/rK-request.md`，内容包括：
   - 任务书当前 revision 的目标与验收要求（原文）；
   - 评审范围：代码 / 设计为 `base..HEAD` 的 diff 和变更文件列表，研究为报告与证据目录；
   - 已执行的验证命令及其结果摘要；
   - 适用的目标仓库约定文件路径（reviewer 自行只读查阅）；
   - 输出 schema 与严重级别定义；
   - 增量轮次另含上轮意见、worker 的处置，以及上轮 HEAD..新 HEAD 的 diff。
3. **调用 Codex**：tsw 启动一次性只读线程（§3），等待完成并读回最终消息。
4. **结构化结果**：解析为 `review/rK.json`；不满足 schema 时视为该轮失败（E02 重试）。
5. **逐条处置**（`review_triage`）：阻断项（`review.block_on` 中的级别）只能标为 `fix` 或 `reject`；非阻断项还可以标为 `defer`。`reject` 和 `defer` 必须写理由。阻断项确需延后时，worker 不能自行 defer，而是提交 `review.defer_requested` 交主会话判断。
6. **决定**（`review_decide`）：
   - `iterate`：存在需修复的阻断项，且本周期轮次 < 本周期有效预算（`max_rounds` + 主会话追加 + 用户追加，见 DN002 §3.3）。修改并提交新 HEAD 后进入下一轮增量评审。
   - `finalize`：每个阻断项都已按 G-SUBMIT 处置：`fix` 且已验证、有据 `reject`、主会话认定不成立（`review.finding_overruled`），或经主会话 / 用户批准延后（`review.defer_approved`，列入报告“已知问题”）。
   - `escalate`：本提交周期预算用完仍有阻断项（`review.budget_exhausted`）、阻断项需要延后（`review.defer_requested`），或评审器不可用（`review.unavailable`）。worker 随即在自己的会话中问用户（`ask_user`，DN006 §8.1a），由用户决定追加轮次、认定意见不成立、批准延后或调整方向；任务进入 `waiting_user`。超时无回复时转交主会话，主会话可在授权边界内代为决定（DN006 §8.1b）。
7. **终版一致性**：`finalize` 时如果当前 HEAD 晚于最后评审的 HEAD（例如定稿后改了报告链接），必须对差异再做一轮增量评审；这一轮同样计入本周期预算，预算耗尽时 `escalate`。
8. **提交**：调用 `submit`，tsw 执行 G-SUBMIT（DN002 §4）。

## 3. 评审器调用

评审器不是单独的插件类型，而是由 `roles.reviewer.session` 选用一个具备 `oneshot`、`direct`、`read_only_enforced` 能力的会话适配器（DN006 §7.4），可以与主会话、worker 使用的适配器不同。评审包渲染、输出 schema 校验、轮次计数与增量范围属于 tsw 核心。各实现见 §3.1；评审输出契约（§4）与处置规则（§5）对所有实现相同。

### 3.1 评审器实现

| 会话适配器 | 方式 | 实现文档 |
| --- | --- | --- |
| `codex-app-server`（默认） | Codex App Server 一次性线程，`sandbox=read-only`，续用线程做增量评审 | [CX004 §2](codex/CX004-reviewer.md) |
| `codex-exec`（回退） | `codex exec --sandbox read-only` | [CX004 §3](codex/CX004-reviewer.md) |
| `claude-headless`（无 Codex 时） | Claude Code 命令行一次性评审，严格只读工具集 | [CC004](claude-code/CC004-reviewer.md) |

所有实现共同的规则：使用配置指定的模型与 effort；模型不可用、effort 不支持或返回的模型与请求不一致时判为本轮失败，**不回退到其他模型**；默认评审器与回退都失败时记录 `review.unavailable` 交主会话判断（可稍后重试或交给人类），**不能跳过评审，也不能改用 worker 的子代理代替配置的评审器**。

### 3.2 v1 实现范围

- `review_run`（MCP，worker）与 `tsw review run`（命令行）是同一实现。开轮之前先检查能检查的：调用者是 live 且目录一致的 worker；评审器命令存在；Git 执行目录中 `head`、`base` 必须是真实提交（只把解析出的提交 ID 交给 git，选项形式的输入直接拒绝）；没有给 `base` 时取 `repositories.<repo>.base_ref`、`origin/HEAD`、`main` 等与 HEAD 的分叉点，取不到就要求显式指定。同一 attempt 同时只运行一轮（`locks/` 中的文件锁）。
- 开轮（`review_start`）后渲染 `runs/Txxx/aN/review/rK-request.md`，调用评审器，结果写入 `rK.json`、调用信息写入 `rK-run.json`（范围、线程 ID、模型、effort、是否续用），然后记录 `review_complete`。评审器报错、启动失败、超时、输出不是 JSON、不满足契约，或 `reviewed_head` 与本轮 HEAD 不一致（Git 缩写须解析为同一提交，非 Git 成果标识须完全相同）时，保存原始输出 `rK-raw.txt` 并记录 `review_fail`；开轮后的任何异常都以失败收尾。任务处于 `reviewing`（E02）时再次调用即在同一轮、按落账的 `head` 与 `base` 重试；落账的范围同样要是执行目录中的完整提交（轮次可能是直接用 `review_start` 开的），否则不调用评审器、记录失败。
- 每轮请求都带当前 revision 的目标与验收原文；需求变更（E07）开启新周期后不续用旧线程，按新需求完整评审。
- 默认评审器 `codex-app-server` 的进程与线程管理见 [CX004 §4](codex/CX004-reviewer.md)。
- `codex-exec` 与 `claude-headless` 尚未实现：配置为评审会话时 `review_run` 返回 `UNSUPPORTED`，不落账新的一轮。

### 3.3 只读与隔离

- reviewer 线程只读，不能修改 worktree，不能 push，不能访问凭据目录。
- 评审包和结果由 tsw 写入该 attempt 的 `runs/Txxx/aN/review/`（worker 不直接写 `runs/`，见 DN006 §9.4）。
- 一次性线程不进入调度、不绑定任务、不占用 `max_workers` 名额。每个 attempt 的 reviewer 线程只服务于该 attempt，不跨任务复用。

## 4. 输出契约

`config/schemas/review-result.schema.json` 约定 Codex 必须只输出如下 JSON：

```json
{
  "verdict": "changes_requested",
  "summary": "一段话总结",
  "reviewed_head": "4f1c2ab",
  "findings": [
    {
      "id": "F1",
      "severity": "high",
      "category": "correctness",
      "location": {"path": "src/ledger.py", "line": 120},
      "claim": "分卷封存后未清除 pending 标记，重启时会重复封存。",
      "evidence": "ledger.py:118-124 先写 manifest 后清 pending，二者之间崩溃即重入。",
      "suggestion": "调整提交顺序或在恢复时按卷字节比对去重。",
      "acceptance_item": "验收第 3 条：中断后可恢复且不重复"
    }
  ],
  "unverified": ["未能运行集成测试：缺少云端测试凭据"]
}
```

| 字段 | 约束 |
| --- | --- |
| `verdict` | `pass` / `changes_requested` / `blocked`（无法评审，例如缺材料） |
| `severity` | `critical` / `high` / `medium` / `low` / `info` |
| `category` | `correctness` `security` `requirement` `test` `docs` `maintainability` `performance` |
| `evidence` | 必须指向可核实的位置或命令输出；没有证据的推测只能是 `info` |
| `acceptance_item` | 能对应到验收要求时填写，便于主控逐项核对 |

## 5. worker 处置规范

worker 不盲信评审意见，处置时先核实：

1. 评论的前提是否符合受支持的契约；
2. 实际入口与触发条件是否可达；
3. 影响是什么；
4. 哪些是事实证据，哪些是假设。

在此基础上权衡最小修复与复杂度、I/O 和维护成本。可以有据拒绝，但仅靠人工构造的测试或理论交错，不能当作生产问题；没有现网复现，也不能否定已证明可达的问题。

处置记录 `review/rK-decision.json`：

```json
{
  "round": 1,
  "cycle": 1,
  "reviewed_head": "4f1c2ab",
  "items": [
    {"finding": "F1", "action": "fix", "commit": "9a0d3e1", "verification": "test_ledger_recover_after_seal 通过"},
    {"finding": "F2", "action": "reject", "reason": "该入口仅在 --debug 下可达，生产配置禁止；见 config.py:44"},
    {"finding": "F3", "action": "defer", "reason": "性能优化，超出本任务验收范围", "follow_up": "建议新建任务"}
  ],
  "decision": "iterate",
  "next_head": "9a0d3e1"
}
```

决策规则：

| 情况 | 决定 |
| --- | --- |
| `verdict=pass`，或每个阻断项都已按 G-SUBMIT 处置（修复并验证、有据拒绝、主会话认定不成立、获批延后） | `finalize` |
| 有阻断项需要修复，本周期轮次 < 本周期有效预算 | `iterate` |
| 有阻断项需要修复，本提交周期预算已用完 | `escalate`：提交 `review.budget_exhausted`，列出未解决项、各轮记录与 worker 的建议，在本会话中问用户（超时转交主会话） |
| 阻断项需要延后（超出本任务范围或依赖外部） | `escalate`：提交 `review.defer_requested`，由主会话批准延后（列入已知问题）、要求继续修复或交给人类 |
| `verdict=blocked` 因缺材料 | 该轮不算有效评审（不满足 G-SUBMIT，也不能确认修复）；补齐材料后经 E06 以新的一轮重跑（计入本周期预算）；缺的材料自己拿不到时在本会话中问用户（`ask_user`） |
| 非阻断项 | worker 自行决定修复或 defer，不影响 finalize |

### 5.1 预算用完后由用户（或转交后的主会话）决定

worker 提交 `review.budget_exhausted` 时附上：未解决的阻断项与各轮处置记录、每轮评审 HEAD、worker 认为最合理的下一步，并在本会话中把这些写给用户。用户（或超时转交后的主会话，按 DN006 §8.2 的方式）常见的决定：

| 情况 | 典型决定 |
| --- | --- |
| 剩余问题清楚、每轮都在收敛 | 追加 1–2 轮（`review.budget_extended`） |
| 评审器反复提同一条意见，worker 的反驳有据 | 核实后认定意见不成立（`review.finding_overruled`），理由写入报告 |
| 问题真实但超出本任务范围 | 批准延后（`review.defer_approved`），列入已知问题，并建议新建任务 |
| 修复方向错误、来回修改不收敛 | 给出新的修复思路或收窄范围（`orchestrator.feedback`） |
| 需要改变验收标准、扩大范围，或主会话没有把握 | 转交后的主会话不代答，转达给用户或继续等待 |

主会话或用户的决定由 tsw 记入事件，并在 worker 的当前处置记录（`review/rK-decision.json`）中引用对应的决定 ID：追加轮次后 worker 继续 `iterate`；认定不成立或批准延后后，对应阻断项的处置记为 `overruled` / `deferred_approved`，worker 可以 `finalize`。

同一提交周期内主会话累计追加的轮次不超过 `review.orchestrator_extension_limit`（默认 3）；再需要追加只能交给人类。主会话的每个决定都记入 `DecisionRecord`，出现在主控验收报告与决定摘要中。

## 6. 主会话的复核

主会话在 G-ACCEPT 中复核评审，但不重新评审：

- 每轮请求、结果、处置文件齐全，且 `reviewed_head` 与提交历史一致；
- 最后一轮的 HEAD 等于提交 HEAD；
- 每条 `reject` / `defer` 的理由是否站得住，以及本任务中主会话自己作出的评审相关决定（追加轮次、认定不成立、批准延后）在验收时是否仍然成立。理由不足时走 T17，给出具体剩余工作；
- 评审覆盖了任务类型要求的范围（代码含测试与 diff，研究含结论与证据）。

主会话认为需要独立的二次审查时，可以要求 worker 用 `fresh` 线程再跑一轮全量评审，结果同样记入证据。

## 7. 报告中的呈现

worker 的 `result.md` / `result.html` 增加“自评审”一节：

- 轮次数和每轮的 verdict；
- 阻断项的处置一览（fix / reject / defer 及理由）；
- 最终评审 HEAD；
- 未能验证的事项。

只引用评审文件，不粘贴完整原文。

## 8. 成本与时间

- 每轮评审是一次 `medium` 推理的一次性线程。增量轮次复用线程，只审 diff，成本通常明显低于首轮。
- 每个提交周期最坏情况的调用次数为“本周期有效预算 × (`retry_limit` + 1)”。默认配置下，不经任何追加时为 3 轮 × 最多 3 次尝试；主会话追加受 `orchestrator_extension_limit` 限制，用户追加由用户逐次批准。主控退回（T17）或需求变更会开启新周期，轮次编号继续递增。
- 评审等待期间 worker 子会话保持工作（Stop hook 提示不要结束 turn），不会触发主会话唤醒。
