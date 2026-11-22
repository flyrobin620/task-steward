---
name: tsw-demo
description: "TaskSteward 新手导览与示例任务：主会话发现账本中还没有任何任务（新安装的环境）时，或用户说“开始示例任务”、要求演示或试用 TaskSteward 时使用。先给用户导览，再提议登记一个示例任务：它不关联真实仓库，报告记录这个任务自己的执行过程（状态机、向用户提问与超时转交、与评审器的互动、follow-up），最后引导用户在会话与 Dashboard 中看报告并验收。只用于在数据目录打开的主会话。"
---

# TaskSteward 新手导览与示例任务

本 skill 只适用于主会话（`whoami` 的角色为 orchestrator）。设计见产品仓库 DN009 §15。示例任务是一个普通的真实任务：登记、规划、派发、验收都按 `orchestrate` 与 `task-intake` skill 做，本 skill 只规定导览的内容、任务的规格和每一步要告诉用户的话。任务规格照下文原样登记，不自己改写或增删。

## 1. 导览（新环境，或用户要求）

`task_list` 为空（新安装的环境）时，在回答用户这一轮的请求之前先给导览；用户说“开始示例任务”时简短导览后直接进入第 2 节。导览用几段话讲清楚，不要长篇：

1. **你在哪里**：这个会话在数据目录中打开，是 TaskSteward 的主会话：它受理、规划任务，把任务派发给 worker 会话（侧栏中可见），处理它们的回传，核验后交给你验收。以后开主会话也是这样：在数据目录新建会话，输入 `/orchestrate`；旧的主会话还持有调度租约时新会话只读，先在旧主会话中让它交接（`orchestrate` skill 的“交接主会话”），旧会话已不在时按同一节接管。
2. **状态都在账本中**：任务、问题、评审、验收都记在数据目录的账本里，会话结束或重启都不会丢；Dashboard 是账本的视图。
3. **当前配置**（`config_show` 读出后列出）：交流语言 `control.language`、评审器 `roles.reviewer`、需要你决定的事怎样找到你 `plugins.human_channels` 与超时 `input.in_session_timeout_minutes`、Dashboard 端口 `dashboard.port`。说明以后可以让主会话改（`config_change`，权限类的要你的原话）。
4. **Dashboard**：在 Bash 中运行只读的 `tsw dashboard status --json`。`installed` 为 true 时，直接给出可点击的链接（结果中的 `url`，例如 `http://127.0.0.1:47800/`），请用户点开，说明之后任务的进展都在那里看；页面要求登录时（第一次在这个浏览器打开、或令牌已失效），请用户在会话外的终端运行一次 `tsw dashboard open`。`installed` 为 true 而 `running` 为 false 时，先运行 `tsw dashboard restart`（主会话可以运行；它启动计划任务并等服务应答），确认 `running` 为 true 后再给链接；结果中有 `conflict`（端口上是另一个数据目录的服务）时告诉用户端口冲突，请用户改 `dashboard.port` 或停掉另一个服务，不给链接。没有安装常驻服务时，请用户在会话外的终端运行 `tsw dashboard`（它打开浏览器）。
5. **怎样开始真实工作**：告诉主会话要管理哪些仓库（本机路径、是否允许推送、开 PR、合并），它逐个加入配置；然后用一句话描述要做的事，它会登记任务、写任务书并派发。
6. **提议示例任务**：问用户要不要先跑一个示例任务（下一节），看一遍一个任务从登记到验收的全过程。用户不要时结束导览，之后随时说“开始示例任务”。

## 2. 开始示例任务前

用两三句话告诉用户将要发生什么，请用户确认（用 `wrap_up` 的提问与按钮）：

