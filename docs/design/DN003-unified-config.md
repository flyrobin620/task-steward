# DN003 统一配置

- 状态：设计草案（T233 r5，已按 PoC 结果修订；T240 增加 §7 经 tsw 工具修改配置）
- 日期：2026-11-05
- 相关：[DN001 总体架构](DN001-architecture.md) · [DN002 任务状态机](DN002-task-state-machine.md) · [DN004 worker 自评审](DN004-worker-self-review.md)

## 1. 原则

1. **一个文件**：所有可调参数都在数据目录（`TSW_HOME`，DN006 §9）的 `config/task-steward.json` 中，包括各角色的模型、effort、权限模式、评审器、调度、仓库、账号引用、通知和策略。不再分散在 `model-settings.json`、`repositories.json`、`.codex/config.toml`、平台参考文档等多处。
2. **请求值与回执分开**：配置是“请求值”；子会话实际的模型、effort、权限以平台回执（`get_session`）为准，并记录在 attempt 事件中。拿不到回执时记为“未知”，不反复追问子会话。比较请求值与回执时先规范化模型 ID：平台可能追加上下文后缀（例如请求 `claude-sonnet-5-5`，回执为 `claude-sonnet-5-5[1m]`）。
3. **不含秘密**：凭据、令牌只按名称引用系统凭据库或 `.local` 中的认证目录，配置里不出现秘密值。
4. **本机覆盖受限**：数据目录中的 `config/task-steward.local.json` 只能覆盖白名单中的本机路径类字段（§5），不能改模型、策略、交付授权。
5. **显式生效**：修改配置后只影响新创建的会话和新调用；已有子会话不自动切换模型。需要切换时由主会话调用 `set_session_model` / `set_session_effort`，并记录事件。

## 2. 完整示例

