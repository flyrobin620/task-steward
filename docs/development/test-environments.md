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

- 每次运行有一个运行 ID：`live-<时间戳>-<随机后缀>`。运行目录 `.local/live/<runId>/`（不提交）中放：
  - `home/`：本次运行专用的数据目录；
  - `repo/`：本次运行专用的临时目标仓库，没有远端；
  - `manifest.json`：本次运行创建的每个会话、线程、worktree。
- 先写清单再创建：每个会话或线程在请求创建前以“可能已提交”状态写入清单，拿到回执后更新。结果未知时
  按清单中的会话名或线程名对账，不重复创建。
- 运行结束时（包括失败）清理清单中的条目：归档本次创建的 Claude 会话与 Codex 线程，并用
  `get_session` / `thread/read` 读回确认。进程退出、`finally` 执行或一次空列表都不等于清理完成；
  无法确认的条目报告为清理失败，并给出剩余对象。
- 只处理清单中的对象，绝不归档、停止或修改清单之外的会话、线程和目录。
- live 运行不使用用户真实的数据目录，不连接真实业务仓库，不推送任何内容。
- 费用：`reviewer` 场景每次运行的模型调用次数写在场景定义中；超出时停止并报告，不自动重试。