- 主会话会在配置中加入一个示例仓库条目 `tsw-demo`（不指向任何本机目录，不推送、不开 PR、不合并），登记一个示例任务并派发给 worker 会话；worker 写一份报告，内容就是这个任务自己的执行过程。
- 需要模型调用：一个 worker 会话若干轮，自评审开启时还有配置的评审器若干轮。先用 `config_show` 看自评审是否开启：`review.self_review`（复用已有的 `tsw-demo` 条目且它带 `self_review` 时以条目的为准，DN004 §9）。
  - 自评审关闭（`off`）时不核对评审器，告诉用户示例任务不做自评审，报告“与评审器的互动”一节只会写明未做（配置关闭）。
  - 自评审开启时核对配置的评审器 `roles.reviewer.session`：调用 `reviewer_models`（不给 `reviewer`，即当前配置的评审器）。它在本机找该评审器的命令行（`codex-app-server` 用 `codex`，`claude-headless` 用 `claude`，`copilot-cli` 用 `copilot`；配置了 `roles.reviewer.executable` 时用它）并列出可用模型，不调用模型。`available` 为 true 时可以开始；配置的模型不在 `models` 中（没有 `current` 为 true 的条目）时告诉用户评审可能因模型不可用而失败，问用户照常开始还是先换模型。
  - `available` 为 false（结果的 `error` 写明原因，例如找不到命令行），或 `reviewer_models` 拒绝了配置的评审器（不是它能列出的评审器，例如尚未实现的 `codex-exec`）时，worker 的自评审会失败，示例任务完成不了。此时不开始，把原因告诉用户，并说明两种处理：安装并登录该评审器的命令行后再说“开始示例任务”；或改用本机可用的评审器与模型：按 `orchestrate` skill 更换评审器的做法，先用 `reviewer_models`（可给 `reviewer`）列出选项给用户选，再以用户选择时的原话 `config_change`，也可以请用户在会话外的终端运行 `tsw reviewer set`。
  - 列出模型不能证明命令行已登录：未登录时第一轮自评审会失败，worker 会在它的会话中问用户怎样处理。
- 过程中 worker 会在它自己的会话里问用户一个问题；用户可以回答，也可以故意不答，看超时后的处理。
- 请用户打开 Dashboard（第 1 节第 4 条）：装了常驻服务时直接给出链接。

用户的确认就是加入 `tsw-demo` 条目的原话（新增仓库是权限类改动，DN003 §7.2）。用户没有明确同意时不开始。

## 3. 加入配置、登记与派发

用户确认后：

1. `config_show` 取 `file_sha256`；`config_change`：`ops` 为 `[{"op": "set", "path": ["repositories", "tsw-demo"], "value": {"delivery": {"push": false, "create_pr": false, "merge": false}}}]`（不写 `path`：tsw 不建 worktree，任务在数据目录的 `work/<任务>/<attempt>/` 中执行）；`reason` 写“示例任务的仓库条目（不指向本机目录、不交付）”；`user_source` 为 `{"channel": "main-session", "principal": "session-owner", "message_ref": "main:<whoami 的 session_id>:<确认时的 UTC 时间>", "text": <用户上一步确认的原话，原样>, "target": ["config", <file_sha256>, "authorize"]}`（主会话没有 hook 给的消息编号；不要编造原话）。先用 `config_show` 看有效配置（含本机覆盖）：已有 `tsw-demo` 条目、且它没有 `path`，`delivery` 的 `push`、`create_pr`、`merge` 都不是 true 时直接复用；同名条目指向真实仓库或允许交付，或者别的仓库的 `aliases` 中有 `tsw-demo` 时停下，告诉用户冲突在哪里，请用户自己处理（改名或改配置），不覆盖、不登记、不派发。
2. `task_register`：
   - `title`：`示例：TaskSteward 示例任务报告`
   - `repo`：`tsw-demo`
   - `kind`：`research`
   - `goal`：`写一份 TaskSteward 示例任务报告，记录本任务自己的执行过程，向第一次使用 TaskSteward 的用户展示它怎样执行一个任务。`
   - `acceptance`（逐条原样）：
     1. `开始写报告之前，用 ask_user 在本会话中问用户一个问题：“这份示例报告侧重哪一方面？A 给新用户的入门（怎样提出任务、在哪里看进度与验收）；B 内部机制（状态机、账本、评审与护栏）。”按用户的答复写报告；问题在用户回答前被主会话代答或转达时，按代答写，并在报告中如实记录`
     2. `报告“任务的一生”一节：简要解释任务状态机（待受理、等待依赖、就绪、派发中、执行中——其中 worker 的执行与自评审——、等待用户、主控核验、完成·待用户验收、已验收，以及退回、暂停、取消、归档），并按本任务的账本（context_task）列出它实际经过的状态与时间`
     3. `报告“向用户提问”一节：记录第 1 条的问题、何时提出、谁在何时回答（用户本人、主会话代答或迟到答复）；按配置说明（input.in_session_timeout_minutes，config 中的实际值）无人回答时会怎样：例行会话把问题转交主会话，主会话自己判断代答或经人类渠道转达，用户之后的答复记为迟到答复、以用户的话为准`
     4. `报告“与评审器的互动”一节：记录写这一节之前各轮自评审的评审器、轮次、意见（编号、严重程度、要点）与处置（修复及验证，或拒绝及理由）；说明评审器与预算由配置决定（roles.reviewer、review.max_rounds、review.block_on）。报告草稿写好后至少运行一轮 review_run；补写这一节后按 worker 说明补审差异，这次补审与之后的轮次不再写回报告（避免无尽循环），报告中写明“之后的评审轮次见 Dashboard 单任务视图”。任务书写明“自评审：关闭”时不运行 review_run，本节写明未做自评审（配置 review.self_review 为 off）以及开启后评审器与预算怎样决定`
     5. `用 follow_up_record 记一条 idea：“把示例报告做成一个交互式网页导览”，并在报告“follow-up 与偏好候选”一节说明 follow-up 与偏好候选是什么、用户在 Dashboard 的哪里看到它们`
     6. `报告最后一节“你接下来可以做什么”：在 Dashboard 中验收或退回本任务；在主会话中告诉它要管理哪些仓库，开始真实任务`
     7. `报告为中文（或用户在第 1 条答复中要求的语言），md 与 html 两份`
   - 已经登记过示例任务、它还在“待受理”时（例如更新 tsw 之前登记、规划被拒绝），不再重复登记，加入配置后对它继续 `task_plan`。
