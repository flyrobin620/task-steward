# DN004 worker 自评审：评审与迭代决策

- 状态：设计草案（T233 r5，已按 PoC 结果修订；T325 修订评审器实现与可关闭的自评审；T331 Claude 评审器改为由 tsw 启动的 `claude-headless`，删除子代理方式；T330 §3.4 评审模型的列表与选择，待用户确认）
- 日期：2026-11-05
- 相关：[DN002 任务状态机 §3.3](DN002-task-state-machine.md) · [DN003 统一配置](DN003-unified-config.md) · 实现：[CX004 Codex 评审器](codex/CX004-reviewer.md) · [CC004 Claude Code 评审器](claude-code/CC004-reviewer.md) · [CP004 Copilot CLI 评审器](copilot/CP004-reviewer.md)

## 1. 目标与变化

worker 完成任务成果后，**由 worker 自己**启动配置的只读评审器（默认 Codex `gpt-6-astra`，推理强度 `medium`，只读沙箱）评审，逐条处置意见，并自行决定迭代还是定稿。定稿后提交给主会话验收，最后交用户人工验收。

与 orchestrator-agent 相比：

| 项 | orchestrator-agent | 本设计 |
| --- | --- | --- |
| Astra 对照版（另一模型独立出一版成品再比较） | 默认用于面向用户的图、报告正文和 HTML | **删除** |
| 代码审查 reviewer | worker 或主控按需启动 | 默认所有任务类型**必经**的自评审环节；只有用户能在配置中全局或按仓库关闭（§9），主会话与 worker 都不能为某个任务跳过 |
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
   - 增量轮次另含上轮意见、worker 的处置，以及上轮 HEAD..新 HEAD 的 diff；
   - 评审器不能运行命令时（§3.1），本轮范围的完整 diff 写入 `review/rK-diff.patch`，请求中给出路径。
3. **调用评审器**（§3）：tsw 启动一次性只读评审进程或线程，等待完成并读回最终消息。
4. **结构化结果**：解析为 `review/rK.json`；不满足 schema 时视为该轮失败（E02 重试）。
5. **逐条处置**（`review_triage`）：阻断项（`review.block_on` 中的级别）只能标为 `fix` 或 `reject`；非阻断项还可以标为 `defer`。`reject` 和 `defer` 必须写理由。阻断项确需延后时，worker 不能自行 defer，而是提交 `review.defer_requested` 交主会话判断。
6. **决定**（`review_decide`）：
   - `iterate`：存在需修复的阻断项，且本周期轮次 < 本周期有效预算（`max_rounds` + 主会话追加 + 用户追加，见 DN002 §3.3）。修改并提交新 HEAD 后进入下一轮增量评审。
   - `finalize`：每个阻断项都已按 G-SUBMIT 处置：`fix` 且已验证、有据 `reject`、主会话认定不成立（`review.finding_overruled`），或经主会话 / 用户批准延后（`review.defer_approved`，列入报告“已知问题”）。
   - `escalate`：本提交周期预算用完仍有阻断项（`review.budget_exhausted`）、阻断项需要延后（`review.defer_requested`），或评审器不可用（`review.unavailable`）。worker 随即在自己的会话中问用户（`ask_user`，DN006 §8.1a），由用户决定追加轮次、认定意见不成立、批准延后或调整方向；任务进入 `waiting_user`。超时无回复时转交主会话，主会话可在授权边界内代为决定（DN006 §8.1b）。
7. **终版一致性**：`finalize` 时如果当前 HEAD 晚于最后评审的 HEAD（例如定稿后改了报告链接），必须对差异再做一轮增量评审；这一轮同样计入本周期预算，预算耗尽时 `escalate`。
8. **提交**：调用 `submit`，tsw 执行 G-SUBMIT（DN002 §4）。

## 3. 评审器调用

评审器不是单独的插件类型，而是由 `roles.reviewer.session` 选用一个能做评审的会话适配器（`oneshot`、`direct`、`read_only_enforced`，DN006 §7.4），可以与主会话、worker 使用的适配器不同。评审包渲染、输出 schema 校验、轮次计数与增量范围属于 tsw 核心。

**评审器由 tsw 启动，结果由 tsw 读回并记账，不经过 worker**：`review_run` 启动评审进程，等它结束并读回最终消息，一次调用完成一轮；worker 只看到 tsw 记下的结果。用户（T331，2026-11-21）：“评审器由 tsw 自己启动并读回结果，然后记账，结果不经过 worker 的手。>> 核心点在这里。reviewer是由tsw启动的。”因此不提供由 worker 启动评审器、再把结果交回的方式（T325 的 `claude-subagent` 已删除，附录 A）。worker 仍能影响评审的输入（范围起点、`notes`、代码中的注释），评审器从请求与它自己读到的文件判断。

