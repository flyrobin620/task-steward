# CC007 Claude Code 中的任务上下文：任务结束时搜集偏好候选

- 状态：设计草案（T288）
- 日期：2026-11-16
- 实现的设计：[DN007 §6.3 任务结束时搜集偏好候选](../DN007-ledger-and-context.md)
- 相关：[CC006 Claude Code 插件](CC006-plugins.md) · [CC001 会话与 hooks](CC001-sessions-and-hooks.md)

## 1. 范围

DN007 §6.3 规定任务结束时由子代理搜集偏好候选、经用户确认后记录。本文写这件事在 Claude Code（Claude 桌面 app 的 Code 标签）中的做法：子代理怎样启动、说明文字放在哪里、worker 与主会话各自的步骤、用户怎样确认。

## 2. 子代理

- **启动方式**：会话用宿主的 `Agent` 工具启动一个子代理：`subagent_type: "general-purpose"`，`description` 为“搜集偏好候选”，`prompt` 原样使用 `preference_sources` 返回的 `prompt`，不自己拼接或删改。子代理有独立的上下文，结束时只把结果交回启动它的会话。
- **为什么是 general-purpose**：它继承会话的模型。判断一句中文是不是“以后都适用”需要理解措辞与上下文，用更小的模型容易把本任务的决定当成偏好，或漏掉不带“以后”字样的长期意图。
- **只读**：提示语要求子代理只读材料与报告草稿、不调用任何写工具（tsw 的写工具、编辑文件、git、gh），只返回一个 JSON 对象。即使它违反要求调用了 tsw 写工具，MCP server 也按会话解析为启动它的 worker 或主会话，只能做那个会话本来就能做的事；它不能把候选变成偏好（`preference_record` 只给主会话与用户本人，且须附用户原话）。
- **说明文字只有一份来源**：产品仓库的 `tsw/host/preference_scan.md`，由 `preference_sources` 在每次调用时读取并放在 `prompt` 开头，后面是本任务的材料、现行偏好与已有候选（JSON）。说明随产品拉取立即生效，不需要 `tsw host render`、插件更新或重启会话（MCP server 在调用时读取文件）。
- **输出**：一个 JSON 对象 `{"candidates": [...], "skipped": [...]}`。每个候选含 `quote`、`source_ref`、`scope`、`topic`、`reason`，可选 `supersedes`；`skipped` 列出看过但不提的（与现行偏好相同、已是候选、只对本任务等），每项写 `source_ref` 与理由。子代理没有找到候选时返回空列表。

考虑过、没有采用的做法：在插件中定义专用的子代理（`agents/` 下限定工具为只读）。它能在宿主层面限制工具，但说明文字随插件副本分发，每次修改都要更新插件、重启会话才生效（CC006 §11.1），与“偏好相关的说明随产品立即生效”相悖；子代理即使误调用写工具也受 tsw 的角色与来源校验约束，收益不足以抵消分发成本。

## 3. worker 的步骤

在 `plugin/roles/worker.md` 的“需要记住的决定”一条中（替换原来“写进报告的‘决定与约定’、主会话验收时转记”）：

1. 自评审定稿、报告草稿写好之后，`submit` 之前：调用 `preference_sources`（`report_path` 为报告草稿的路径）。
2. 用 `Agent` 工具按 §2 启动子代理，`prompt` 用返回的 `prompt`。
3. 拿到结果后调用 `preference_candidates_record`：`scanned_through` 用 `preference_sources` 返回的值，`candidates` 用子代理的候选。worker 可以去掉明显不成立的候选（例如子代理把 worker 自己的话当成了用户的话），不能新增或改写原话；tsw 会核对每条原话确实出现在所引用的材料中。没有候选时也调用一次（`candidates` 为空），表示搜集过。
4. 报告“决定与约定”一节列出本任务的候选编号（`PC-…`）与原话，注明“待用户在主会话确认”；不再自己判断哪些要记为偏好，也不写“建议记为偏好”之外的结论。
5. 提交之后用户又在本会话中说了以后都适用的话（例如需求变更后重新提交），重新提交前再做一次 1–3。

worker 不问用户是否记为偏好：确认在主会话中进行（DN007 §6.3）。

## 4. 主会话的步骤

在 `tsw/host/orchestrate_skill.md` 的“核验与验收”中（替换原来“报告‘决定与约定’一节中用户明确说过以后也适用的约定，向用户确认后用 `preference_record` 记录”）：

1. 核验 worker 的提交时看 `task_read` 的 `preference_scan`：`missing` 或 `stale` 时，在 `task_accept` 之前自己按 §3 的 1–3 补做（`task_id` 为该任务；报告路径为提交中的报告）。
2. `task_accept` 之后，与 follow-up 一起把本任务未决的候选交给用户：每条写编号、原话、来源编号、建议的范围与主题；有替代建议的写出被替代偏好的编号与原文。问用户每条是“记录”“改范围或说法后记录”还是“不记”。
3. 用户答复后：
   - 记录：`preference_record`，`candidate` 为候选编号，`scope` / `topic` 按用户的意思（没说就用建议值），`supersedes` 只在用户同意替代时给出；`user_source` 是用户这次答复的原话，`target` 为 `["preference", <scope>, "standing"]`。用户改了说法时，`text` 写用户的新说法（必须原样出现在这次答复中）；否则不写 `text`，用候选原话。
   - 不记：`preference_candidate_dismiss`，`user_source` 是用户的原话（`target` 为 `["preference", <候选编号>, "dismiss"]`），`reason` 写用户的理由或“用户不记”。
   - 用户暂不答复：候选留在 `attention` 的 `preference_candidates` 中，下次用户在主会话时再问；不要代用户决定，也不要因为没有答复就拒绝。
4. 用户在一条答复中处理多条候选时，每条分别调用，引用同一段原话。

主会话中用户直接说出的长期偏好仍按“配置与维护”当场 `preference_record`，不经候选。

## 5. 用户在终端处理

用户可以不经主会话，在 Claude 之外的终端运行（与 `tsw preference record / revoke` 一样，在宿主会话中运行会被拒绝，DN006 §8.10）：

- `tsw preference list --candidates [--all]`：列出未决的候选（`--all` 包括已记录与已拒绝的）；
- `tsw preference record --candidate <PC-n> [--scope …] [--topic …] [--text …] [--supersedes …]`：确认一条候选，缺省用候选的原话、建议的范围与主题；
- `tsw preference dismiss <PC-n> --reason <理由>`：拒绝一条候选。

## 6. 待验证

- 子代理在真实任务中提出的候选是否准确（不把本任务的决定当成偏好、不漏掉明确的长期意图）：本任务的单元、组件与 surface 测试只覆盖材料、记录、核对与确认的流程，不调用模型；子代理判断的质量要在之后的真实任务中观察，必要时调整 `preference_scan.md`。