```json
{
  "version": 1,
  "control": {
    "language": "zh-CN",
    "ledger_dir": "ledger",
    "runs_dir": "runs",
    "todo_index": "todo/TODO.md",
    "status_page": "views/STATUS.html",
    "archive_page": "views/ARCHIVE.html",
    "data_dir_sync": {"enabled": false}
  },
  "plugins": {
    "host_integration": "claude-code",
    "sessions": ["claude-app", "codex-app-server", "codex-exec"],
    "human_channels": ["in-session", "teams-chat"],
    "storage": {"use": "fs"}
  },
  "input": {
    "triage": "orchestrator_judgment",
    "channels": ["in-session", "teams-chat"],
    "decision_digest": {"enabled": true, "schedule": "daily_and_on_task_done", "channel": "teams-chat"},
    "teams_poll": {"interval_minutes": 5, "max_interval_minutes": 30}
  },
  "roles": {
    "orchestrator": {
      "session": "claude-app",
      "model": "claude-opus-5-5",
      "effort": "medium",
      "permission_mode": "auto"
    },
    "worker": {
      "session": "claude-app",
      "model": "claude-opus-5-5",
      "effort": "high",
      "permission_mode": "auto",
      "context": "fresh",
      "prompt_template": "config/prompts/worker.md",
      "allow_in_task_subagents": true
    },
    "reviewer": {
      "session": "codex-app-server",
      "fallback_session": "codex-exec",
      "model": "gpt-6-astra",
      "effort": "medium",
      "context_window": 1200000,
      "timeout_seconds": 1800
    }
  },
  "review": {
    "max_rounds": 3,
    "orchestrator_extension_limit": 3,
    "retry_limit": 2,
    "block_on": ["critical", "high"],
    "incremental": "resume_thread",
    "prompt_templates": {
      "code": "config/prompts/review-code.md",
      "design": "config/prompts/review-design.md",
      "research": "config/prompts/review-research.md"
    },
    "output_schema": "config/schemas/review-result.schema.json"
  },
  "ledger": {"segment_max_mb": 16, "checkpoint_every": 64},
  "context": {"catalog_page_size": 8, "max_chars": 20000, "fts_tokenizer": "trigram", "short_query_fallback": "like_cards_only", "require_read_reason": true},
  "scheduling": {
    "max_workers": 5,
    "lease_minutes": 120,
    "prestart": {"enabled": true, "max_standby": 10},
    "wake": {"worker_message_parent": true, "reportable_events": ["worker.submitted", "worker.question", "review.budget_exhausted", "review.defer_requested", "review.unavailable", "task.paused", "report_back"]}
  },
  "sessions": {
    "name_pattern": "{task_id_lower}-{attempt}-g{generation}",
    "archive_on_accept": false,
    "archive_on_archive": true
  },
  "worktrees": {
    "root": "~/tsw-worktrees",
    "branch_pattern": "tsw/{task_id_lower}-{attempt}",
    "work_dir": "work/{task_id}/{attempt}"
  },
  "reports": {
    "formats": ["md", "html"],
    "html_from_md": true,
    "print_styles": true
  },
  "repositories": {
    "example-app": {
      "aliases": ["app"],
      "path": "~/src/example-app",
      "provider": "github",
      "host": "github.com",
      "repository": "example-org/example-app",
      "remote": "origin",
      "base_ref": "origin/main",
      "integration_branch": "tsw/integration-example-app",
      "auth_profile": "github-work",
      "commit_identity": {"name": "Your Name", "email": "you@example.com"},
      "instructions": ["AGENTS.md", "CLAUDE.md"],
      "worktree_files": [{"source": "dev-settings.json", "destination": ".local/dev-settings.json"}],
      "validation": [],
      "delivery": {"push": true, "create_pr": true}
    }
  },
  "accounts": {
    "github-personal": {"provider": "github", "host": "github.com", "account": "your-handle", "auth": {"kind": "gh", "config_dir": "~/.config/tsw-auth/gh/github-personal"}},
    "github-work": {"provider": "github", "host": "github.com", "account": "your-work-handle", "auth": {"kind": "gh", "config_dir": "~/.config/tsw-auth/gh/github-work"}}
  },
  "notifications": {
    "teams_self_chat": {"enabled": true, "transport": "teams-mcp", "events": ["task.accepted", "task.paused", "input.requested", "worker.question", "review.escalated"]}
  },
  "policies": {
    "executable_hashing": "forbidden_without_user_approval",
    "email": "draft_only",
    "pr_default_state": "ready_for_review",
    "user_acceptance_source_required": true,
    "auto_archive_days": 7,
    "reserved_for_user": ["send_email", "merge_protected_branch", "production_deploy", "delete_user_data"]
  },
  "import": {
    "sources": {
      "orchestrator-agent": {
        "root": "~/orchestrator-agent",
        "mode": "copy-ledger-reference-runs",
        "resumable": ["pending", "ready", "paused", "done_without_user_acceptance", "active_handover"]
      }
    }
  }
}
```

## 3. 字段说明

### 3.1 `roles`

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `orchestrator.session` | 会话适配器名 | 主会话所在宿主（DN006 §7.4）；用户在该宿主中打开主会话 |
| `orchestrator.model` / `effort` | 模型 ID / `low`…`max` | 主会话由用户在宿主中打开，tsw 只校验并在启动时提示不一致，不能替主会话改模型 |
| `orchestrator.permission_mode` | 中性取值 `ask` / `edits` / `auto` / `full` | 主会话的建议模式；子会话的模式不能高于它。由会话适配器映射为宿主名称（DN006 §7） |
| `worker.session` | 会话适配器名 | 承载 worker 的会话适配器，须具备 `interactive`；delegated 适配器必须与主会话同一宿主 |
| `worker.model` / `effort` | 同上 | 经会话适配器传给宿主（例如 Claude app 的 `start_session`） |
| `worker.permission_mode` | 同上 | 经会话适配器映射后传给宿主（例如 Claude app 的 `start_session.permission_mode`）；高于主会话时 tsw 在派发前拒绝并提示 |
| `worker.context` | `fresh` | 固定 fresh；`fork` 会把主会话长上下文带入，不允许 |
| `worker.prompt_template` | 路径 | worker 启动指令模板，可用变量：`{task_id}` `{attempt}` `{brief_path}` `{writable_roots}` `{report_paths}` |
| `worker.allow_in_task_subagents` | 布尔 | worker 能否在自己任务内使用 Agent 子代理 |
| `reviewer.session` / `fallback_session` | 会话适配器名 | 评审所用会话适配器，可与主会话、worker 不同；须具备 `oneshot`、`direct`、`read_only_enforced`（DN006 §7.4） |
| `reviewer.model` / `effort` / `timeout_seconds` | — | 默认 `gpt-6-astra` / `medium`；只读由所选适配器强制，不另设字段 |

