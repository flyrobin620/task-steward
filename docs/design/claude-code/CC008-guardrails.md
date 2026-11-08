# CC008 Claude Code 上的护栏

- 状态：设计草案（T242 r2，待用户确认）
- 日期：2026-11-08
- 实现：[DN008 校验原则](../DN008-guardrails.md)
- 相关：[CC001 Claude Code 上的会话与 hooks](CC001-sessions-and-hooks.md) · [CC006 Claude Code 插件](CC006-plugins.md)

## 1. 目标

按 DN008 在 Claude Code（桌面 app 与命令行）上实现护栏，**尽可能薄**：一般风险交给 Claude Code 自己的权限模式与 auto 分类器；tsw 只加宿主不知道的、确定性的 block list 规则。规则越少，规则本身出错的机会越少。

“现状”指 `main` 与 T241 分支（`tools/live.py`、host-e2e，未合并）上的实现。

## 2. 分工

| 层 | 判断什么 | 是否 LLM | 在 Claude Code 中 |
| --- | --- | --- | --- |
| Claude Code 权限模式与 auto 分类器 | 一般风险：删除、写到意外的位置、危险命令；一次调用是否出自用户意图 | 是（宿主的保护，不是 tsw 的护栏，DN008 §4 第 3 条） | 会话的权限模式；worker 建议使用 `auto`（`roles.worker.permission_mode`） |
| tsw 生成的允许规则 | 把“授权记录在 tsw 中、由 tsw 把关”的动作从分类器的判断中移出 | 否 | `permissions.allow`（§6） |
| tsw hooks 的 block list | tsw 特有的已知错误（§3） | 否 | `PreToolUse` 调用 `tsw guard` |
| tsw MCP 与状态机 | 角色、任务、租约、版本、G-*、用户来源（DN008 §6） | 否 | MCP server 按角色列出工具；`run_tool` 的检查与各工具的守卫 |
| 事后发现 | 漏网的偏差（不保证发现） | 部分 | 流程（确定性）：`ledger audit` 的摘要链、G-SUBMIT / G-ACCEPT 对提交、登记与处置记录的检查。内容（LLM）：主会话验收时判断 diff、PR 与合并后的 CI，不是护栏 |

权限模式为 `bypassPermissions` 时没有分类器，一般风险没有宿主保护；这是用户自己的选择，tsw 不补上。

## 3. block list

所有规则只在已登记的会话（主会话、worker、已撤销、待绑定）中生效，无关会话不受影响。规则只看工具名、目标路径前缀、命令中的程序与子命令（分词，不解析路径、重定向、变量与脚本）。

| 编号 | 会话 | 触发 | 防的错误 | 拒绝信息中的正确做法 |
| --- | --- | --- | --- | --- |
| CC-B1 | 所有 | 编辑类工具（Edit / Write / MultiEdit / NotebookEdit）的目标在数据目录的 `ledger/` `runs/` `todo/` `config/` `outbox/` `legacy/` `views/` `cache/` `locks/` 中 | 手工“修”账本、把报告直接写进 `runs/`、编辑配置文件（走错步骤） | 对应的 tsw 工具：报告写在 `work/` 由 `submit` 导入；配置用 `config_change` |
| CC-B2 | 所有 | 编辑类工具的目标是数据目录（含其下执行目录）中的 `.claude/settings*.json` | 遇到权限拒绝后自行添加允许规则，越过用户选定的权限；改动被 tsw 重新生成覆盖 | 把权限拒绝告诉用户，由用户决定 |
| CC-B3 | worker | 编辑类工具的目标在本任务仓库的主检出（`repositories.<repo>.path`）中，而执行目录不在其中 | 写进主仓库检出而不是任务 worktree（对象错误） | 在执行目录（任务 worktree）中修改 |
| CC-B4 | worker | `git … push`，仓库没有 `delivery.push` | 未授权的远端动作 | 不推送；提交时说明 |
| CC-B5 | worker | `gh pr create`，仓库没有 `delivery.create_pr` | 同上 | 同上 |
| CC-B6 | worker | `gh pr merge`，或 `gh api` 且参数含 `merge`；任务不是开发任务或仓库没有 `delivery.merge` | 不该由 worker 合并时合并，主会话流程随之错乱 | 提交时用 `merge_skipped` 说明，由主会话合并。**不可放行**：它是 G-SUBMIT 合并授权的镜像，放行后合并记录仍会被 G-SUBMIT 拒绝；需要 worker 合并时由用户修改 `delivery.merge`（用户在会话外手工编辑，或经主会话 `config_change`），然后在原 worker 中重试：hook 与 G-SUBMIT 每次都重新读取配置，不需要重启会话；任务须为开发任务。会话创建时没有合并授权，worker 的 `settings.local.json` 中就没有 `gh pr merge` 的允许规则，auto 分类器可能要求确认 |
| CC-B7 | worker | 有合并授权，但 `gh pr merge` 没有 `-R HOST/OWNER/NAME` 或本仓库的 PR 链接与配置的 `host`（默认 `github.com`）、`repository` 一致；或用 `gh api` 合并 | 按当前目录或遗留的 `GH_HOST` 合并了别的仓库或别的主机的 PR（目标错误） | `gh pr merge <编号> -R github.com/<owner>/<name>`，或该仓库的 PR 链接 |
| CC-B8 | worker | `CronCreate` / `ScheduleWakeup` 的提示不以 `<scheduled-task>` 开头 | 定时提示到达时与用户消息无法区分，被记成用户答复（live `host-e2e` 实测，误认来源） | 提示以 `<scheduled-task>` 开头、`</scheduled-task>` 结束 |
| CC-B9 | 主会话 | `start_session` 的会话名不是已落账、代次有效的 attempt | 没有先 `attempt_start` 就建会话，或重复建会话 | 先 `attempt_start`，用返回的会话名。**不可放行**：没有落账的会话无法绑定到 attempt（DN002 §6） |
| CC-B10 | 已撤销 / 待绑定 | 编辑类工具、Bash、`start_session` | 已被替换的会话继续写；绑定前就开始工作（状态漂移） | 已撤销：回到主会话；待绑定：等主会话绑定后的消息 |

