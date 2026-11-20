"""Use-case catalog and transition-list check (docs/development/use-cases.md).

The check has two parts. The static part needs no test results and runs on every change
(``check_static``); the coverage part judges this run's test results (``check_coverage``) and
writes the executed coverage report. ``check`` runs both. The check fails when:

1. an active use case has no passing test in one of its required tiers that ran;
2. an active transition or guard has no passing test in one of its required tiers that ran;
3. a test references an unknown or retired use case, or an unknown or planned transition;
4. an id is duplicated or malformed, an entry misses a field, a tier is unknown, an omitted tier
   has no reason, or a tier is both required and omitted;
5. a CLI command or tool is not claimed by an active use case, or a claim names nothing;
6. a required test was skipped, not run or failed in this run;
7. the transition list and the DN002 tables disagree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

TIERS = ("unit", "component", "surface", "conformance", "live")
SURFACES = ("cli", "mcp", "hook", "plugin")
UC_ID = re.compile(r"^UC-[A-Z]+-\d{3}$")
DN002_ROW = re.compile(r"^\| (T\d\d|E\d\d|I\d\d|G-[A-Z-]+) \|")
UC_TEXT_FIELDS = ("id", "title", "actor", "acceptance", "component", "status")
TRANSITION_TEXT_FIELDS = ("id", "kind", "status")


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


def _text_fields(entry: Mapping[str, Any], fields: Iterable[str], label: str) -> list[str]:
    return [
        f"{label}: 缺少字段 {field} 或为空"
        for field in fields
        if not isinstance(entry.get(field), str) or not str(entry.get(field)).strip()
    ]


def _validate_tiers(entry: Mapping[str, Any], label: str, *, omitted_required: bool) -> list[str]:
    problems: list[str] = []
    raw_required: object = entry.get("requiredTiers", [])
    if not isinstance(raw_required, list):
        return [f"{label}: requiredTiers 必须是列表"]
    required = [str(tier) for tier in cast("list[Any]", raw_required)]
    unknown = [tier for tier in required if tier not in TIERS]
    if unknown:
        problems.append(f"{label}: 未知层级 {unknown}")
    if not omitted_required:
        return problems
    raw_omitted: object = entry.get("omittedTiers", {})
    if not isinstance(raw_omitted, dict):
        return [*problems, f"{label}: omittedTiers 必须是对象"]
    omitted = {str(k): v for k, v in cast("dict[Any, Any]", raw_omitted).items()}
    unknown = [tier for tier in omitted if tier not in TIERS]
    if unknown:
        problems.append(f"{label}: omittedTiers 中有未知层级 {unknown}")
    both = [tier for tier in TIERS if tier in required and tier in omitted]
    if both:
        problems.append(f"{label}: 层级 {both} 同时列在 requiredTiers 与 omittedTiers")
    for tier in TIERS:
        if tier not in required and not str(omitted.get(tier, "")).strip():
            problems.append(
                f"{label}: 层级 {tier} 未列入 requiredTiers，需在 omittedTiers 写明理由"
            )
    return problems


def _validate_use_case(entry: Mapping[str, Any], label: str) -> list[str]:
    problems = _text_fields(entry, UC_TEXT_FIELDS, label)
    surfaces: object = entry.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        problems.append(f"{label}: 缺少字段 surfaces 或为空")
    else:
        unknown = [s for s in cast("list[Any]", surfaces) if s not in SURFACES]
        if unknown:
            problems.append(f"{label}: surfaces 中有未知取值 {unknown}")
    for field, kind in (("claims", list), ("requiredTiers", list), ("omittedTiers", dict)):
        if not isinstance(entry.get(field), kind):
            problems.append(f"{label}: 缺少字段 {field} 或类型不对")
    return problems + _validate_tiers(entry, label, omitted_required=True)


class Catalog:
    """The use-case catalog and the transition list, indexed, with the static problems found
    while reading them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.problems: list[str] = []
        catalog = _load(root / "docs" / "use-cases" / "catalog.json")
        transitions = _load(root / "docs" / "use-cases" / "transitions.json")
        self.use_cases: dict[str, Mapping[str, Any]] = {}
        for entry in catalog["useCases"]:
            uc_id = str(entry.get("id"))
            if not UC_ID.match(uc_id):
                self.problems.append(f"{uc_id}: id 格式应为 UC-<AREA>-<NNN>")
            if uc_id in self.use_cases:
                self.problems.append(f"{uc_id}: id 重复")
            self.use_cases[uc_id] = entry
            if entry.get("status") not in ("active", "retired"):
                self.problems.append(f"{uc_id}: status 必须是 active 或 retired")
            self.problems += _validate_use_case(entry, uc_id)
        self.items: dict[str, Mapping[str, Any]] = {}
        for entry in transitions["items"]:
            item_id = str(entry.get("id"))
            if item_id in self.items:
                self.problems.append(f"{item_id}: 迁移清单中重复")
            self.items[item_id] = entry
            self.problems += _text_fields(entry, TRANSITION_TEXT_FIELDS, item_id)
            self.problems += _validate_tiers(entry, item_id, omitted_required=False)
            if entry.get("status") not in ("active", "planned", "retired"):
                self.problems.append(f"{item_id}: status 必须是 active、planned 或 retired")
            if entry.get("status") == "active" and not entry.get("requiredTiers"):
                self.problems.append(f"{item_id}: active 的迁移必须列出 requiredTiers")

    def active(self) -> list[tuple[str, str, list[str], str]]:
        """``(kind, id, required tiers, test marker key)`` of every active entry."""
        return [
            *(
                ("use case ", uc_id, list(entry.get("requiredTiers", [])), "use_cases")
                for uc_id, entry in self.use_cases.items()
                if entry.get("status") == "active"
            ),
            *(
                ("迁移", item_id, list(entry.get("requiredTiers", [])), "transitions")
                for item_id, entry in self.items.items()
                if entry.get("status") == "active"
            ),
        ]


