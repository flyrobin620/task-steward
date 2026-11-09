# 规则变更记录

修改 `AGENTS.md`、子目录中的 `AGENTS.md` 或任何规则文档，或者把规则移到别处之前，读本文。

- 在规则所在位置修改，不追加带日期的“取代”段落。
- 每次变更都记录在下表：日期、变更、原位置、新位置、原因。规则不能在缩短文件时悄悄消失。
- `AGENTS.md` 不超过 150 行、10 KB。只与某类工作有关的内容移到专题文档，并在第 2 节的表中加一行。
- Markdown 链接不会让 agent 读取被链接的文件，只有第 2 节的表会。因此每个专题文档都要在那张表中有
  触发行。
- 子目录中的 `AGENTS.md` 按目录生效，而不是按任务。CI、兼容性这类横跨多个目录的主题放在专题文档里，
  用意图触发。
- `CLAUDE.md` 只引用 `AGENTS.md`，`.github/copilot-instructions.md` 只指向它；规则不在这两个文件中重复。

| 日期 | 变更 | 原位置 | 新位置 | 原因 |
| --- | --- | --- | --- | --- |
| 2026-11-05 | 初始开发规则 | — | `AGENTS.md` 与 `docs/development/*` | 进入 v1 开发前制定，见 [0001](../decisions/0001-development-process.md) |
| 2026-11-06 | 编排器派发的任务由 worker 自己合并，编排者合并后核对；敏感改动类别全部保留，用户确认由 worker 在自己的会话中取得 | `AGENTS.md` 第 4 节第 6 步、`workflow.md`、`review.md`（编排者验收后合并） | 同一位置 | 用户决定，见 [0002](../decisions/0002-worker-merges.md) |
| 2026-11-07 | live 场景的入口 `tools/live.py` 与实际用法：运行目录可放到数据目录之外但不能在启动它的会话的数据目录中；目标仓库的远端是运行目录中的裸仓库，GitHub 由替身模拟（模拟证据）；`host-e2e` 由用户在 app 中按 `OPERATOR.md` 操作后 `verify`；Claude 会话由操作者归档后 `cleanup --archived` 记入清单；`reviewer` 中未实现的评审器报告为未验证 | `testing-layers.md`、`test-environments.md` 的 live 部分 | 同一位置 | T241 真机运行，用户 2026-11-07 决定 claude-headless 拆为后续任务 |
| 2026-11-09 | live `host-e2e` 的执行目录不再写任何设置文件，运行的数据目录以 `claim_sessions` 让 worker 找到它；`prepare` 先检查已安装插件所用的运行时认识 `claim_sessions` | `test-environments.md`（执行目录写 `settings.local.json`） | 同一位置 | 带设置文件的文件夹会再次要求信任（T245 live 实测），用户要求改为可选参数覆盖 `TSW_HOME` |
| 2026-11-08 | 合并授权不再限于开发任务；合并后的 `main` CI 与需要合并后运行的 live 由 worker 完成，编排者只核对证据；须用户确认才能合并的类别缩小为规则 / 决定与设计文档，一个任务确认一次（增删护栏规则、改变已确认决定除外） | `AGENTS.md` 第 4 节第 6、7 步、`workflow.md`、`review.md` | 同一位置（第 4 节新增“合并后”一步） | 用户决定，见 [0003](../decisions/0003-worker-delivery-loop.md) |
| 2026-11-08 | 设计文档分两层：`DN*` 只写与宿主无关的原则与设计，宿主实现放在 `docs/design/<宿主>/`（`claude-code/CC*`、`codex/CX*`），编号与所实现的 DN 相同 | — | `docs/development/docs.md`“设计文档”、`AGENTS.md` 开头 | 用户在 T242 中要求原则与 Claude 实现分开存放 |
| 2026-11-09 | 开发期插件清单与市场条目不写 `version`；产品更新后运行 `tsw host render`，它幂等地运行宿主的插件更新（`claude plugin update`）并报告副本是否一致 | — | `release.md`“当前状态” | T247：固定的 `0.1.0-dev` 让已安装的副本停在第一次安装时；用户决定由 host render 包装各宿主的更新 |
| 2026-11-09 | 新增 live 场景 `routine-e2e`（例行会话、超时转交与主会话代答、经 Teams 转达与答复登记）；超时转交从 `host-e2e` 的覆盖范围中移出 | `testing-layers.md` 的场景表（`host-e2e` 含超时转交） | 同一位置 | T243：超时转交改由例行会话完成，用户 2026-11-09 确认例行表设计 |
| 2026-11-09 | `routine-e2e` 增加检查 W：主会话是否被例行会话的定时运行唤醒改由主会话的 hook 记录判定，操作者不再回答 `--woken-by-ticks`（保留为只作记录） | `testing-layers.md` 场景表（检查 O 含操作者观察的唤醒） | 同一位置 | T250：T243 的检查 O 依赖操作者观察漏判了每次运行的唤醒；用户 2026-11-09 确认方案 |
