# 代码质量与依赖

修改 lint、类型检查或测试配置，或增加、升级依赖之前，读本文。

## 质量门禁

- 警告即错误：ruff、pyright 与测试运行中的警告都会让门禁失败（pytest 使用 `-W error`）。
- 一份共享配置（`pyproject.toml`），同样适用于 `tsw/`、`plugin/`、`tools/` 与 `tests/`。
- ruff 负责 lint 与格式；pyright 以 strict 模式检查 `tsw/`、`plugin/` 与 `tools/`，`tests/` 用 basic 模式。
- 公共函数与插件接口有完整的类型注解；插件接口用 `typing.Protocol` 定义（DN006）。
- 不压制警告。确需例外时在行内写明规则、理由与 issue 链接，并在评审中说明。
- 负向探针：修改质量配置或升级工具时，验证每类规则（lint、格式、类型、警告）确实会让门禁失败。
- 删除没有任何代码读取的配置项和环境变量。

## 运行时依赖

- `tsw/` 与 `plugin/` 运行时只依赖 Python 3.10+ 标准库。门禁用 `sys.stdlib_module_names` 检查所有
  导入，出现第三方包即失败。
- 用户只需要 Python 与 git 就能运行 tsw；`claude`、`codex` 命令行是否可用由对应插件在运行时检测并
  报告，不是安装依赖。

## 开发依赖

- 开发工具（pytest、ruff、pyright 等）写在 `requirements-dev.txt`，全部固定确切版本，在虚拟环境
  `.venv/` 中安装。CI 只从这个文件安装。
- Dependabot 提出的升级与普通改动一样评审，并在同一个 PR 里修好因升级出现的新告警。
- 不运行来源不明的安装脚本；新增开发依赖在 PR 中写明用途。
