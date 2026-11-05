# 0001 开发流程、测试层级与 CI

- 状态：已接受（2026-11-05，以用户批准本 PR 为准）
- 适用：进入 v1 开发前制定的仓库开发规则

## 背景

v1 设计（DN001–DN007）已合并，下一步是实现。实现之前需要确定：规则文档的语言与形式、测试分层、CI
的平台与形态、评审与合并授权。规则的骨架参考了一套通用的仓库初始化模板（AGENTS.md 加专题规则文档、
use-case 目录、汇总门禁），按本仓库的实际形态取舍：Python 标准库实现、对外表面为 CLI / MCP /
hooks / 插件接口、有持久化数据、不使用云资源、v1 不发布。

## 用户回答（2026-11-05）

| 问题 | 选项 | 回答 |
| --- | --- | --- |
| 开发规范用什么语言 | 中文（推荐）/ 英文 | 中文；代码、标识符、commit message、PR 标题用英文 |
| 开发规范是否出 HTML 版 | 只要 Markdown（推荐）/ 也要 HTML | 只要 Markdown；HTML 继续用于 README 与设计文档 |
| 开发 PR 由谁决定合并 | agent 合并，敏感改动问用户（推荐）/ 每个 PR 都由用户确认 | agent 合并，敏感改动问用户 |
| CI 的平台与 Python 版本 | Windows + Ubuntu × 3.10 / 3.13（推荐）/ 再加 macOS / 只跑 Ubuntu | Windows only |

此前（同日，确定 v1 范围时）用户已确认：v1 插件为宿主集成 `claude-code`、会话适配器 `claude` 与
`codex`、人类渠道 `in-session`、存储后端 `fs`；历史导入（DN005）放到 v2。

## 决定

1. **规则文档**：中文 Markdown。`AGENTS.md` 写通用规则、开发循环和“先读哪份文档”的表，专题规则在
   `docs/development/`。`CLAUDE.md` 只引用 `AGENTS.md`，`.github/copilot-instructions.md` 只指向它。
2. **测试层级**：`unit`、`component`、`surface`、`conformance`、`live`
   （[testing-layers.md](../development/testing-layers.md)）。前四层在本地门禁与 CI 运行；`live` 调用
   真实 Claude app 与模型，只在本机运行，触及宿主集成、会话适配器或评审协议的改动必须运行对应场景。
3. **覆盖门禁**：use case 与状态迁移（DN002 的 T / E 迁移与守卫）的覆盖是门禁；行覆盖率只报告。
4. **CI**：GitHub Actions，只用 Windows runner，Python 3.10 与 3.13；每个 PR 运行全部阶段，汇总检查
   `Gate` 是唯一的必需检查；CI 不使用任何秘密（[ci.md](../development/ci.md)）。
5. **工具**：pytest、ruff、pyright（strict），开发依赖固定确切版本；运行时只依赖标准库，由门禁检查
   （[code-quality.md](../development/code-quality.md)）。
6. **评审**：每个 PR 由与作者不同的模型独立评审，默认 Codex `gpt-6-astra` medium 只读
   （[review.md](../development/review.md)）。
7. **合并**：squash 合并；`Gate` 绿、最终 head 评审通过、意见全部解决、（派发的任务）编排者验收后由
   agent 合并；CI、门禁、规则文档、决定记录、设计文档、发布配置、权限与身份校验代码、`LICENSE` 的
   改动须用户明确确认。
8. **v1 范围**：按上面的用户确认实现 v1 插件；DN005 的历史导入与 DN002 §3.4 的导入规则放到 v2，
   在状态迁移清单中标记为 `planned`，不计入 v1 的覆盖门禁。
9. **兼容性**：账本只追加；各契约的版本规则以设计文档为准
   （[compatibility.md](../development/compatibility.md)）。

## 后果

- 门禁与 CI 包装器（`tools/gate.py`、`tools/docs.py`、`tools/live.py`）、`.github/workflows/ci.yml`、
  `requirements-dev.txt`、use-case 目录与状态迁移清单由第一个开发 PR 按这些规则实现；在那之前，引用
  它们的检查报告为“未验证”，合并按 [workflow.md](../development/workflow.md) 的过渡规则执行。
- 本 PR 合并后，在 GitHub 上为 `main` 设置保护规则（必需检查 `Gate`、必须解决对话、分支须基于最新
  `main`、禁止直接推送）。该设置在 `Gate` 第一次实际运行后启用，启用前须用户确认。
- 只在 Windows 上运行 CI：Linux 与 macOS 上的文件锁、路径与原子重命名行为不在 CI 覆盖范围内，支持
  这些平台之前需要重新决定。

## 待定

- TODO(decision)：旧版本 tsw 遇到不认识的账本事件类型时的行为。提议：拒绝写入并报告需要升级，只读
  命令照常可用；写入 DN007。
- TODO(decision)：插件分发方式与是否发布到 PyPI（[release.md](../development/release.md)）。