**escape**（DN008 §5 第 4 条）：每条拒绝信息以规则编号开头，写出上表的正确做法；可放行的规则还写明“动作确属任务需要时，问用户；用户同意后用 `user_answer` 登记放行这条规则”。CC-B6、CC-B9 是状态机守卫的镜像，不能放行或关闭（`waive_rules` 与 `guardrails.disabled` 中出现它们时以 `invalid_input` 拒绝），只给出状态机的路径。其余规则的做法：

1. **就地放行（worker）**：worker 用 `ask_user` 说明被拦下的动作与理由；用户在 worker 会话中回复后，worker 调用 `user_answer`，除答复外带上 `waive_rules`（如 `["CC-B4"]`）。tsw 用 hook 记录的用户原话记录 `guardrail.waived`（规则编号、attempt、用户来源）。hook 对该 attempt 不再执行这些规则；attempt 结束即失效。这与评审预算追加的做法相同（DN006 §8.1a），不需要主会话参与。
2. **长期关闭（用户）**：配置 `guardrails.disabled` 列出的规则对所有会话不再拦截。它是新的权限类键（DN003 §7.2），由主会话按用户在主会话或其他渠道的原话修改；worker 会话中的原话只用于就地放行，不改全局配置。
3. **主会话被拦下**（CC-B1、CC-B2）：在主会话中问用户，按用户的话用 `config_change` 写入 `guardrails.disabled`，或由用户自己执行。CC-B9 不可关闭：先 `attempt_start`，再用返回的会话名创建会话。
4. **用户自己做**：用户总可以在会话外自己执行被拦下的动作。

**失败方式**：hook 内部异常时放行并在 stderr 报告（现状不变）。

## 4. 其他 hooks

| 编号 | hook | 行为 | 归类 |
| --- | --- | --- | --- |
| CC-S1 | SessionStart | 识别角色；主会话领取租约并注入 attention 摘要；worker 注入任务与回传规则；待绑定会话提示等待绑定 | 保留（不是拦截） |
| CC-S2 | UserPromptSubmit（worker） | worker 有待答问题时，把用户消息原文记为 `input.session_message` 并告诉 worker 编号；去掉宿主前置的 `<system-reminder>`（T241）；以 `<cross-session-message` `<side-sessions-event` `<task-notification` `<ci-monitor-event` `<scheduled-task` 开头的注入消息不记 | 保留。前置 reminder 的修复是必需的：本任务中用户在 worker 会话的回复以 `<system-reminder>` 开头，`main` 上的实现把它当成注入消息没有记录，worker 因而无法登记答复 |
| CC-S3 | UserPromptSubmit（worker） | 检测 `[report]` 并提示回传 | 保留 |
| CC-S4 | Stop（主会话） | 有未处理的回传或到期未转交的问题时阻止结束一次 | 保留 |
| CC-S5 | Stop（worker） | 评审进行中时阻止结束一次；记录 `worker.turn_ended` | 保留 |

