# CI 设计

修改 `.github/workflows/**`、`tools/gate.py` 或其他 CI 包装器之前，读本文。这些改动合并前须用户
明确确认（AGENTS.md 第 4 节）。

## 形态

- 平台：GitHub Actions，只在 Windows runner（`windows-latest`）上运行；Python 3.10 与 3.13 两个版本。
- 触发：每个 PR，以及推送到 `main`。不用 workflow 级的路径过滤，它会让必需检查一直挂起，或让被跳过的
  检查看起来是绿的。
- 按改动范围选择阶段（scope，见下一节）：`lint` 与 `docs` 每次都运行，`test` 只在改动需要时运行；
  推送到 `main` 时总是运行全部阶段。
- CI 与本地运行同样的命令（`tools/gate.py`），不在 workflow 里另写测试逻辑。
- 汇总任务 `Gate` 总是运行，是唯一的必需状态检查（名称固定为 `Gate`，`main` 的保护规则要求它）。
  以下情况失败：
  - `scope` 失败，或记录的计划不是为本次运行的提交生成的，或与各阶段实际使用的计划不一致；
  - 计划选中的阶段被跳过、取消或失败；计划没有选中的阶段不是“跳过”；
  - 某个层级发现 0 个测试，或没有产生本次运行的新结果文件（旧报告不算）；
  - 证据（结果文件、覆盖报告、目录检查与覆盖报告）缺失。

## 按改动范围选择阶段（scope）

- 组件图 `tools/scope.json` 把仓库路径映射到组件，每个组件写明它需要的阶段；`always` 列出每次都
  运行的阶段。`tools/scope.py` 读取改动的路径（PR 合并提交对第一个父提交的差异，重命名同时算新旧
  两个路径），选出所有命中组件的阶段，结果写成计划 `plan.json`，作为产物上传。
- 每个改动的路径都必须属于某个组件；有路径不属于任何组件时 `scope` 失败，需要先把它加入组件图。
- 选中全部阶段：改动 `tsw/`、`plugin/`、`tests/`、`tools/`（含组件图与 scope 本身）、
  `.github/workflows/**`、`pyproject.toml`、`requirements-dev.txt` 等运行时、测试、工具链与流水线文件。
- 检查的输入不能绕过检查：`docs/use-cases/**`（目录检查的输入）与测试或目录检查读取的设计文档
  （`docs/design/DN002-task-state-machine.md`）改动时选中 `test`。测试或目录检查开始读取
  `tsw/`、`plugin/`、`tests/`、`tools/` 之外的仓库文件时，同一个 PR 把它加入选中 `test` 的组件。
- 只改其他文档与治理文件（`docs/**`、`README`、`AGENTS.md`、`.github` 下的模板与配置等）时只运行
  `lint` 与 `docs`。
- 标签 `ci:all` 选中全部阶段：PR 加上或去掉标签都会重新运行 CI，不需要新的推送。
- scope 的规则有 unit 测试（组件图有效、没有归属的路径失败、上面每类路径选中的阶段、`ci:all`）。

## 阶段

| 阶段 | 内容 |
| --- | --- |
| `scope` | 按改动的路径与组件图生成计划，上传 `plan.json` |
| `lint`（每次） | ruff 检查与格式检查、pyright、`tsw/` 只依赖标准库的检查、workflow 静态检查 |
| `docs`（每次） | 文档检查（HTML 与 Markdown 同步、没有孤立的 HTML 视图、链接、图的校验、规则文档写到的自动化存在）、隐私扫描、目录的静态检查（字段、id、层级、迁移清单与 DN002 一致、对外表面认领） |
| `test`（3.10、3.13，按计划） | unit、component、surface、conformance，以及按本次结果判定的目录覆盖；上传结果、目录覆盖报告与行覆盖率 |
| `Gate` | 核对计划与实际运行的阶段双向一致，以及各阶段的结果文件与测试数量 |

live 层级不在 CI 中运行：它需要用户本机的 Claude app、登录状态与模型额度。

## 凭据与信任

- CI 不需要任何秘密；`GITHUB_TOKEN` 权限设为 `contents: read`。不使用 `pull_request_target`。
- 不在会处理 PR 内容的 workflow 上开 `workflow_dispatch`：手动触发会运行被触发引用上的定义。
- 来自 fork 的 PR 与本仓库分支的 PR 跑同样的阶段（都没有凭据）。

## 运行与工具

- 同一引用上新的运行取消旧的 PR 运行（每个引用一个运行）；`main` 上的运行不取消。
- 每个步骤设置超时。测试步骤的超时（30 分钟）至少是平常用时的两倍：同一提交在不同 runner 上的
  用时可差一倍。
- `tools/gate.py` 把每个测试层级的测试文件分给 `--jobs` 个并行的 pytest 进程（默认为 CPU 数，
  至多 8；`--jobs 1` 不并行），每个进程有自己的结果文件、覆盖数据与临时目录，结束后合并为该层级的
  结果文件。测试本来就不共享进程级环境、锁文件或数据目录（[test-environments.md](test-environments.md)），
  并行运行不需要另加隔离；某个测试只在并行时失败，说明它违反了这条约定。例外是驱动外部程序、
  而该程序有时限的测试（如 `tests/surface/test_dashboard_browser.py` 中的无界面浏览器）：所有核都被
  其他进程占满时它会超时（T355）。这样的测试文件在模块级写 `pytestmark = pytest.mark.alone`，门禁
  不把它分进并行进程，而在该层级的并行进程全部结束后，每个文件单独一个进程依次运行。
- 固定所有影响输出的工具版本：Actions 用完整提交 SHA，Python 开发依赖用确切版本
  （[code-quality.md](code-quality.md)）。升级版本与重新生成输出放在同一个 PR。
- Dependabot 覆盖 `github-actions` 与 `pip`，更新 PR 与普通改动一样评审。
- 文档中写到的每个自动化都要有处理程序，缺少时 `docs` 检查失败：`AGENTS.md` 与
  `docs/development/*.md` 中写作命令的 `python <路径>` 脚本必须存在；写到的 CI 标签（`ci:<名称>`）
  必须在 `.github/workflows/` 中被处理。标签本身由仓库所有者在 GitHub 上创建，检查不核对平台。
