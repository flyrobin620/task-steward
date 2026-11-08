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
- 宿主会话的隔离：用户设置给每个 Claude Code 会话 `TSW_HOME`。`host-e2e` 在运行的数据目录与预建的执行
  目录中写 `.claude/settings.local.json`：用项目设置把 `TSW_HOME` 指向本次运行，挂上记录 hook payload
  的钩子，并让本检出的 hooks 守卫宿主插件缓存副本还看不到的工具（例如定时工具）。这些文件只为隔离，不含
  权限规则：tsw 不在执行目录中写权限规则，worker 的放行来自用户设置中的 auto 模式规则（与真实数据目录
  相同）；也因此预建的执行目录仍会要求信任确认，“新任务不弹信任确认”只能在真实数据目录中观察。项目
  设置里的 `env.PATH` 到不了会话的 shell，所以任务书用完整路径调用 `bin/gh`（hooks 仍按程序名 `gh`
  检查合并授权与目标仓库）；替身还模拟主分支上合并提交的 CI（`gh run list --commit`，结论为
  `success`，模拟证据）。
- 先写清单再创建：每个会话或线程在请求创建前以“可能已提交”状态写入清单，拿到回执后更新。结果未知时
  按清单中的会话名或线程名对账，不重复创建。
- 运行结束时（包括失败）清理清单中的条目：归档本次创建的 Claude 会话与 Codex 线程，并用
  `get_session` / `thread/list`（已归档）读回确认。Claude 会话只能在 app 中归档：操作者归档并读回后用
  `live.py cleanup <runId> --archived <会话 ID>` 记入清单；Codex 线程由 `cleanup` 归档并读回。进程退出、
  `finally` 执行或一次空列表都不等于清理完成；无法确认的条目报告为清理失败，并给出剩余对象。清理完成
  后才能用 `live.py discard <runId>` 删除运行目录。
- 只处理清单中的对象，绝不归档、停止或修改清单之外的会话、线程和目录。
- live 运行不使用用户真实的数据目录，不连接真实业务仓库，不向任何网络远端推送。
- 费用：`reviewer` 场景每次运行的模型调用次数写在场景定义中；超出时停止并报告，不自动重试。
  `host-e2e` 的会话与评审轮次上限也写在场景定义中，报告附费用估算。