## 5. 现有校验逐项归类

归类：**保留** = 按现状，只改措辞；**简化**；**删除**；**改写** = 由 §3 的某条 block list 规则取代。

### 5.1 hooks 守卫（`tsw/host/guard.py`）

| 现有校验 | 归类 | 理由 |
| --- | --- | --- |
| worker 编辑类工具的目标必须在可写根内 | 改写为 CC-B1、CC-B3 | allow list；改为只拦已知的错误目标（数据目录自有目录、主仓库检出），写到临时目录、缓存等不再被拦 |
| Bash 写入路径必须在可写根内（`tsw/host/shell.py` 解析重定向、文件命令、PowerShell cmdlet、`cd`、嵌套 shell） | 删除 | allow list，解析复杂；删除、移动、写到意外位置属于一般风险，交给 auto 分类器；漏网的可能在主会话验收核对 diff 时发现，也可能漏掉，按易用性优先接受（DN008 §5 第 5 条）。`shell.py` 只保留分词与程序、子命令识别（CC-B4–CC-B7 用） |
| Bash 写入 tsw 自有目录（解析出的路径） | 删除 | 同上；账本被意外改动时由摘要链与 `ledger audit` 发现（确定性）；其他自有目录的意外改动可能漏掉 |
| 文本兜底：命令中出现数据目录路径加自有目录名，且出现 `>` `rm` `cp` 等写入词就拒绝 | 删除 | 误拦只读命令：`grep … runs/…/brief.md > work/…/x.txt`、`cp runs/…/brief.md work/…/` 都被拒（本任务用临时目录实测） |
| `.claude/settings*.json` 不能写（T241，编辑类工具与 shell 写法） | 简化为 CC-B2 | 只看编辑类工具；理由改为越过用户的权限选择与被覆盖，不再写“防止会话给自己追加权限” |
| `git push` / `gh pr create` 对照 `delivery.push` / `create_pr` | 保留为 CC-B4、CC-B5 | 授权记录在 tsw 中，宿主不知道 |
| 合并须是开发任务且有 `delivery.merge` | 保留为 CC-B6 | 同上 |
| 合并目标：必须写明 host、拒绝 `GH_REPO` / `GH_HOST`、拒绝短选项合写、解析 REST 合并路径 | 简化为 CC-B7 | 保留“连同 host 写明本仓库”（省略 host 时遗留的 `GH_HOST` 会改变目标）；host 写明后环境变量不再影响目标，去掉对它们的单独拒绝与合写选项解析；`gh api` 合并一律拦下。现状的拒绝信息“无法确认的目标：o/n”（本任务实测）改为写出 `-R github.com/o/n` |
| 会话中运行 `tsw` 写入口；运行 `tsw guard`（理由“否则会话可以编造用户消息”） | 删除（hook 一侧） | CLI 自己按会话身份拒绝（`host_session_cli`，§5.2）；`tsw guard` 的拦截只为对抗伪造 |
| `python -c` / heredoc 中 `import tsw` 就拒绝 | 删除 | 只为对抗；误拦调查或开发 tsw 本身的 worker（本任务中 `python -c "from tsw…"` 被拒） |
| 对可执行文件求哈希的命令被拒（`policies.executable_hashing`） | 删除 | 用户的通用约定（2026-10-30），不是 tsw 特有的事；拒绝信息提到的“账本中的用户批准记录”并不存在。建议用户放进自己的全局指令。若用户希望保留，可作为 CC-B11 加入 block list（确定性、简单） |
| 会话目录与执行目录不一致时拒绝编辑与命令（只放行 `cd` 回去） | 删除（hook 一侧） | 打断太多：任何 `cd` 到别处都会挡住全部操作；最常见的错误目标由 CC-B3 拦截。MCP 写工具的目录一致性检查保留（§5.3） |
| 已撤销 / 待绑定会话拒绝编辑、命令与建会话 | 保留为 CC-B10 | 状态漂移 |
| 主会话 `start_session` 须是已落账的会话名 | 保留为 CC-B9 | 走错步骤 |
| worker 定时提示须带 `<scheduled-task>`（T241） | 保留为 CC-B8 | 理由改为实测的误认来源，删去“worker 可以伪造用户答复” |
| 会话消息记录、`[report]`、Stop、SessionStart | 保留为 CC-S1–CC-S5 | — |
| hook 异常时放行 | 保留 | DN008 §8 第 2 条 |

