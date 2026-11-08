# CC006 Claude Code 插件

- 状态：设计草案（T242 r2 自 DN006 拆出，待用户确认）
- 日期：2026-11-08
- 实现：[DN006 插件层](../DN006-plugin-layer.md)
- 相关：[CC001 Claude Code 上的会话与 hooks](CC001-sessions-and-hooks.md) · [CC004 Claude Code 评审器](CC004-reviewer.md) · [CC008 Claude Code 上的护栏](CC008-guardrails.md)

## 1. 范围

本文是 DN006 插件层在 Claude Code 上的实现：宿主集成 `claude-code`、会话适配器 `claude-app` 与 `claude-headless`、`in-session` 渠道的记录方式。插件接口与能力声明见 DN006。


与 Claude Code 耦合的设计元素及其归属的插件类型：

| 设计元素 | Claude 实现 | 归属 |
| --- | --- | --- |
| 主会话与 worker 的指令、skill、工具注册 | `CLAUDE.md`、`.claude-plugin`、skills、`.mcp.json` | P1 |
| 写入范围、危险命令拦截、结束前检查 | Claude Code hooks | P1 |
| 创建、续用、停止、观察 worker 会话 | `start_session`、`SendMessage`、`list_sessions`、`get_session`、`stop_session` | P2 |
| 唤醒主会话 | worker→父会话 `send_message(session_id="parent")`、side-sessions-event | P2（唤醒能力） |
| 派发窗口与预启动（G-WINDOW、attempt `standby`） | 只有用户本人的 turn 能在任务目录创建会话；任何 turn 都能给已有会话发消息 | P2（能力声明） |
| 权限模式名称 | `default` / `acceptEdits` / `auto` / `bypassPermissions` | P2（映射表） |
| 评审器 | Codex App Server 一次性线程 | P2 的 oneshot 能力 + `roles.reviewer.session` |
| 待答问题的投递与回收 | 用户在子会话直接答复 | P3 |
| 控制根 = 私有 Git 仓库 | ledger、runs 随仓库提交 | P4 + 数据目录 |

## 2. 宿主集成 `claude-code`

`claude-code` 的插件按用户级启用，任何目录的会话（主会话、`~/tsw-worktrees` 或 `work/<任务>/<attempt>` 中的 worker、无关会话）都会加载它，所以插件只携带每个会话都需要的 MCP 注册与 hooks。主会话的流程说明不放进插件：`render_instructions` 把 `CLAUDE.md` 和 `orchestrate` skill 写进数据目录，后者是 Claude Code 的项目 skill（`<cwd>/.claude/skills/`），只有以数据目录为工作目录的会话会发现它。Claude Code 也从上级目录加载 `CLAUDE.md` 与项目 skill，执行目录在数据目录之下（`work/<任务>/<attempt>`）的 worker 同样会看到二者，所以二者都写明：`whoami` 的角色不是 orchestrator 时忽略。skill 的来源在产品代码中（`tsw/host/orchestrate_skill.md`），产品更新后重新运行 `tsw host render`。

插件由 Claude Code 复制到插件缓存运行，缓存里没有 `tsw` 运行时。启动器按顺序查找：`TSW_REPO` 环境变量、插件所在的产品仓库（源码树或原地使用的本地市场）、`tsw init` 写入数据目录标记文件的 `product_repo`；都找不到时 MCP server 报错退出，hooks 静默放行。

规则执行：hooks 调用 `tsw guard`，按 [CC008](CC008-guardrails.md) 的 block list 拦截已知的错误动作；tsw 已把关的动作由生成的允许规则放行（CC008 §6）。

## 3. 会话适配器

| 适配器 | 用途 | 做法 |
| --- | --- | --- |
| `claude-app` | 主会话与 worker（interactive、delegated） | 创建、投递、停止、观察、对账分别用 app 的 `start_session`、`SendMessage`、`stop_session`、`get_session`、`list_sessions {linked:true}`；创建参数与平台约束见 [CC001 §3–§4](CC001-sessions-and-hooks.md)；中性权限映射 ask→default、edits→acceptEdits、auto→auto、full→bypassPermissions |
| `claude-headless` | 无 Codex 时的评审器（oneshot、direct） | `claude -p` 严格只读，调用约定见 [CC004](CC004-reviewer.md) |

`claude-app` 的 interactive 动作：

| 动作 | `claude-app` |
| --- | --- |
| create | `start_session`（fresh、cwd、use_worktree=false；预启动时 prompt 为待命说明） |
| deliver_brief | `SendMessage`（任何 turn 都可以；直接启动时由 `start_session` 的短指令 prompt 指向任务书路径，两阶段合一；正文不放进 prompt，见 [CC008 §6](CC008-guardrails.md)） |
| send | `SendMessage` |
| stop | `stop_session` |
| observe | `get_session` |
| find | `list_sessions {linked:true}` 按名称 |
| 唤醒主会话 | push：worker `mcp__ccd_session_mgmt__send_message(session_id="parent")`；兜底 side-sessions-event |

## 4. `in-session` 渠道

worker 有待答问题时，`UserPromptSubmit` hook 把用户消息记为 `input.session_message`（DN006 §8.1a）。哪些消息记录、哪些作为注入消息跳过，见 [CC008 §4](CC008-guardrails.md) 的 CC-S2。

## 5. 委托操作示例

派发 T241 的 worker（Claude app 宿主），DN006 §4 的委托操作：

```json
{
  "operation_id": "op-T241-a1-g1-start",
  "kind": "session.start",
  "task_id": "T241",
  "attempt": "a1",
  "generation": 1,
  "action": {
    "neutral": "start_worker_session",
    "tool": "mcp__ccd_session__start_session",
    "params": {
      "initiation": "user_asked",
      "context": "fresh",
      "cwd": "~/tsw-worktrees/example-app/T241-a1",
      "use_worktree": false,
      "name": "t241-a1-g1",
      "model": "claude-opus-5-5",
      "effort": "high",
      "permission_mode": "auto",
      "prompt": "按 runs/T241/a1/brief.md 执行。工具、评审与回传协议见 brief 第 1 节。",
      "background": "为 example-app 的报表页增加 CSV 导出：在任务 worktree 中实现服务端流式导出接口与界面上的导出按钮，支持十万行分页与导出链接过期，补齐单元测试与端到端测试，完成后用配置的评审器自评审，再按 brief 提交报告。"
    }
  },
  "expects_receipt": {"fields": ["session_id"], "on_unknown": "reconcile_by_name"},
  "expires_at": "2026-11-05T12:00:00Z"
}
```

回执：

```json
{"operation_id": "op-T241-a1-g1-start", "outcome": "ok", "result": {"session_id": "local_6c1f…"}, "evidence": "tool result"}
```
