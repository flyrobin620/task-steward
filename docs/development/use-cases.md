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
  "requiredTiers": ["unit", "component", "surface"],
  "omittedTiers": {"conformance": "不经过插件接口", "live": "不依赖宿主行为"}
}
```

- id 形如 `UC-<AREA>-<NNN>`，稳定且不复用；退役的条目保留并标记 `status: retired`。
- 层级为 `unit`、`component`、`surface`、`conformance`、`live`。没有列入 `requiredTiers` 的层级必须
  在 `omittedTiers` 里写明理由。
- 在新增或改变行为的同一个 PR 里更新目录。

## 状态迁移清单

`docs/use-cases/transitions.json`（第一个开发 PR 创建）列出 DN002 §3.2 迁移表中的每个迁移
（T01–T27、E01–E07）与 §4 的守卫（G-*），以及导入规则（I01–I06，导入在 v2 实现，之前标记为 `planned`）。
DN002 的表是权威来源，清单是它的机器可读副本：

- 检查工具比较 DN002 表中的编号与清单，两边不一致即失败；
- 每个 `active` 的迁移与守卫至少有一个 unit 测试，涉及文件与并发的迁移还要有 component 测试。

## 测试与 use case 的对应

测试声明自己覆盖的 use case 与迁移，不维护手写的测试清单：

| 测试 | 标记 |
| --- | --- |
| pytest | `@pytest.mark.use_case("UC-LEDGER-001")`、`@pytest.mark.transition("T07")` |
| live 场景定义 | `"useCases": ["UC-HOST-001"]` |

## 目录检查

目录检查不需要构建，每个 PR 都运行，是 `Gate` 的一部分。以下情况失败：

1. 某个 active use case 的必需层级没有测试；
2. 某个 active 迁移或守卫没有要求的测试；
3. 测试引用了未知或已退役的 id；
4. id 重复或被复用；
5. 新增的对外表面没有被任何 use case 认领：检查工具把从代码中提取的 CLI 命令与选项、MCP 工具、hooks
   事件、插件接口方法与目录比对，没有例外；
6. 本次运行中，某个必需测试被跳过、没有被发现或没有执行。

已执行的覆盖报告（use case / 迁移 → 测试 → 层级 → 结果）作为产物发布。覆盖以本次运行的结果为准，
不以测试代码存在或旧报告为准。live 层级在 CI 中不运行，目录检查只核对它有场景定义；是否运行由
PR 的验证表说明。

## 覆盖率

行覆盖率只报告，不作为门禁；门禁是 use case 与状态迁移的覆盖。