### 3.1 评审器实现

| 会话适配器 | 方式 | 只读的强制方式 | 增量轮次 | 实现文档 |
| --- | --- | --- | --- | --- |
| `codex-app-server`（默认） | 同步：Codex App Server 一次性线程 | `sandbox=read-only` | 续用线程，只审增量 | [CX004 §2](codex/CX004-reviewer.md) |
| `codex-exec`（回退，尚未实现） | 同步：`codex exec` | `--sandbox read-only` | 每轮全量并附上轮意见 | [CX004 §3](codex/CX004-reviewer.md) |
| `claude-headless` | 同步：`claude -p` 非交互 | 只开放 `Read`、`Grep`、`Glob`，`dontAsk`，不加载插件、hooks 与 MCP | 续用会话（`--resume`），只审增量 | [CC004](claude-code/CC004-reviewer.md) |
| `copilot-cli` | 同步：`copilot -p` 非交互 | 只开放 `view`、`grep`、`glob`，拒绝 `write`、`shell`、`memory`、`url` | 续用会话（`--resume`），只审增量 | [CP004](copilot/CP004-reviewer.md) |

所有实现共同的规则：

- 使用配置指定的模型与 effort。模型不可用、effort 不支持，或评审器实际使用的模型与请求不一致（包括宿主或管理策略换了模型）时判为本轮失败，**不回退到其他模型**。
- 默认评审器与回退都失败时记录 `review.unavailable` 交主会话判断（可稍后重试或交给人类），**不能跳过评审**（自评审关闭的情形见 §9），**也不能改用配置以外的评审器**：worker 自己启动的子代理不是评审器，它的结论不能代替一轮评审。
- 不能运行命令的评审器（`claude-headless`、`copilot-cli`）由 tsw 把本轮范围的完整 diff 写入 `review/rK-diff.patch`（续用线程的增量轮次只写增量；不续用时在后续轮次另有上一轮 HEAD 以来的 `rK-diff-since.patch`），请求中给出路径。评审器只读查阅执行目录、报告草稿与评审包，不运行测试；测试与验证结果以 worker 在 `notes` 中写明的为准，评审器把无法核实的事项列入 `unverified`。

### 3.2 v1 实现范围

- `review_run`（MCP，worker）与 `tsw review run`（命令行）是同一实现。开轮之前先检查能检查的：调用者是 live 且目录一致的 worker；自评审未关闭（§9）；同步评审器的命令存在；Git 执行目录中 `head`、`base` 必须是真实提交（只把解析出的提交 ID 交给 git，选项形式的输入直接拒绝）；没有给 `base` 时取 `repositories.<repo>.base_ref`、`origin/HEAD`、`main` 等与 HEAD 的分叉点，取不到就要求显式指定。同一 attempt 同时只运行一轮（`locks/` 中的文件锁）。
- 开轮（`review_start`）后渲染 `runs/Txxx/aN/review/rK-request.md`（需要时连同 `rK-diff.patch`），调用评审器，结果写入 `rK.json`、调用信息写入 `rK-run.json`（范围、线程或会话 ID、请求与实际的模型、effort、是否续用），然后记录 `review_complete`。评审器报错、启动失败、超时、输出不是 JSON、不满足契约，或 `reviewed_head` 与本轮 HEAD 不一致（Git 缩写须解析为同一提交，非 Git 成果标识须完全相同）时，保存原始输出 `rK-raw.txt` 并记录 `review_fail`；开轮后的任何异常都以失败收尾。任务处于 `reviewing`（E02）时再次调用即在同一轮、按落账的 `head` 与 `base` 重试；落账的范围同样要是执行目录中的完整提交（轮次可能是直接用 `review_start` 开的），否则不调用评审器、记录失败。
- 每轮请求都带当前 revision 的目标与验收原文；需求变更（E07）开启新周期后不续用旧线程，按新需求完整评审。
- 正在进行的一轮（`reviewing`）由开轮时的评审器完成：重试沿用开轮时记录的评审会话适配器与模型，即使配置已经改了；新的一轮才用新的配置。例外：开轮时的评审器在本版本已删除（T325 版本开启、仍在等子代理结果的 `claude-subagent` 轮次）时，重试改用当前配置的评审器与模型，`rK-run.json` 记下实际使用的评审器。新一轮开始前先检查自评审是否关闭（§9），关闭时不再检查评审器。
- 默认评审器 `codex-app-server` 的进程与线程管理见 [CX004 §4](codex/CX004-reviewer.md)，`claude-headless` 见 [CC004](claude-code/CC004-reviewer.md)，`copilot-cli` 见 [CP004](copilot/CP004-reviewer.md)。
- `codex-exec` 尚未实现：配置为评审会话时 `review_run` 返回 `UNSUPPORTED`，不落账新的一轮。
- `claude-subagent` 已删除（T331）：配置中出现它时配置校验失败，错误信息给出改法（`roles.reviewer.session` 与 `plugins.sessions` 中改为 `claude-headless`，模型可沿用别名）；`review_run` 不再接受交回的结果（`round`、`result`、`error`、`agent_id` 参数与对应的命令行选项删除，[决定 0004](../decisions/0004-remove-claude-subagent-reviewer.md)）。