已删除字段：`roles.comparison`（Astra 对照版）。导入历史配置时如出现该字段，记录为已忽略，不报错。

**不可配置的评审规则**：评审是必经环节，任何人（包括主会话）都不能跳过；提交 HEAD 必须等于最后评审 HEAD。这两条是 DN002 的不变量（G-SUBMIT），配置中没有开关；配置中出现 `review.required`、`review.require_final_head_reviewed`、`review.on_unavailable` 时报错。评审器不可用、预算用完等情形交主会话判断（DN006 §8）。

### 3.1a `plugins` 与 `input`

`plugins` 声明加载哪些插件实现（DN006 §6–§9），每个实现自带配置 schema，校验时一并检查。三个角色各自用 `roles.*.session` 从已加载的会话适配器中选择，可以互不相同。组合示例见 DN006 §10。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `plugins.host_integration` | `claude-code` | 宿主集成（DN006 §6）：生成流程说明、工具注册与规则执行配置 |
| `plugins.sessions` | `claude-app`、`codex-app-server`、`codex-exec` | 加载的会话适配器；`roles.*.session` 只能引用其中的实现 |
| `plugins.human_channels` | `in-session`、`teams-chat` | 可用的人类渠道 |
| `plugins.storage.use` | `fs` | `fs` 或 `fs+git`（数据目录作为用户私有 Git 仓库） |
| `input.triage` | `orchestrator_judgment` | `orchestrator_judgment`：主会话在授权边界内综合判断（DN006 §8.2）；`human_direct`：主会话不判断，一律交给人类 |
| `input.channels` | 已加载的全部 `request_response` 渠道 | 升级时同时投递的渠道；必须是已加载渠道的子集；第一个通过核实的答复生效 |
| `input.decision_digest` | 每日及任务完成时，经 Teams | 把主会话的自主决定汇总通知用户，便于事后核对（DN006 §8.4）；`channel` 为 `"local"` 时只写入 STATUS 页与验收报告 |
| `input.teams_poll` | 5 分钟，退避到 30 分钟 | Teams 渠道的轮询间隔（主会话运行时） |

### 3.2 `review`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `max_rounds` | 3 | 每个提交周期的基础评审轮次（含首轮）；有效预算还要加上主会话与用户的追加（DN002 §3.3）。用完仍有阻断项时交主会话判断（DN006 §8.3）：可追加轮次、认定意见不成立、批准延后、调整方向或交给人类。轮次编号在 attempt 内持续递增 |
| `orchestrator_extension_limit` | 3 | 同一提交周期内主会话累计最多追加的轮次；再需要追加只能交给人类决定 |
| `retry_limit` | 2 | 单轮调用失败的重试次数 |
| `block_on` | `critical`, `high` | 必须处置（修复或有据拒绝）的严重级别 |
| `incremental` | `resume_thread` | 后续轮次复用同一 reviewer 线程，只审增量；`fresh` 表示每轮新线程全量审 |
| `prompt_templates` | 三类任务各一个 | 评审提示模板 |
| `output_schema` | JSON Schema 路径 | 评审器输出必须满足的结构（DN004 §4） |

### 3.2a `policies.reserved_for_user`

主会话判断的唯一硬边界是“不越过已有授权”（DN006 §8.2）。`policies.reserved_for_user` 列出用户额外保留给自己的决定，例如：

```json
{"policies": {"reserved_for_user": ["send_email", "merge_protected_branch", "production_deploy", "delete_user_data"]}}
```

凭据输入、用户验收与退回、超出任务书和配置授权的动作始终保留给用户，不需要也不能在这里删除。

