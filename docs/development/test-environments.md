# 测试环境与隔离

创建、修改或清理测试用到的数据目录、git 仓库、子进程环境，或修改 live 场景脚本之前，读本文。

## CI 层级（unit / component / surface / conformance）

- 每个测试有自己的临时数据目录（`TSW_HOME`）和临时 git 仓库。测试夹具在创建时检查 `TSW_HOME` 位于
  本次运行的临时根目录下，否则立即失败。
- 启动子进程时使用干净的环境：
  - 删除继承的 `CLAUDE*`、`ANTHROPIC*`、`CODEX*` 变量（PoC 发现宿主变量会改变 `claude` 命令行的认证
    行为）；
  - 把 `HOME`、`USERPROFILE`、`CLAUDE_CONFIG_DIR`、`CODEX_HOME` 指向临时目录，确保读不到用户真实配置；
  - 需要会话 ID 等宿主变量时由测试显式设置（例如 `CLAUDE_CODE_HOST_SESSION_ID`）。
- 不联网，不调用模型，不访问 GitHub。临时 git 仓库没有远端，测试中不执行 push。
- MCP server 与 hooks 走 stdio，不占用端口；需要端口时动态分配，不用固定端口。
- 并发运行的测试不共享进程级环境变量、锁文件或数据目录。
- 测试进程不刷盘：`tests/conftest.py` 把 `os.fsync` 换成空操作，并给每个 SQLite 连接设
  `synchronous = OFF`。刷盘只在机器断电时才有区别，测试不模拟断电；而几个并行的测试进程同时刷盘时，
  CI runner 上每次刷盘要排队等磁盘（T317 实测慢约 30 倍）。这替换的是平台的落盘保证，tsw 的代码路径
  照常运行；测试启动的子进程（`tsw`、MCP server、hooks）照常刷盘。
- 录制的平台样本（hooks payload、`codex app-server` JSON-RPC 记录、`get_session` 回执）放在
  `tests/fixtures/`，提交前去除会话 ID 以外的个人信息（用户名、本机路径、主机名）。

## live 层级

入口是 `python tools/live.py`，场景定义在 `tools/live/scenarios/*.json`（`useCases`、检查项、模型调用上限、
操作步骤）。

- 每次运行有一个运行 ID：`live-<时间戳>-<随机后缀>`。运行目录默认在 `.local/live/<runId>/`（不提交），
  可以用 `--runs-dir` 或 `TSW_LIVE_RUNS` 放到别处，但不能放在启动它的会话的数据目录（`TSW_HOME`）之中：
  在那里打开的会话会读到该数据目录的 `CLAUDE.md`，就不再隔离（`live.py` 拒绝这样的位置）。运行目录中放：
  - `home/`：本次运行专用的数据目录；
  - `repo/` 与 `origin.git/`：本次运行专用的临时目标仓库，唯一的远端是同一运行目录中的裸仓库；
  - `bin/gh`：GitHub CLI 的替身（`tools/live/gh_stub.py`），在 `origin.git` 上模拟 PR 与 squash 合并，
    状态在 `gh-state.json`，调用记录在 `gh-log.jsonl`。经替身得到的 PR 与合并是模拟证据；
  - `manifest.json`：本次运行创建的每个会话、线程、目录；`result.json` 与 `evidence.md`：检查结果
    （`evidence.md` 已去除本机路径与用户名，可附在 PR 中）；`hook-payloads.jsonl`：真实 app 发出的 hook
    payload（可去除个人信息后作为 surface 测试的录制样本）。
