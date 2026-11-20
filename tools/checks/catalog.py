"""Use-case catalog and transition-list check (docs/development/use-cases.md).

The check fails when:

1. an active use case has no passing test in one of its required tiers that ran;
2. an active transition or guard has no passing test in one of its required tiers that ran;
3. a test references an unknown or retired use case, or an unknown or planned transition;
4. an id is duplicated, malformed or missing omitted-tier reasons;
5. a CLI command or tool is not claimed by an active use case, or a claim names nothing;
6. a required test was skipped, not run or failed in this run;
7. the transition list and the DN002 tables disagree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

TIERS = ("unit", "component", "surface", "conformance", "live")
UC_ID = re.compile(r"^UC-[A-Z]+-\d{3}$")
DN002_ROW = re.compile(r"^\| (T\d\d|E\d\d|I\d\d|G-[A-Z-]+) \|")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dn002_ids(root: Path) -> list[str]:
    text = (root / "docs" / "design" / "DN002-task-state-machine.md").read_text(encoding="utf-8")
    found: list[str] = []
    for line in text.splitlines():
        match = DN002_ROW.match(line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def _validate_entry(entry: Mapping[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    required = list(entry.get("requiredTiers", []))
    unknown = [tier for tier in required if tier not in TIERS]
    if unknown:
        problems.append(f"{label}: 未知层级 {unknown}")
    omitted = dict(entry.get("omittedTiers", {}))
    for tier in TIERS:
        if tier not in required and not str(omitted.get(tier, "")).strip():
            problems.append(
                f"{label}: 层级 {tier} 未列入 requiredTiers，需在 omittedTiers 写明理由"
            )
    return problems


def check(
    root: Path,
    results: Iterable[Mapping[str, Any]],
    tiers_run: set[str],
    cli_commands: Iterable[str],
    *,
    tools: Iterable[str] = (),
    mcp_tools: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Returns ``(problems, not_verified)``."""
    problems: list[str] = []
    not_verified: list[str] = []
    catalog = _load(root / "docs" / "use-cases" / "catalog.json")
    transitions = _load(root / "docs" / "use-cases" / "transitions.json")
    tests = list(results)

    use_cases: dict[str, Mapping[str, Any]] = {}
    for entry in catalog["useCases"]:
        uc_id = str(entry["id"])
        if not UC_ID.match(uc_id):
            problems.append(f"{uc_id}: id 格式应为 UC-<AREA>-<NNN>")
        if uc_id in use_cases:
            problems.append(f"{uc_id}: id 重复")
        use_cases[uc_id] = entry
        if entry.get("status") not in ("active", "retired"):
            problems.append(f"{uc_id}: status 必须是 active 或 retired")
        problems += _validate_entry(entry, uc_id)

    items: dict[str, Mapping[str, Any]] = {}
    for entry in transitions["items"]:
        item_id = str(entry["id"])
        if item_id in items:
            problems.append(f"{item_id}: 迁移清单中重复")
        items[item_id] = entry
        if entry.get("status") not in ("active", "planned", "retired"):
            problems.append(f"{item_id}: status 必须是 active、planned 或 retired")
        if entry.get("status") == "active" and not entry.get("requiredTiers"):
            problems.append(f"{item_id}: active 的迁移必须列出 requiredTiers")
    design = dn002_ids(root)
    missing = [i for i in design if i not in items]
    extra = [i for i in items if i not in design]
    if missing:
        problems.append(f"迁移清单缺少 DN002 中的编号：{missing}")
    if extra:
        problems.append(f"迁移清单有 DN002 中不存在的编号：{extra}")

    # Tests that reference ids.
    for test in tests:
        for uc_id in test.get("use_cases", []):
            entry = use_cases.get(uc_id)
            if entry is None:
                problems.append(f"{test['nodeid']}: 引用了未知的 use case {uc_id}")
            elif entry.get("status") == "retired":
                problems.append(f"{test['nodeid']}: 引用了已退役的 use case {uc_id}")
        for item_id in test.get("transitions", []):
            item = items.get(item_id)
            if item is None:
                problems.append(f"{test['nodeid']}: 引用了未知的迁移 {item_id}")
            elif item.get("status") != "active":
                problems.append(f"{test['nodeid']}: 引用了非 active 的迁移 {item_id}")

    def coverage(kind: str, ident: str, required: list[str], key: str) -> None:
        for tier in required:
            if tier == "live":
                if not _live_scenarios(root, ident):
                    problems.append(f"{ident}: 要求 live 层级，但没有引用它的 live 场景定义")
                continue
            if tier not in tiers_run:
                not_verified.append(f"{ident}: 本次未运行 {tier} 层级")
                continue
            mine = [t for t in tests if t.get("tier") == tier and ident in t.get(key, [])]
            passed = [t for t in mine if t.get("outcome") == "passed"]
            bad = [t for t in mine if t.get("outcome") != "passed"]
            if not passed:
                problems.append(f"{ident}: {kind}要求的 {tier} 层级没有通过的测试")
            problems.extend(
                f"{ident}: 必需的测试未通过（{t.get('outcome')}）{t['nodeid']}" for t in bad
            )

    for uc_id, entry in use_cases.items():
        if entry.get("status") == "active":
            coverage("use case ", uc_id, list(entry.get("requiredTiers", [])), "use_cases")
    for item_id, entry in items.items():
        if entry.get("status") == "active":
            coverage("迁移", item_id, list(entry.get("requiredTiers", [])), "transitions")

    # Public surface inventory.
    claimed: dict[str, str] = {}
    for uc_id, entry in use_cases.items():
        if entry.get("status") != "active":
            continue
        for claim in entry.get("claims", []):
            claimed[str(claim)] = uc_id
    commands = (
        {f"cli:{name}" for name in cli_commands}
        | {f"tool:{name}" for name in tools}
        | {f"mcp:{name}" for name in mcp_tools}
    )
    problems += [
        f"对外表面没有被任何 active use case 认领：{c}" for c in sorted(commands - set(claimed))
    ]
    problems += [
        f"{claimed[c]}: 认领的表面不存在 {c}"
        for c in sorted(set(claimed) - commands)
        if c.startswith(("cli:", "tool:", "mcp:"))
    ]
    return problems, not_verified


def _live_scenarios(root: Path, ident: str) -> bool:
    directory = root / "tools" / "live" / "scenarios"
    if not directory.is_dir():
        return False
    for path in directory.glob("*.json"):
        scenario = _load(path)
        if ident in scenario.get("useCases", []):
            return True
    return False
