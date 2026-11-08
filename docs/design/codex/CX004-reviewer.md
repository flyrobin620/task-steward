# CX004 Codex 评审器

- 状态：设计草案（T242 r2 自 DN004 拆出，待用户确认）
- 日期：2026-11-08
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md)

## 1. 范围

本文是 DN004 评审器在 Codex 上的实现：默认会话适配器 `codex-app-server` 与回退 `codex-exec`。评审包、输出契约、处置规则与轮次计数见 DN004。

## 2. 主路径：App Server 一次性线程

沿用 orchestrator-agent `agent oneshot` 已验证的方式，移植为 tsw 的 reviewer 适配器：

1. 在本机临时启动私有 `codex app-server --listen ws://127.0.0.1:<随机端口>`，结束后结束整个进程树。
2. `thread/start`：`model = roles.reviewer.model`（`gpt-6-astra`），`model_reasoning_effort = roles.reviewer.effort`（`medium`），`sandbox = read-only`，`cwd` = 执行目录，`model_context_window = roles.reviewer.context_window`，`allowProviderModelFallback = false`。
3. `turn/start` 发送评审包；等待 `turn/completed`，超时（`timeout_seconds`）则中断并记为失败。
4. 读回 finalMessage，解析 JSON。
5. 首轮保留线程（keep thread），线程 ID 记入 `review.started`。后续轮次用 `resume_thread` 续用同一线程做增量评审；最后一轮结束后归档线程。

模型不可用、effort 不支持或返回的模型与请求不一致时，直接判为本轮失败，**不回退到其他模型**。

## 3. 回退：`codex exec`

App Server 无法启动时，用 `codex exec` 非交互执行同一评审包（指定模型、effort、`--sandbox read-only`），输出同样按 schema 解析。回退本身记录在 `review.completed.data.transport` 中。回退也失败时记录 `review.unavailable` 交主会话判断（可稍后重试或交给人类）：**不能跳过评审，也不能改用 Claude 子代理冒充 Codex 评审**。

## 4. v1 实现细节

- `codex-app-server` 通过 stdio 与 `codex app-server` 通信（不另开端口）。线程在同一周期的各轮之间保留，下一轮按 `review.incremental = resume_thread` 续用上一轮的线程；归档留给 attempt 结束后的清理。评审器进程在 Windows 上挂起启动、放入关闭即终止成员的 Job Object 后再恢复（其他平台为独立进程组），无法放入 Job Object 时在它运行之前结束并报告评审器不可用；结束时关闭输入、等待片刻，然后无论直接子进程是否已退出都结束整个进程树，输出管道只由读取线程关闭。