### 5.2 身份与角色解析（`tsw/host/identity.py`、`tsw/cli.py`）

| 现有校验 | 归类 | 理由 |
| --- | --- | --- |
| 按 `CLAUDE_CODE_HOST_SESSION_ID` 解析角色：worker、已撤销（不回落为主会话）、主会话、待绑定、其他 | 保留 | 每个会话拿到正确的角色、工具与上下文；措辞从“防冒充”改为这一点 |
| 会话目录只作一致性检查 | 保留 | — |
| 已登记会话中 CLI 只开放 `HOST_CLI_COMMANDS`，其他命令以 `host_session_cli` 拒绝 | 保留 | tsw 自己的接口：用 `tsw tool run --role …` 代替 MCP 会让角色、租约由参数声明（走错步骤）；拒绝信息指向 MCP 工具 |
| 租约记录机器标识 | 保留 | 状态漂移 |
| 不在 prompt 中放 token | 保留 | 理由改为“没有必要：会话 ID 已足够区分角色” |

### 5.3 按角色开放 MCP 工具（`tsw/host/mcp.py`、`tsw/machine/base.py`）

| 现有校验 | 归类 | 理由 |
| --- | --- | --- |
| 工具列表按角色给出；调用时再查角色 | 保留 | 接口语义（DN008 §6） |
| worker 只能操作自己的任务、必须是当前 attempt 绑定的 live 会话 | 保留 | 对象错误、状态漂移 |
| worker 只能读授权读取集合（`read_scope`） | 删除 | 用户 2026-11-08 决定放宽（DN008 §12）：读取不会写错状态；授权读取集合改为推荐起点（DN007 §7.2） |
| worker 写工具要求会话目录一致（`cwd_mismatch`） | 保留 | 会话建在了错误目录（绑定错误） |
| 主会话写工具要求租约（G-LEASE） | 保留 | — |
| `expected_version`、`command_id` 内容不同则拒绝 | 保留 | — |
| 权限类配置键须附用户原话，绑定修改前文件摘要；一条原话只能授权一次修改（`source_reused`） | 简化 | 删除 `source_reused`：摘要绑定已防住对象错误 |
| 正式导入须先同参数演练、附用户原话；一条原话只授权一次导入 | 简化 | 决定对象加入参数摘要，删除“只用一次”的扫描 |
| 接管须附用户原话；配置无效时只开放配置工具；worker 不能改配置 | 保留 | — |

### 5.4 状态机守卫（`tsw/machine/guards.py`）

G-PLAN、G-DEP、G-LEASE、G-SLOT、G-RES、G-WINDOW、G-ONE-ATTEMPT、G-PAUSE、G-SUBMIT、G-ACCEPT 与迁移合法性全部保留（DN008 §6）。G-USER 简化：`VerifiedSource` 的 `principal` 改为可选，不再校验、不再称“已核实”。

### 5.5 用户来源

| 现有校验 | 归类 | 理由 |
| --- | --- | --- |
| `VerifiedSource` 必需 `channel` `principal` `message_ref` `text` 与一致的 `target` | 简化 | `principal` 可选（DN008 §9） |
| 主会话代答、worker 回执、超时不满足 G-USER | 保留 | — |
| `user_answer` 必须引用 hook 记录的、同一任务同一问题同一会话的消息 | 保留 | 理由改为“答复以用户原话为准”，删去“worker 不能伪造答复” |
| 主会话不能记录 `in-session` 来源；worker 会话中的原话不能授权改配置 | 保留 | 对象错误：用户在 worker 会话中回答的是本任务的问题；需要放行护栏时用本任务范围的就地放行（§3） |
| `teams-chat`：按消息 ID 精确读回、发送者为本人（未实现） | 删除 | 只对抗他人冒充；答复登记只核对“问题仍待答、回复对应到问题编号”（DN008 §9） |
| `hub-portal`：owner 认证（未实现） | 简化 | owner 认证是门户自己的访问控制，不属于 tsw |
| 只有 `pending` 且上下文绑定一致时 T12 生效，其余记为迟到答复 | 保留 | 即“问题仍待答” |

### 5.6 账本与其他