- 宿主会话的隔离：用户设置给每个 Claude Code 会话 `TSW_HOME`。`host-e2e` 用
  `tsw init --claim-sessions` 建运行的数据目录：在其中工作的会话（主会话与 `work/` 下的 worker）一律使用
  它，不看 `TSW_HOME`，执行目录里因此不需要任何设置文件。只有运行的数据目录本身（主会话所在、操作者打开
  时就要信任的文件夹）写 `.claude/settings.local.json`：挂上记录 hook payload 的钩子，让本检出的 hooks
  守卫宿主插件缓存副本还看不到的工具（例如定时工具），并以 `permissions.defaultMode: auto` 让在那里打开的
  会话以 auto 模式开始（deeplink 设不了权限模式；检查 A 从第一条用户消息的 hook payload 核对）。worker 会话找到运行时靠真实数据目录记录的
  `product_repo`，所以 `prepare` 先检查那个检出已认识 `claim_sessions`，否则拒绝（worker 会落到真实数据
  目录）。tsw 不在执行目录中写
  任何设置，worker 的放行来自用户设置中的 auto 模式规则（与真实数据目录相同）。Claude 桌面 app 对带
  `.claude/settings.local.json` 的文件夹（即使只有 `env` 与 `hooks`）会再次要求信任（2026-11-09 live 实测，
  确认框写明 “Execution allowed by: .claude/settings.local.json”），所以执行目录必须没有这个文件；运行中
  worker 会话创建时是否弹出信任确认，就是“新任务不弹信任确认”的检验。在场的操作者把观察到的信任确认与
  权限拦截在 `verify` 时用 `--trust-prompted` / `--blocked` 记下（操作者观察，不是自动检查）；无人观察时
  不带这些参数，对应检查记为未验证。项目
  设置里的 `env.PATH` 到不了会话的 shell，所以任务书用完整路径调用 `bin/gh`（hooks 仍按程序名 `gh`
  检查合并授权与目标仓库）；替身还模拟主分支上合并提交的 CI（`gh run list --commit`，结论为
  `success`，模拟证据）。
- 先写清单再创建：每个会话或线程在请求创建前以“可能已提交”状态写入清单，拿到回执后更新。结果未知时
  按清单中的会话名或线程名对账，不重复创建。
- 运行结束时（包括失败）清理清单中的条目：归档本次创建的 Claude 会话与 Codex 线程，并用
  `get_session` / `thread/list`（已归档）读回确认。Claude 会话由运行 live 的会话用宿主的会话工具归档：
  `live.py <场景> sessions <runId>` 列出在运行目录中工作的会话（并把清单还没有的补记进去），对每个未归档
  的会话 `archive_session`，读回后用
  `live.py cleanup <runId> --archived <会话 ID>` 记入清单；Codex 线程由 `cleanup` 归档并读回。进程退出、
  `finally` 执行或一次空列表都不等于清理完成；无法确认的条目报告为清理失败，并给出剩余对象。清理完成
  后才能用 `live.py discard <runId>` 删除运行目录。
- 例行会话带着会话内的定时任务，app 不让会话工具直接归档它（“still has live work”）。收尾时先
  `live.py <场景> retire-routine <runId>`：它在隔离数据目录中关闭例行会话并给出例行会话的 ID；运行 live
  的会话给它发消息“运行 routine_board”，它删除定时任务并告诉隔离的主会话，主会话归档它（CC006 §6.6）；
  再 `retire-routine <runId> --wait` 等到账本中有 `routine.archived`。超时时按它的提示由运行 live 的会话
  自己归档并登记。
