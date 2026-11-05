# CI 设计

修改 `.github/workflows/**`、`tools/gate.py` 或其他 CI 包装器之前，读本文。这些改动合并前须用户
明确确认（AGENTS.md 第 4 节）。

## 形态

- 平台：GitHub Actions，只在 Windows runner（`windows-latest`）上运行；Python 3.10 与 3.13 两个版本。
- 触发：每个 PR，以及推送到 `main`。不用 workflow 级的路径过滤，它会让必需检查一直挂起，或让被跳过的
  检查看起来是绿的。
- 每次都运行全部阶段，暂不做按改动范围选择阶段。全部阶段的总时长超过约 15 分钟时，再加入
  按改动范围选择阶段的解析（scope），并为它编写测试。
- CI 与本地运行同样的命令（`tools/gate.py`），不在 workflow 里另写测试逻辑。
- 汇总任务 `Gate` 总是运行，是唯一的必需状态检查。以下情况失败：
  - 任何阶段被跳过、取消或失败；
  - 某个层级发现 0 个测试，或没有产生本次运行的新结果文件（旧报告不算）；
  - 证据（结果文件、覆盖报告、目录检查报告）缺失。

## 阶段

| 阶段 | 内容 |
| --- | --- |
| `lint` | ruff 检查与格式检查、pyright、`tsw/` 只依赖标准库的检查、workflow 静态检查 |
| `docs` | 文档检查（HTML 与 Markdown 同步、链接、图的校验）、隐私扫描、use-case 目录与状态迁移清单检查 |
| `test`（3.10、3.13） | unit、component、surface、conformance；上传结果、覆盖报告与行覆盖率 |
| `Gate` | 汇总以上阶段，核对结果文件与测试数量 |

live 层级不在 CI 中运行：它需要用户本机的 Claude app、登录状态与模型额度。

## 凭据与信任

- CI 不需要任何秘密；`GITHUB_TOKEN` 权限设为 `contents: read`。不使用 `pull_request_target`。
- 不在会处理 PR 内容的 workflow 上开 `workflow_dispatch`：手动触发会运行被触发引用上的定义。
- 来自 fork 的 PR 与本仓库分支的 PR 跑同样的阶段（都没有凭据）。

## 运行与工具

- 同一引用上新的运行取消旧的 PR 运行（每个引用一个运行）；`main` 上的运行不取消。
- 每个步骤设置超时。
- 固定所有影响输出的工具版本：Actions 用完整提交 SHA，Python 开发依赖用确切版本
  （[code-quality.md](code-quality.md)）。升级版本与重新生成输出放在同一个 PR。
- Dependabot 覆盖 `github-actions` 与 `pip`，更新 PR 与普通改动一样评审。
- 文档中写到的每个自动化（标签、命令）都要有处理程序；缺少时检查失败。
