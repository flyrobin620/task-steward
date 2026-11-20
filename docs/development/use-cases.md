# use-case 目录、状态迁移清单与测试映射

新增或改变行为，或改动任何对外表面（CLI 命令、MCP 工具、hooks、插件接口、状态迁移）之前，读本文。

## use-case 目录

`docs/use-cases/catalog.json` 列出所有受支持的 use case（第一个开发 PR
依据 DN001–DN007 起草，每条在首次实现时确认）。用 JSON 是为了让检查工具只依赖标准库。

```json
{
  "id": "UC-LEDGER-001",
  "title": "主会话提交事件后，投影与 STATUS 页反映新状态",
  "actor": "主会话 / worker / 用户 / tsw CLI",
  "acceptance": "能观察到的输出：账本事件、投影字段、文件、退出码",
  "surfaces": ["mcp", "cli"],
  "component": "ledger",
  "status": "active",
  "claims": ["cli:ledger audit"],
  "requiredTiers": ["unit", "component", "surface"],
  "omittedTiers": {"conformance": "不经过插件接口", "live": "不依赖宿主行为"}
}
```

`claims` 列出该 use case 认领的对外表面：`cli:<命令>`（tsw CLI 命令）、`tool:<名称>`（tsw 工具）、
`mcp:<名称>`（MCP 工具）。

- 每个条目都有示例中的全部字段：`id`、`title`、`actor`、`acceptance`、`component`、`status` 为非空
  文本，`surfaces` 为非空列表（取值 `cli`、`mcp`、`hook`、`plugin`），`claims` 为列表，`requiredTiers`
  为列表，`omittedTiers` 为对象。
- id 形如 `UC-<AREA>-<NNN>`，稳定且不复用；退役的条目保留并标记 `status: retired`。
- 层级为 `unit`、`component`、`surface`、`conformance`、`live`。没有列入 `requiredTiers` 的层级必须
  在 `omittedTiers` 里写明理由；同一层级不能同时出现在两处，`omittedTiers` 只能写已知层级。
- 在新增或改变行为的同一个 PR 里更新目录。

## 状态迁移清单

`docs/use-cases/transitions.json`（第一个开发 PR 创建）列出 DN002 中的每个编号：§3.2 的迁移
（T01–T27）、§3.3 的 worker 内部迁移（E01–E07）、§4 的守卫（G-*），以及 §3.4 的导入规则
（I01–I06）。DN002 的表是权威来源，清单是它的机器可读副本；`active` 的条目用 `requiredTiers` 写明需要的层级。尚未实现的编号标记为 `planned`（例如导入，
见 [0001](../decisions/0001-development-process.md)），实现时改为 `active`：

- 检查工具比较 DN002 表中的编号与清单，两边不一致即失败；
- 每个 `active` 的迁移与守卫至少有一个 unit 测试，涉及文件与并发的迁移还要有 component 测试。

## 测试与 use case 的对应

测试声明自己覆盖的 use case 与迁移，不维护手写的测试清单：

| 测试 | 标记 |
| --- | --- |
| pytest | `@pytest.mark.use_case("UC-LEDGER-001")`、`@pytest.mark.transition("T07")` |
| live 场景定义 | `"useCases": ["UC-HOST-001"]` |

## 目录检查

目录检查是 `Gate` 的一部分，分两部分（[ci.md](ci.md)）：

- **静态部分**不需要测试结果，每个 PR 都在 `docs` 阶段运行（`tools/gate.py --steps catalog-static`）：
  字段完整、id 格式与重复、层级的取值与理由、迁移清单与 DN002 一致、对外表面的认领、live 层级有场景定义。
- **覆盖部分**按本次测试结果判定，随测试运行（`--steps tests,catalog`，同时也运行静态部分）。

以下情况失败：

1. 某个 active use case 的必需层级没有测试；
2. 某个 active 迁移或守卫没有要求的测试；
3. 测试引用了未知或已退役的 id；
4. id 重复或被复用，条目缺少字段，或同一层级同时列在 `requiredTiers` 与 `omittedTiers`；
5. 对外表面没有被任何 active use case 认领，或认领的表面不存在：检查工具把从代码中提取的 tsw CLI
   命令（`cli:`）、tsw 工具（`tool:`）与 MCP 工具（`mcp:`）与目录比对，没有例外。CLI 选项、hooks 事件
   与插件接口方法不逐项提取，由认领它们所属命令或工具的 use case 及其测试覆盖；
6. 本次运行中，某个必需测试被跳过、没有被发现或没有执行。

覆盖部分把执行过的覆盖报告（use case / 迁移 → 层级 → 测试 → 结果）写入结果目录的
`catalog-coverage.json`，随测试结果作为产物发布，`Gate` 核对它存在。覆盖以本次运行的结果为准，
不以测试代码存在或旧报告为准。live 层级在 CI 中不运行，目录检查只核对它有场景定义；是否运行由
PR 的验证表说明。

## 覆盖率

行覆盖率只报告，不作为门禁；门禁是 use case 与状态迁移的覆盖。