- 运行 live 的会话归档本运行的会话、给例行会话发那一条消息，在 auto 模式下由分类器判断。`archive_session`
  自己的说明是“只在用户明确同意归档某个会话之后调用”；分类器把用户在本会话中没有要求的归档判为越权：
  T341 的 worker 归档 `repo/` 信任会话被拒（[Unauthorized Access]），归档本运行的主会话被拒
  （[Unsanctioned Communication]）；T340 的 worker 归档主会话与 worker 放行；T345 中用户在本会话说“允许”
  之后，归档本会话启动的探测会话放行（实测）。由此推断：放行取决于会话记录中有没有用户本人的授权，
  worker 的任务书不是用户的话；同一动作在不同运行中结果不同（推断，没有逐项实测）。所以开发者在自己的
  用户设置中加入 `python tools/live.py auto-mode-rule` 打印的规则“TaskSteward Live Cleanup”（用户设置的
  `autoMode.allow`；live 工具不写 `~/.claude`）：它只覆盖 `sessions` 为本运行列出、目录在本运行目录中的
  会话的归档，以及按 `retire-routine` 给本运行例行会话发的那一条消息。没有加规则时，运行 live 的会话在
  收尾前请用户在本会话中明确同意归档本运行的会话，分类器仍拒绝时由用户在侧栏归档。T345 的 host-e2e
  在用户加入这条规则后运行：运行 live 的 worker 会话归档 `repo/` 信任会话、主会话与两个 worker 都放行，
  例行会话由本运行的主会话在它删除定时任务后归档，用户在收尾中没有操作（实测）。
- 只处理清单中的对象，绝不归档、停止或修改清单之外的会话、线程和目录。
- live 运行不使用用户真实的数据目录，不连接真实业务仓库，不向任何网络远端推送。
- 费用：`reviewer` 场景每次运行的模型调用次数写在场景定义中；超出时停止并报告，不自动重试。
  `host-e2e` 的会话与评审轮次上限也写在场景定义中，报告附费用估算。

### 操作者步骤

需要操作者的场景（`host-e2e`、`routine-e2e`、`handoff-e2e`、`change-e2e`、`title-e2e`）在场景定义的
`operator` 中逐步写明谁做：`user`（只有用户本人能做，`why` 写明原因）、`session`（运行 `live.py` 的会话）、
`script`（`live.py` 的命令）。确定的步骤由脚本做，不交给模型判断；`prepare` 生成的 `OPERATOR.md` 先列用户
要做的步骤与原因，再列会话与脚本做的事。替用户做事的手段：

- `live.py <场景> open <runId> <链接>`：用 deeplink（`claude://code/new?folder=…&q=…`）在 app 中打开以运行
  目录中的文件夹为目录、第一条消息已填好的新会话，用户只需点发送（首次打开时信任文件夹）。app 的
  `code/new` 只读 `q`（或 `prompt`）、`folder`、`file`、`environment`、`runner_pool`：不能设权限模式，也
  不会自动发送（桌面 app 6.28899 的链接处理代码，2026-11-22 核实）。
- `live.py <场景> drive <runId>`（在后台运行）：按场景的 `drive` 规则，在账本出现规则等待的事件后，经本机
  workdeck 把场景写好的话发到本运行的会话（回答问题、验收、追加要求、派发与暂停等），或打开下一个链接。
  workdeck 发来的消息就是用户本人的话（DN008 §2、DN006 §8.11），其他会话发的消息不是，所以不能改由会话的
  `send_message` 代发。drive 每发一条消息都启动一个只在本机回环地址上服务（`--no-gateway`，不登录、不连
  网关）、只允许给目标会话投递（`messaging.allowSessions`）的 workdeck 守护进程，并核对目标会话在运行目录
  中工作；投递结果未知时不重发（与 workdeck 门户相同），停下并报告。记录在运行目录的 `drive.jsonl`。
- `live.py <场景> sessions <runId>`：经同样的只读守护进程（不允许投递给任何会话）列出在运行目录中工作的
  会话：会话 ID、角色、状态、权限模式与标题；用于找到信任用的会话、核对权限模式与收尾归档。
- live 工具不读会话记录（`~/.claude` 中的 transcript）：测试与工具不读 `~/.claude`，在 auto 模式下读取
  其他会话的 transcript 也会被分类器拒绝（2026-11-22 实测）。只显示在 app 或 Teams 界面上的现象仍是
  操作者观察，无人观察时记为未验证。

清点（T340；“之前”指 T332 的两次 `host-e2e` 运行与各场景原来的 `OPERATOR.md`）：