3. `task_plan`（理由写“示例任务，无依赖”），然后按 `orchestrate` skill 预启动或派发（这一轮是用户的 turn）。派发后告诉用户 worker 会话的名称，说明它会出现在侧栏中。

## 4. 执行中告诉用户的话

- 派发后：装了常驻服务时给出两个链接：看板（`<url>`）与这个任务的单任务视图（`<url>tasks/<任务>`，例如 `http://127.0.0.1:47800/tasks/T001`），请用户点开，在看板上看这个任务从“待受理”经“就绪”“派发中”到“执行中”，在单任务视图中看任务书、attempt 与会话链接（点击在 app 中打开 worker 会话）；之后每次提到进展（提问、评审、提交）都附上单任务视图的链接。没装常驻服务时请用户在 `tsw dashboard` 打开的页面中点任务卡片的「详情」。
- worker 提问时（`attention` 或 inbox 中出现它的问题，或用户问起）：提醒用户到 worker 会话中回答；也可以不答，配置的超时（`input.in_session_timeout_minutes`）之后例行会话把问题转交给你，届时按 `handle-input` skill 处理，并告诉用户这就是超时转交。
- 自评审进行时：告诉用户 Dashboard 的单任务视图会列出评审轮次。

## 5. 提交之后

1. 按 `orchestrate` skill 的“核验与验收”做技术验收（G-ACCEPT），核对报告包含第 3 节验收的每一条。
2. 在会话中用几句话概括报告（问题与答复、评审轮次与意见、经过的状态），给出报告路径 `runs/<任务>/<attempt>/result.md` 与 html。
3. 引导用户在 Dashboard 中（装了常驻服务时附上看板与单任务视图的链接）：
   - 看板：任务现在是“完成·待用户验收”；切换“按时间顺序”与“任务流”视图；
   - 单任务视图：任务书与 revision、attempt 与会话、评审轮次、问题与答复、follow-up、时间线，以及报告链接；
   - 页首的“长期偏好”：偏好候选在这里确认或不记；
   - 验收：在卡片或单任务视图中点“验收”（或先点“退回”写一句理由，看任务回到执行、worker 按理由修改后再提交，然后再验收）。
4. 用户验收后，告诉用户示例任务已完成；之后可以在主会话中告诉它要管理的仓库，开始真实的任务。示例任务与 `tsw-demo` 条目留着无害，默认都保留。用户要清理时，按 `orchestrate` skill 归档任务；只有用户明确要求删除 `tsw-demo` 条目、它仍是示例加入时的样子（没有 `path`、交付全关），并且没有其他未归档的任务使用它（例如示例的 follow-up 转成的任务）时，才经 `config_change`（`{"op": "unset", "path": ["repositories", "tsw-demo"]}`，附用户这次的原话）删除。
