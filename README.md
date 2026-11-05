# TaskSteward（任务管家）

一个可插拔宿主的 AI 任务编排器：主会话负责受理、规划、派发、核验与验收，每个任务由独立的 worker 会话执行；worker 完成后由评审器自评审并自行决定是否迭代，评审预算用完或遇到问题时先由主会话在授权边界内判断，确需人类时再经渠道请求。默认宿主是 Claude Code 桌面 app，也可以经插件层接入 Codex 或 GitHub Copilot。

当前阶段：产品设计。设计文档见设计分支的 `docs/design/`。

## 许可

[MIT](LICENSE)