### 3.2b `ledger` 与 `context`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `ledger.segment_max_mb` | 16 | 活动分段达到该大小时封存为 `.jsonl.gz` |
| `ledger.checkpoint_every` | 64 | 每多少个事件写一次 L1 检查点（DN007 §3.4） |
| `context.catalog_page_size` | 8 | `context_catalog` 每页卡片数 |
| `context.max_chars` | 20000 | 单次 `context_read` 返回的最大字符数，超出分页 |
| `context.fts_tokenizer` | `trigram` | 全文索引分词器；中文依赖 trigram（DN007 §5.2） |
| `context.short_query_fallback` | `like_cards_only` | 少于 3 个字符的查询词在卡片与别名上做子串匹配 |
| `context.require_read_reason` | `true` | `context_read` 是否必须写理由 |

### 3.3 `scheduling` 与 `sessions`

- `max_workers`：活动任务上限（DN002 G-SLOT）。实际并发取它与平台可用额度的较小值。
- `prestart.enabled` / `prestart.max_standby`：是否在用户 turn 中为已规划任务预启动 worker 会话，以及同时待命的会话上限（DN002 §5.6）。关闭预启动时，自动 turn 中变为可派发的任务都要等用户下一次发消息才能创建会话。
- `wake.worker_message_parent`：reportable 事件发生时，worker 是否同时用 `mcp__ccd_session_mgmt__send_message(session_id="parent")` 给父会话发消息（DN001 §6.3）。
- `sessions.archive_on_accept`：验收后是否归档子会话。默认 `false`，保留以便用户回看；归档时须有工具回执。

### 3.4 `repositories` 与 `accounts`

结构沿用 orchestrator-agent 的 `config/repositories.json` 与 `config/accounts.json`，合并为两个小节（账号只记录名称与认证目录引用，凭据本身不进数据目录），并新增 `aliases`（原 `repositories.md` 中的缩写表）。导入时自动转换（DN005 §4.4）。`delivery.push` / `create_pr` 是该仓库的默认交付授权，具体任务仍以用户当次指令为准。

### 3.5 `policies`

集中保存原先写在 AGENTS.md 中、可由程序检查的用户约定。hooks 和门禁读取这些值：

| 字段 | 来源约定 | 执行点 |
| --- | --- | --- |
| `executable_hashing` | 用户 2026-10-30：默认不做可执行文件哈希 | worker PreToolUse hook 拦截 |
| `email` | 邮件默认只存草稿 | 发送类 MCP 工具调用前的 hook |
| `pr_default_state` | 以合并为目的的 PR 默认 ready-for-review | `pr_link` 登记校验 |
| `user_acceptance_source_required` | 用户验收须有真实来源 | `user_accept` 门禁 |
| `auto_archive_days` | 已验收满 7 天归档 | sync 规则 T24 |

## 4. 校验规则

tsw 在加载时完整校验，任何错误都拒绝所有写命令（只读命令仍可用；修改配置的工具见 §7.1）。数据目录维护命令（`quiesce`、`resume-writes`、`backup`、`restore`）不读取配置，配置损坏时仍可用于备份与修复。默认值中的字段都是必填的，用 `null` 删除后报错；只有明确可选的字段（如 `roles.reviewer.fallback_session`）可以删除。结构错误（类型不对、缺少必填字段）先报告，语义规则在结构正确后才检查：

1. `version` 必须为 1；未知字段报错（防止拼写错误被静默忽略），但 `roles.comparison` 这一已删除字段只记录警告。
2. 模型 ID 匹配 `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`；effort 属于 `low|medium|high|xhigh|max`。
3. `roles.worker.permission_mode` 不高于 `roles.orchestrator.permission_mode`，按中性取值比较（顺序：ask < edits < auto < full）；比较之后才由会话适配器映射为宿主名称。
4. `roles.*.session` 与 `fallback_session` 必须是 `plugins.sessions` 中已加载的实现，并满足 DN006 §7.4 的能力要求：worker 需 `interactive`（delegated 时与主会话同宿主）；评审器需 `oneshot`、`direct`、`read_only_enforced`。不满足时校验失败，不能派发。
5. 路径字段为绝对路径或相对数据目录的路径；模板与 schema 文件必须存在。
6. 每个仓库的 `auth_profile` 必须在 `accounts` 中存在，且 provider / host 一致；`accounts.*.auth.config_dir` 解析后必须位于数据目录之外（凭据不进入 `TSW_HOME`，也就不会被备份）。
7. 别名全局唯一，不能与其他仓库 ID 冲突。
8. `input.channels` 中的每个渠道都必须已在 `plugins.human_channels` 中加载，且能力为 `request_response`；未显式设置时取已加载的全部 `request_response` 渠道。
9. `input.decision_digest.enabled=true` 时，`channel` 必须为 `"local"`（只写入 STATUS 页与验收报告，不经任何渠道通知），或是已加载且支持 `notify` 的渠道；合并后缺少 `channel` 视为配置错误。`null` 只用于合并时删除字段（§5a），不作为业务取值。
10. 校验针对合并后的有效配置（默认值 + 数据目录配置 + 本机覆盖），`tsw config show --effective` 可查看结果。