def check_static(
    root: Path,
    cli_commands: Iterable[str],
    *,
    tools: Iterable[str] = (),
    mcp_tools: Iterable[str] = (),
) -> list[str]:
    """Everything that does not need test results: rules 4, 5 and 7, and live scenarios."""
    catalog = Catalog(root)
    problems = list(catalog.problems)
    design = dn002_ids(root)
    missing = [i for i in design if i not in catalog.items]
    extra = [i for i in catalog.items if i not in design]
    if missing:
        problems.append(f"迁移清单缺少 DN002 中的编号：{missing}")
    if extra:
        problems.append(f"迁移清单有 DN002 中不存在的编号：{extra}")

    for _kind, ident, required, _key in catalog.active():
        if "live" in required and not _live_scenarios(root, ident):
            problems.append(f"{ident}: 要求 live 层级，但没有引用它的 live 场景定义")

    claimed: dict[str, str] = {}
    for uc_id, entry in catalog.use_cases.items():
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
    return problems


def check_coverage(
    root: Path, results: Iterable[Mapping[str, Any]], tiers_run: set[str]
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Rules 1, 2, 3 and 6 from this run's results: ``(problems, not_verified, report)``.

    The report maps every active use case and transition to its required tiers, and each tier to
    the tests of this run that declare it with their outcomes (``not_run`` for a tier that did not
    run here, ``live`` for the live tier, judged by its scenario definitions).
    """
    catalog = Catalog(root)
    tests = list(results)
    problems: list[str] = []
    not_verified: list[str] = []
    for test in tests:
        for uc_id in test.get("use_cases", []):
            entry = catalog.use_cases.get(uc_id)
            if entry is None:
                problems.append(f"{test['nodeid']}: 引用了未知的 use case {uc_id}")
            elif entry.get("status") == "retired":
                problems.append(f"{test['nodeid']}: 引用了已退役的 use case {uc_id}")
        for item_id in test.get("transitions", []):
            item = catalog.items.get(item_id)
            if item is None:
                problems.append(f"{test['nodeid']}: 引用了未知的迁移 {item_id}")
            elif item.get("status") != "active":
                problems.append(f"{test['nodeid']}: 引用了非 active 的迁移 {item_id}")

    report: dict[str, Any] = {"tiersRun": sorted(tiers_run), "useCases": {}, "transitions": {}}
    for kind, ident, required, key in catalog.active():
        section = report["useCases" if key == "use_cases" else "transitions"]
        tiers: dict[str, Any] = {}
        section[ident] = {"requiredTiers": required, "tiers": tiers}
        for tier in required:
            if tier == "live":
                tiers[tier] = {"status": "scenario" if _live_scenarios(root, ident) else "missing"}
                continue
            if tier not in tiers_run:
                tiers[tier] = {"status": "not_run"}
                not_verified.append(f"{ident}: 本次未运行 {tier} 层级")
                continue
            mine = [t for t in tests if t.get("tier") == tier and ident in t.get(key, [])]
            passed = [t for t in mine if t.get("outcome") == "passed"]
            bad = [t for t in mine if t.get("outcome") != "passed"]
            tiers[tier] = {
                "status": "passed" if passed and not bad else "failed",
                "tests": [{"nodeid": t["nodeid"], "outcome": t.get("outcome")} for t in mine],
            }
            if not passed:
                problems.append(f"{ident}: {kind}要求的 {tier} 层级没有通过的测试")
            problems.extend(
                f"{ident}: 必需的测试未通过（{t.get('outcome')}）{t['nodeid']}" for t in bad
            )
    return problems, not_verified, report


def check(
    root: Path,
    results: Iterable[Mapping[str, Any]],
    tiers_run: set[str],
    cli_commands: Iterable[str],
    *,
    tools: Iterable[str] = (),
    mcp_tools: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Both parts: ``(problems, not_verified)``."""
    static = check_static(root, cli_commands, tools=tools, mcp_tools=mcp_tools)
    problems, not_verified, _report = check_coverage(root, results, tiers_run)
    return static + problems, not_verified


def _live_scenarios(root: Path, ident: str) -> bool:
    directory = root / "tools" / "live" / "scenarios"
    if not directory.is_dir():
        return False
    for path in directory.glob("*.json"):
        scenario = _load(path)
        if ident in scenario.get("useCases", []):
            return True
    return False
