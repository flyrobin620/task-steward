# 文档

修改设计文档、图、README 或其他公开文档之前，读本文。

## 语言与格式

| 文档 | 语言 | 格式 |
| --- | --- | --- |
| `README` | 中文 | Markdown + HTML |
| 设计文档 `docs/design/DN*.md` | 中文 | Markdown + HTML |
| 开发规则 `AGENTS.md`、`docs/development/*` | 中文 | 只有 Markdown |
| 决定记录 `docs/decisions/*` | 中文 | 只有 Markdown |
| 代码、标识符、命令、commit message、PR 标题 | 英文 | — |

## Markdown 与 HTML

- Markdown 是来源，HTML 由 `tools/docs.py render` 生成，绝不手工编辑。HTML 自包含，可离线打开并打印，
  内嵌图使用同目录的 SVG。
- 生成的 HTML 记录来源 Markdown 的 SHA-256；`tools/docs.py check` 在两者不一致时失败（报告“已过期”），
  并检查相对链接与锚点有效。
- 改 Markdown 的同一个 PR 里重新生成 HTML。

## 设计文档

- 实现必须与设计一致。实现中发现设计需要调整时，先改设计文档（同一 PR 或先行的设计 PR），设计改动
  合并前须用户确认。
- 设计文档的“待定”“假设”内容在被决定或验证后更新原处，并在 `docs/decisions/` 记录已确认的决定。
- 状态机等图的权威来源是 `docs/design/diagrams/*.mmd`；SVG 由 `draw.py` 生成，`check.py` 校验每条
  迁移、节点、编号都在图中可见，结果写入 `validation.json`。改 `.mmd` 后同步调整 `draw.py` 的布局，
  再运行两个脚本。

## 公开仓库卫生

- 不提交工作身份、雇主或其他组织的内部信息、内部主机名、本机绝对路径、个人账号以外的身份信息。
- `tools/docs.py check` 包含隐私扫描：内置规则检查本机路径、邮箱等通用模式；额外的私有词表放在
  `.local/privacy-terms.txt`（不提交），存在时一并检查。
- 录制的平台样本与测试夹具同样适用这些规则（[test-environments.md](test-environments.md)）。