| 现有校验 | 归类 | 理由 |
| --- | --- | --- |
| 摘要链（`checksum` / `prev`）、`ledger audit`、投影与账本不一致时拒绝写、重建前审计、备份 manifest、未知事件类型停止归约 | 保留 | 措辞改为“检测截断、损坏与意外修改”，删去“防篡改” |
| 报告草稿须在本 attempt 的 `work/` 中；评审输出按 schema 解析；评审模型须与配置一致；导入快照路径不越界；配置与任务书的输入校验 | 保留 | 对象错误、跳过评审、输入错误 |

## 6. 允许规则（T241）

| 会话 | 设置文件 | 允许 | 由谁把关 |
| --- | --- | --- | --- |
| 主会话 | 数据目录 `.claude/settings.json` | tsw MCP 工具；`start_session`、`send_message` / `SendMessage`、`stop_session`、`get_session`、`list_sessions`、`archive_session` | CC-B9；tsw 工具的角色、租约、守卫 |
| worker | 执行目录（在数据目录 `work/` 之下时）`.claude/settings.local.json` | tsw MCP 工具；`send_message` / `SendMessage`；有合并授权时 `Bash(gh pr merge:*)` | CC-B6、CC-B7；hook 的拒绝优先于允许规则 |

- `start_session` 由 app 自己的分类器把关、不受允许规则约束，所以创建 worker 会话时 `prompt` 只写短指令、`background` 只写标题与目录，任务书正文由 worker 从 `runs/` 读取（T241 实测：正文中的“自行合并”被分类器误判）。
- 其余一切动作仍由 Claude Code 的权限模式与分类器判断：tsw 不放宽它不检查的动作。
- 已知缺口：执行目录不在 `work/` 之下（例如数据目录外的仓库 worktree）时不写允许规则，那里的 worker 合并时可能被分类器拦下；由用户在该目录自行配置，或把 worktree 放在 `work/` 之下。

## 7. 对其他文档与代码的影响

本次已随文档拆分修订的：DN001 §1、§4、§7、§8.2、§10、§11 的措辞，DN002 §3.5 的合并目标，DN006 §6.3 的规则强度与 §8.1a 第 3、4 步。其余待实现时修订：

| 位置 | 修订 |
| --- | --- |
| DN003 §3.5 `executable_hashing` 执行点、DN003 §7.2 `source_reused`、DN003 新增 `guardrails.disabled`；DN006 §8.1a `user_answer` 新增 `waive_rules` 与事件 `guardrail.waived` | 按 §3、§5 |
| DN006 §8.1b 定时提示段（T241）中“伪造”；§8.5 `verify` 与 `principal`；§8.6 Teams 核实；§8.8 Portal；§9.7 导入授权 | 按 DN008 §9 与 §5.5 |
| 代码注释：`guard.py` 模块说明“Hooks are the enforced boundary”；`identity.py`、`cli.py`、`guard.py` 中“could claim / invent”式注释 | 改为护栏的说法 |
| 宿主能力 `enforcement: enforced \| advisory` | 取值不变（契约），解释为机械检查覆盖的程度 |

兼容性：`VerifiedSource.principal` 从必需改为可选，旧事件与旧调用照常有效；导入授权的旧决定对象在过渡期仍接受；删除的只是拒绝，不删除工具、事件、字段或命令；新增的只有配置键 `guardrails.disabled`、`user_answer` 的可选参数 `waive_rules` 与事件类型 `guardrail.waived`。

## 8. 实现拆分（由用户决定）

一种可行的拆分：

1. **措辞**（只改文档与注释）：§7。
2. **hooks 改为 block list**（行为变化，需测试与 live `host-e2e`）：CC-B1–CC-B10、规则编号与 escape 信息、就地放行（`waive_rules`、`guardrail.waived`）与 `guardrails.disabled`；删除 shell 路径解析、文本兜底、tsw CLI 与 `import tsw` 识别、哈希规则、hook 侧目录一致性检查。
3. **用户来源、授权与读取范围**（契约兼容变化）：`principal` 可选、删除 `source_reused`、导入授权对象加参数摘要；worker 的检索与 `task_read` 放宽为可读全部任务（DN007 §7.2）。

第 2、3 项涉及 hooks 守卫与 MCP 写工具授权，按 `AGENTS.md` 第 4 节须用户确认后合并。T241 中的 CC-B2、CC-B8、CC-S2 与 §6 在 T241 合并后按本文调整措辞与归类。
