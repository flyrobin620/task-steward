# 测试层级

写或改测试之前，或决定某个行为需要哪些层级之前，读本文。测试用到的目录、进程环境和 live 运行的隔离
见 [test-environments.md](test-environments.md)。

## 层级

| 层级 | 测什么 | 用什么 | 证据类型 | 在哪里跑 |
| --- | --- | --- | --- | --- |
| `unit` | 纯逻辑：状态迁移与守卫、归约、配置校验、评审预算、模型 ID 规范化等 | 进程内调用，不碰文件系统以外的资源 | 真实 | 本地门禁、CI |
| `component` | 真实文件系统、SQLite、git 与子进程上的行为：账本提交锁、检查点与投影、`rebuild`、备份恢复、写入闸门、worktree 创建；崩溃恢复与并发 | 临时数据目录与临时 git 仓库；多进程用确定性屏障；故障注入点 | 真实；故障注入单独标注 | 本地门禁、CI |
| `surface` | 对外表面：`tsw` CLI、stdio MCP server、hooks、插件包结构 | CLI 作为独立进程运行，分别断言退出码、stdout、stderr；MCP 经真实 stdio 与 JSON-RPC 序列化；hooks 以独立进程读取录制的真实 payload | 真实（payload 为录制样本） | 本地门禁、CI |
| `conformance` | 每类插件接口的一致性套件（DN006 §5.2）：幂等、结果未知时对账、错误码、能力声明 | 对每个插件实现运行同一套件；本仓库之外的平台用 stub 或录制回放替代 | 模拟（stub / 录制回放） | 本地门禁、CI |
| `live` | 真实宿主与真实评审器：Claude 桌面 app 中的主会话与 worker 全流程、`codex app-server` 与 `claude -p` 只读评审 | 本机真实 app、真实 CLI 与模型；隔离的临时数据目录与临时仓库 | 真实 | 只在本机，绝不在 CI |

- 每次汇报都写明运行了哪些层级（数量与结果），以及没有运行的层级和原因。
- 真实、模拟和故障注入的证据分开写。替代外部平台的 stub 或录制回放属于模拟证据，不能写成真实证据。
- 不为本地测试另写一套生产行为的实现（例如假的账本、假的派发器）。测试真实组件，只替换本仓库之外的
  平台：Claude app 的会话工具、`claude` 与 `codex` 命令行、模型调用。
- surface 测试必须经过独立进程和真实序列化；在进程内直接调用命令处理函数属于 unit 测试。
- 每个 use case 需要哪些层级由 [use-cases.md](use-cases.md) 规定。

## live 场景

live 层级调用真实 app 与模型，有费用，并且需要用户在场（新目录的信任确认只能在用户看着主会话时弹出）。
以下改动必须在最终 head 上运行对应场景，并在 PR 中附证据：

| 改动范围 | 场景 |
| --- | --- |
| P1 宿主集成（插件清单、hooks、MCP 注册、角色解析、worker 指令、生成的权限规则） | `host-e2e`：预启动、派发、worker 自评审、会话内提问、worker 合并、提交、编排者验收、取消与恢复 |
| 例行会话与例行表、超时转交、`teams-chat` 渠道（DN006 §8.7、§8.9） | `routine-e2e`：例行会话的启动、分离与定时任务、超时转交与主会话代答、经 Teams 转达与答复登记；空跑不唤醒主会话由主会话的 hook 记录判定（检查 W） |
| 调度租约的交接与唤醒目标（DN001 §6.3–§6.4：让出、领取、worker 与例行会话发给持租主会话） | `handoff-e2e`：主会话 A 派发 worker 后让出租约，B 领取；worker 的唤醒消息到达 B 而不是 A，B 按会话 ID 回复 worker |
| P2 `claude-app` 适配器（会话创建、对账、投递、唤醒） | `host-e2e` |
| P2 评审器适配器（`claude-headless`、`codex-app-server`）或评审协议（DN004） | `reviewer`：每种已实现的评审器在固定样例上各做一次全量评审与一次增量评审，核对只读（目标目录无写入）与输出 schema；未实现的评审器报告为“未验证” |
| 发布前 | 全部场景 |

不能运行时（例如用户不在场），PR 中把对应层级写为“未验证”并说明原因；需要用户确认的合并不能以此跳过。

运行方式：

| 场景 | 命令 | 用户 |
| --- | --- | --- |
| `reviewer` | `python tools/live.py reviewer` | 不需要；结束时自动清理线程 |
| `host-e2e` | `python tools/live.py host-e2e prepare`，用户按运行目录中的 `OPERATOR.md` 在 app 中操作，再 `python tools/live.py host-e2e verify <runId>` | 需要：以运行的数据目录新建主会话（信任文件夹）、发出操作步骤中的消息、在 worker 会话中回答问题 |
| `routine-e2e` | `python tools/live.py routine-e2e prepare`，按 `OPERATOR.md` 在 app 与 Teams 中操作，再 `python tools/live.py routine-e2e verify <runId>` | 需要：新建主会话、不在 worker 会话中回答、在 Teams 自聊中以任务 ID 开头回复 |
| `handoff-e2e` | `python tools/live.py handoff-e2e prepare`，按 `OPERATOR.md` 在 app 中操作，再 `python tools/live.py handoff-e2e verify <runId>` | 需要：先后新建两个主会话、在 worker 会话中发一条消息 |
| 收尾 | `python tools/live.py cleanup <runId> --archived <会话 ID>…`，`python tools/live.py discard <runId>` | 在 app 中归档本次运行的会话 |

`verify` 逐项核对隔离账本中的事件（场景定义的 `checks`），把结果写进运行目录的 `result.json` 与可附在
PR 中的 `evidence.md`，并分开标注模拟部分（GitHub 替身）。

## 编写测试

- 竞争的操作先全部启动再一起等待；冲突后从存储中读回权威状态。
- 不在固定延迟后断言。用带截止时间的轮询，或确定性屏障与可控时钟。
- 不重试失败的断言直到通过。修好不稳定的测试，或带负责人、issue 与到期日隔离；被隔离的测试不能是
  某个必需层级的唯一覆盖。
- 测试自己创建临时目录、git 仓库和数据，不依赖其他测试的残留，也不依赖执行顺序。
- 故障注入点由核心代码提供，只在测试环境变量开启时生效，关闭时不改变任何行为。
- 断言 use case 承诺的输出（账本事件、投影、文件、退出码），不只断言“成功”。