## 5. 本机覆盖

`config/task-steward.local.json` 只允许覆盖：`repositories.*.path`、`worktrees.root`、`roles.reviewer.executable`、`accounts.*.auth.config_dir`、`import.sources.*.root`。其他字段出现在本机覆盖中时报错。

## 5a. 合并规则

有效配置由三层按顺序合并：产品内置默认值 → 数据目录 `config/task-steward.json` → 本机覆盖 `config/task-steward.local.json`。合并规则：

- 对象逐字段合并；
- 数组整体替换（例如 `plugins.sessions` 与 `input.channels`）；
- 值为 `null` 表示删除该字段，回到“未设置”（例如 `roles.reviewer.fallback_session: null` 表示不使用回退评审器）；
- 合并完成后才做 §4 的校验。DN006 §10 的组合示例都是第二层的覆盖片段。

## 6. 生效与派生物

| 派生物 | 生成方式 | 说明 |
| --- | --- | --- |
| worker 启动指令 | 派发时由 `prompt_template` 渲染 | 每个 attempt 写入 `runs/Txxx/aN/brief.md` 并随 attempt 事件记录模板版本 |
| 评审请求 | `review_run` 时由模板渲染 | 写入 `review/rK-request.md` |
| 有效配置快照 | `tsw config show --effective` | 显示合并本机覆盖后的结果，并标出每个值的来源 |
| 配置摘要 | 每次派发记录在 `task.dispatch_started` 事件中 | 用于解释“这个 attempt 是按哪份配置创建的” |

配置文件位于数据目录，随数据目录备份；使用 `fs+git` 存储时也随数据目录的私有仓库提交。产品 repo 只提供 schema 与默认模板。tsw 不会把配置值复制到其他配置文件。

## 7. 经 tsw 工具修改配置

用户 2026-11-06 决定：主会话可以修改任何配置，每次修改校验通过才生效、记入账本、可以回退；扩大或收窄权限的键须附用户原话。worker 不能修改配置。主会话直接编辑 `config/` 中的文件仍被 hooks 拒绝；用户在会话外手工编辑仍然允许，tsw 不做外部修改检测。

### 7.1 工具与执行顺序

| 工具 | 角色 | 作用 |
| --- | --- | --- |
| `config_change` | 主会话（持有调度租约） | 按顺序应用 `ops` 修改 `config/task-steward.json`：`{op: "set", path, value}` 或 `{op: "unset", path}`；`path` 用点号，键名含点号时写成字符串数组；空路径的 `set` 替换整个文件。附 `reason`，可选 `user_source` 与 `expected_sha256` |
| `config_revert` | 主会话（持有调度租约） | 把文件恢复到某次修改（`to_change`，如 `C003`）**之前**的内容；恢复本身也是一次修改 |
| `config_show` | 主会话（只读） | 有效配置、每个值的来源、校验问题，以及配置文件的 sha256 |
| `config_history` | 主会话（只读） | 修改记录；给出 `change_id` 时返回该次修改前后的全文 |

两个写工具在账本提交锁内按以下顺序执行：