| 场景 | 步骤 | 之前 | 现在 | 方法 | 不能自动化的原因与出处 |
| --- | --- | --- | --- | --- | --- |
| host、change、title | 信任 `repo/`（新建会话、选文件夹、发送、信任） | 用户全部手动 | 用户点发送并信任 | `open trust` | 信任确认是宿主要求用户本人的动作；deeplink 不自动发送（同上） |
| host、change、title | 归档信任用的会话 | 用户 | 运行 live 的会话 | `sessions`、`archive_session` | — |
| 全部 | 新建主会话、把权限模式设为 auto、发出第一条消息 | 用户全部手动 | 用户点发送（首次打开时信任） | `open main`；`permissions.defaultMode` | 主会话的第一条消息必须是用户的话：`start_session` 建的会话，第一条消息来自启动它的会话（CC001 §4、DN008 §2）；deeplink 不自动发送 |
| host | 回答 T001 的问题 | 用户点按钮 | drive | workdeck | — |
| host | 派发、暂停、恢复、取消 T003 | 用户在主会话中说 | drive | workdeck | — |
| host | 验收、把 follow-up 转成任务 | 用户（T332 第一次从 workdeck 门户发） | drive | workdeck | — |
| change | 在 worker 会话中追加要求；再次验收 | 用户 | drive | workdeck | — |
| handoff | 让 A 让出租约；在 worker 中“现在提问”；取消 T001 | 用户 | drive | workdeck | — |
| handoff | 新建主会话 B | 用户全部手动 | drive 在 A 让出后打开链接，用户点发送 | `open`（drive 规则） | 同“新建主会话” |
| routine | 不回答、等待超时 | 用户等待 | 用户等待（无需操作） | — | 这一步要验证的就是没有人回答 |
| routine | 在 Teams 自聊中回复 | 用户 | 用户 | — | 例行会话读取的是用户自聊中的新消息；由会话经 WorkIQ 代发需要用户另行同意，未固化 |
| routine | Teams 卡片是否分行显示（`--card-multiline`） | 用户观察 | 用户观察 | — | 只能在 Teams 客户端中看到 |
| title | 在 Claude 之外的终端运行 `tsw task accept` | 用户 | 用户 | — | `user-cli` 渠道的含义就是用户本人的终端（G-USER），由会话或脚本代为运行就不是 |
| title | 在侧栏手动改 T002 的标题 | 用户 | 用户 | — | 要验证的是主会话认出用户亲手改的标题；由会话代改测的就不是这条路径 |
| title | 在主会话中取消、归档 T002 | 用户 | drive | workdeck | — |
| routine | 在主会话中取消 T001、T002 | 用户 | drive | workdeck | — |
| title | 侧栏标题与改名确认（`--observed`） | 用户观察，必须填写 | 可选；无人观察时检查记为未验证 | — | 改名确认只显示在 app 界面上 |
| host、routine、handoff、change | 信任确认、权限确认与分类器拒绝（`--trust-prompted`、`--blocked`） | 用户观察，必须填写 | 可选；无人观察时检查记为未验证 | — | 只显示在 app 界面上，live 工具不读 transcript；无人值守时信任与权限确认会让会话停住，由账本检查间接覆盖 |
| 全部 | `verify`、`cleanup` | 运行 live 的会话（用户提供观察） | 运行 live 的会话 | `live.py` | — |
| 全部 | 归档本次运行的会话 | 用户 | 运行 live 的会话 | `sessions`、`archive_session`（归档主会话时连带归档它启动的 worker） | — |
| 启动了例行会话的场景 | 归档例行会话 | 用户（侧栏） | 运行 live 的会话与隔离的主会话 | `retire-routine`（关闭例行会话、等 `routine.archived`）；例行会话按 `disarm` 删除定时任务后由主会话 `archive_session` | 带定时任务时 app 拒绝 `archive_session`，删除后放行（T345 实测） |