### 3.3 只读与隔离

- reviewer 只读，不能修改 worktree，不能 push，不能访问凭据目录。只读由会话适配器用宿主机制强制（沙箱、工具白名单），不靠提示。
- 评审包和结果由 tsw 写入该 attempt 的 `runs/Txxx/aN/review/`（worker 不直接写 `runs/`，见 DN006 §9.4）。
- 一次性线程与评审进程不进入调度、不绑定任务、不占用 `max_workers` 名额。每个 attempt 的 reviewer 线程只服务于该 attempt，不跨任务复用。
- 独立性：评审器有自己的上下文，只看到评审请求与它自己读到的文件，不继承 worker 的对话。与 worker 同一宿主的评审器（`claude-headless`）应选与 worker 不同的模型（相同时配置校验警告）。

### 3.4 评审模型的选择（T330）

用户 2026-11-21（主会话，针对 F-T325-2）：“评审模型应该是一个config（不止针对copilot-cli reviewer），安装时用户可以选择（更新安装体验）。更换时用户也可以选择（cli + chat）。应当使用合理的model list方法给一个list让用户选择。”

**配置**：`roles.reviewer.model` 与 `roles.reviewer.effort`（DN003 §3.1）对所有评审器通用，是本节唯一的配置，不新增键。tsw 不再为某个评审器写死模型：安装与更换时都从所选评审器在本机实际可用的模型中选。代码中的默认值（`codex-app-server` 为 `gpt-6-astra` / `medium`，`claude-headless` 为 `claude-sonnet-5-5` / `high`，`copilot-cli` 为 `gpt-6.1-sol` / `medium`）只在两处起作用：列表中有它时作为预选项，以及取不到列表时的建议值。

**取得模型列表**：tsw 对一种评审器启动它的命令行（可执行文件按 `roles.reviewer.executable`，进程树与评审相同，超时 60 秒），用宿主公开的列模型方法读回列表。只做握手与查询，**不调用模型、不计费**。每种评审器的方法与实测见实现文档：

| 评审器 | 方法 | 推理强度从哪里来 | 不列出的项 | 实现文档 |
| --- | --- | --- | --- | --- |
| `codex-app-server` | `codex app-server` 的 JSON-RPC `model/list`（按 `nextCursor` 分页） | 每个模型的 `supportedReasoningEfforts` 与 `defaultReasoningEffort` | `hidden` 的模型 | [CX004 §5](codex/CX004-reviewer.md) |
| `copilot-cli` | `copilot --acp`（Agent Client Protocol）的 `initialize` 与 `session/new`，读 `models.availableModels` | `configOptions` 中 `reasoning_effort` 的选项（会话级，不按模型区分） | `auto`：每次由 Copilot 挑模型，与“实际模型须与配置一致”（§3.1）矛盾 | [CP004 §8](copilot/CP004-reviewer.md) |
| `claude-headless` | `claude -p --input-format stream-json` 的控制请求 `initialize`（Agent SDK 的 `supportedModels()` 用的同一请求），读 `models`；隔离选项与环境变量与评审相同，所以用户设置中的网关决定列表 | 每个模型的 `supportedEffortLevels` | `default`：随用户的默认模型变化，评审总是显式传模型（CC004 §3） | [CC004 §8](claude-code/CC004-reviewer.md) |

统一的结果：每个模型的 `id`（写进 `roles.reviewer.model` 的值，就是命令行 `--model` 接受的值）、名称、说明、支持的推理强度与默认推理强度，以及宿主自己的默认模型。列表只给出能写进配置的选项：推理强度只保留配置接受的 `low` 到 `max`（Codex 有的模型多一档 `ultra`，不列出）；`claude-headless` 只列 `claude-…` 模型与别名（网关自己的模型名无法按 CC004 §4 核实）。