1. 读取当前文件，计算摘要；给了 `expected_sha256` 而不一致时以 `config_conflict` 拒绝。
2. 应用修改，得到新的文件内容；与默认值、本机覆盖合并后按 §4 完整校验。**无效时以 `config_invalid` 拒绝，文件不动，不记账。**
3. 比较修改前后的叶子字段，得到改动的键；其中有权限类键（§7.2）时检查 `user_source`。
4. 记录 `config.changed` 事件：修改编号（`C001` 起）、类型（`change` / `revert`）、`ops` 或被恢复的修改编号、改动的键、权限类键、理由、用户来源、修改前后的全文与摘要。
5. 写入文件（临时文件加原子替换）。

事件先落账再写文件。两步之间中断时，下一次任何提交（或 `ledger rebuild`）发现最近一次修改尚未写入，且文件仍是该修改记录的“修改前”内容，就补写新内容；文件已是其他内容（用户在此期间手改过）时不覆盖。“已经处理到哪一次修改”记在 `cache/config-applied.json`，不在投影里，所以 `ledger rebuild` 不会把早已写入的修改再次写到用户之后手工恢复的内容上；从备份恢复后（`cache/` 不备份）只按文件内容判断。配置不含秘密（§1），所以事件保存全文，任何一次修改都能精确回退。

当前配置无效时（例如手工编辑后不是合法 JSON），其他写工具都被拒绝，但 `config_change` 与 `config_revert` 仍可用：它们只要求修改**后**的配置有效。修复需要调度租约，所以配置无效时仍按默认租期领取、续期、释放或接管租约（主会话的 SessionStart 与 `lease_acquire`，命令行的 `tsw lease`）。当前文件不能解析时，只能用空路径的 `set` 替换整个文件。命令行 `tsw tool run config_change` 在会话外同样可以修复。

这两个工具只修改 `config/task-steward.json`。本机覆盖 `config/task-steward.local.json` 只含路径类字段（§5），仍由用户编辑。

### 7.2 权限类键

改动下列键（新增、删除或修改，**不区分扩大还是收窄**）时，必须附用户原话：

| 键 | 理由 |
| --- | --- |
| `repositories.*.delivery`、`repositories.*.auth_profile`、`repositories.*.commit_identity`，以及新增或删除整个 `repositories.<id>` | 远端交付授权、交付身份 |
| `accounts` | 凭据引用 |
| `policies` | 用户保留的决定与约定 |
| `roles.*.permission_mode`、`roles.*.session`、`roles.reviewer`（全部）、`roles.worker.allow_in_task_subagents` | 角色权限模式、会话宿主、评审器 |
| `plugins` | 加载的会话适配器、人类渠道与存储决定能力边界 |
| `input.triage`、`input.channels` | 主会话能否自行判断、问题投到哪里 |
| `review.block_on`、`review.max_rounds`、`review.orchestrator_extension_limit` | 评审门槛与主会话可自行追加的上限 |

替换整个上级对象（例如 `roles`）同样算改动其下的权限类键。收窄也要原话：逐键判断扩大还是收窄容易出错，一律要求原话更简单。

用户原话以 `VerifiedSource`（DN006 §8.5）提交，渠道为主会话、Teams 或 Portal（worker 会话中的原话不能授权主会话改配置），决定对象为 `["config", <修改前文件的 sha256>, "authorize"]`：它绑定到这一次修改所基于的文件。一条用户消息（渠道 + 消息引用）只能授权一次修改：即使回退后文件回到原来的内容，同一条原话也不能再次授权（`source_reused`）；同一命令的重试用相同的 `command_id`，按幂等返回原结果。修改前文件的摘要由 `config_show` 给出；文件不存在时为 `"none"`。

权限判定按键的路径段进行，不按拼接后的文本：仓库 ID 含点号（如 `repositories["my.repo"]`）时，同样能识别其下的 `delivery` 等键。

其余键主会话可以直接修改，事件中记录理由。

### 7.3 边界

- worker 调用这些工具一律以 `role_denied` 拒绝（工具只注册给主会话）；没有调度租约的主会话以 `lease_held` 等拒绝。
- hooks 照旧拒绝主会话用编辑类工具或命令行写入 `config/`，拒绝信息指向 `config_change`。
- 用户手工编辑的文件是下一次修改的基准，事件中记录基准摘要。
- 生效方式不变（§1 第 5 条）：只影响之后创建的会话和之后的调用。