**取不到列表时**（命令行不存在、未登录、超时、输出不是预期的协议）：不阻断安装与更换，给出原因。用户可以自己输入模型 ID（安装时建议值为现有配置或代码默认值），它标为“未核实”：第一轮评审按 §3.1 核实，模型不可用即判本轮失败，不回退。取到了列表而用户给的模型不在其中时拒绝（拼错，或宿主的策略不允许），除非用户明确要求写入列表外的模型（命令行 `--unlisted`）：宿主的列表可能不全（例如 Copilot 的管理策略只在调用时生效，CP004 §8）。

**推理强度**：只能选所选模型支持的强度。预选顺序：现有配置（模型支持时）、代码默认值（模型支持时）、模型自己的默认强度。

**安装**（DN009 §6）：评审器之后再问两项，选 `none`（不做自评审）时不问：

1. 评审模型：从所选评审器的列表中选；预选现有配置或代码默认值（在列表中时），否则宿主自己的默认模型。
2. 推理强度：从所选模型支持的强度中选。

终端入口用方向键选择（DN009 §16）；会话入口由 setup skill 在用户回答评审器之后运行 `tsw setup models --reviewer <评审器> --json` 取列表，再在对话中问。答案 `reviewer_model`、`reviewer_effort` 写到 `roles.reviewer.model` / `effort`（`codex-app-server` 也写入，显式记下用户的选择）。答案文件中没有这两项（早先版本保存的答案、非交互的 `install --answers`）时不取列表，用代码默认值，`check` 与 `plan` 给出警告（模型没有对照本机列表核实，安装后可以用下面的方式更换）。已有数据目录时安装不改配置（DN009 §5 第 3 步），只报告差异；改用下面的更换方式。

**更换：命令行**（用户本人在会话外的终端，DN006 §8.10）：

- `tsw reviewer models [--reviewer <评审器>] [--json]`：只读，列出模型与支持的推理强度，标出当前配置；缺省为当前配置的评审器。
- `tsw reviewer set [--reviewer <评审器>] [--model <模型>] [--effort <强度>] [--unlisted] [--yes]`：没有给出的项在终端中用方向键选择（非交互运行时必须给全）；显示改动前后的值，确认后写入 `roles.reviewer.session` / `model` / `effort`（换评审器时 `fallback_session` 置为 `null`）。写入与 `config_change` 是同一实现：完整校验、`config.changed` 事件、原子替换文件（DN003 §7.1）。写入绑定到开始选择时读到的配置文件：选择期间配置被改过时拒绝写入，请用户重新运行；结果读回写入后的配置。换评审器时 `roles.reviewer.executable`（旧评审器的命令行）一并删除；它写在本机覆盖中时（tsw 不改本机覆盖，DN003 §5）拒绝更换，请用户先改该文件。`reviewer_models` 给出的 `ops` 示例同样处理。`roles.reviewer` 是权限类键：用户本人在终端运行这条命令就是授权，来源由 tsw 记为渠道 `user-cli`（`text` 为命令行本身），与 `tsw task accept` 相同；在任何宿主会话中运行都拒绝。不需要调度租约。

**更换：主会话 chat**：

- 只读 MCP 工具 `reviewer_models`（主会话）：输入可选的 `reviewer`，返回同样的列表、当前配置、配置文件摘要，以及写入用的 `ops` 模板。
- 用户在主会话中要求更换评审器、模型或推理强度时，主会话调用它，把选项交给用户选（Claude app 中用 `wrap_up` 的按钮，选项多时列出编号），不替用户选；再以用户选择时的原话为 `user_source` 调用 `config_change`（权限类键，DN003 §7.2）。orchestrate skill 写明这一做法。

**生效**：与其他配置相同，只影响之后开始的评审轮次；正在进行的一轮沿用开轮时的评审器与模型（§3.2）。

## 4. 输出契约

`config/schemas/review-result.schema.json` 约定评审器必须只输出如下 JSON：

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

## 9. 关闭自评审

用户可以选择不做自评审（T325；用户原话见 T308 会话 M-T308-9：“提供一个不自评审的选项”）。这改变了此前“评审必经、不可由配置关闭”的决定（DN002 G-SUBMIT、DN003 §3.1），所以只能由用户以配置做出：

- **配置**：`review.self_review`（`required` 默认 / `off`）对所有任务生效；`repositories.<id>.self_review` 对该仓库的任务覆盖全局值。两者都是权限类键（DN003 §7.2），改动要附用户原话。主会话与 worker 都不能为某个任务临时关闭自评审。
- **取值时机**：按任务所属仓库，在每次 `review_run`、`review_start` 与 `submit` 时读取当时的配置。任务书写明“自评审：关闭（来源键）”或“自评审：开启”。
- **关闭时**：`review_run` 与 `review_start` 以 `self_review_off` 拒绝；worker 在 `executing`（或关闭前已进入的 `triaging`、`revising`）中直接 `submit`（T13）。G-SUBMIT 不检查评审条件（评审轮次、最后评审 HEAD、`finalize`、阻断项处置），其他条件照常：报告 md 与 html、合并与主分支 CI、live 说明、工作树。`worker.submitted` 记录 `review: {"mode": "off", "source": <生效的键>}`。
- **关闭前已有的轮次**：照常留在证据中、写进报告，但不再约束提交；正在进行的一轮（`reviewing`）要先以完成或失败结束。
- **重新开启**：改回 `required` 后，尚未提交的任务按评审必经的规则提交（本周期没有有效评审时先 `review_run`）；已提交的任务不受影响。
- **报告与验收**：报告的“自评审”一节写“未做（配置关闭，来源键）”。主会话在 G-ACCEPT 中核对提交记录的模式与来源，不要求评审证据。
- **仓库自己的规则不变**：仓库要求的评审（例如 PR 须经独立评审才能合并）不因关闭 tsw 自评审而失效，worker 照仓库规则执行，tsw 不替仓库判断。
- **安装**：`tsw setup` 的评审器问题另有一个选项 `none`（不做自评审），选择它即写入 `review.self_review: "off"`（DN009 §6）。

## 附录 A：备选方案存档

| 事项 | 采用 | 备选与不采用的原因 |
| --- | --- | --- |
| 没有 Codex 时的 Claude 评审器（§3.1，T331） | `claude-headless`：tsw 以 `claude -p` 启动只读进程，读回结果并记账（[CC004](claude-code/CC004-reviewer.md)） | `claude-subagent`（T325 实现，T331 删除，存档于 [CC004 附录 A](claude-code/CC004-reviewer.md)）：worker 用 Agent 工具启动插件提供的只读子代理，再把子代理的最终消息交回 `review_run`。当时用户在 T308 会话中选择子代理（M-T308-10：“claude 自己的reviewer用subagent做就可以了，不用专门用claude headless或者cli”）。删除的原因：结果经 worker 转交，worker 可以改写或伪造；tsw 也拿不到子代理实际使用的模型。用户 2026-11-21：“评审器由 tsw 自己启动并读回结果，然后记账，结果不经过 worker 的手。>> 核心点在这里。reviewer是由tsw启动的。所以codex和copilot-cli就都是对的。所以正确的改法其实是换回claude-headless， 由tsw启动，就行了？”“F-T325-3 转任务，删掉 claude-subagent” |
| 子代理结果的取回（T325，随 `claude-subagent` 删除） | worker 把子代理最终消息原文交回 `review_run` | 插件的 SubagentStop hook 从子代理记录中直接取结果：要解析宿主内部的子代理记录格式。两者都随子代理方式一起不再适用 |
| 评审模型列表的来源（§3.4，T330） | 各宿主公开的列模型方法：Codex `model/list`、Copilot ACP `session/new`、Claude 控制请求 `initialize`；都不调用模型 | 代码中的固定清单：宿主更新与组织策略会让它过时（F-T325-2 的起因）；`copilot help config` 列出的模型：是 CLI 内置的清单，不反映本机账号可用的模型；Copilot 不存在模型时的报错：只说“不可用”，不列出可用模型（T330 实测）；Anthropic `GET /v1/models`：需要 API 密钥，不走用户设置中的网关，与 `claude -p` 实际可用的模型不一致；每次加载配置或每轮评审前核对列表：要启动宿主命令行，太慢，模型不可用时评审本来就会失败 |
| 命令行更换评审模型的授权（§3.4，T330） | `tsw reviewer set` 记为 `user-cli` 渠道的用户本人操作（与 `tsw task accept` 相同） | 只在主会话中更换：用户要求“cli + chat”都能换；`tsw tool run config_change --role orchestrator` 加手写的 `user_source`：要持有调度租约，并要用户手写来源 JSON |
| 关闭自评审的粒度（§9，T325） | 全局与按仓库的配置，权限类键 | 按任务（主会话受理时指定）：主会话能自行关闭评审，越过用户的授权；“可选评审”（不要求评审，但做了的轮次仍约束提交）：规则更复杂，用户只要求了不做自评审的选项 |
