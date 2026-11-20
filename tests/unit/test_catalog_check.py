"""The use-case catalog check (tools/checks/catalog.py) on a synthetic repository: every rule
fails on an input made to break it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.checks import catalog

OMIT_ALL_BUT_UNIT = {
    "component": "纯逻辑",
    "surface": "没有对外表面",
    "conformance": "不经过插件接口",
    "live": "不依赖宿主",
}


def _use_case(uc_id: str = "UC-DEMO-001", **changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": uc_id,
        "title": "示例",
        "actor": "用户",
        "acceptance": "退出码 0",
        "surfaces": ["cli"],
        "component": "demo",
        "status": "active",
        "claims": ["cli:demo"],
        "requiredTiers": ["unit"],
        "omittedTiers": dict(OMIT_ALL_BUT_UNIT),
    }
    entry.update(changes)
    return entry


def _repo(
    root: Path,
    use_cases: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    dn002: tuple[str, ...] = ("T01",),
    scenarios: dict[str, list[str]] | None = None,
) -> Path:
    cases = root / "docs" / "use-cases"
    cases.mkdir(parents=True)
    (cases / "catalog.json").write_text(
        json.dumps({"tiers": [], "useCases": use_cases or [_use_case()]}), encoding="utf-8"
    )
    default = [{"id": "T01", "kind": "transition", "status": "active", "requiredTiers": ["unit"]}]
    (cases / "transitions.json").write_text(
        json.dumps({"source": "DN002", "items": default if items is None else items}),
        encoding="utf-8",
    )
    design = root / "docs" / "design"
    design.mkdir(parents=True)
    rows = "\n".join(f"| {ident} | 迁移 |" for ident in dn002)
    (design / "DN002-task-state-machine.md").write_text(
        f"# DN002\n\n| 编号 | 说明 |\n| --- | --- |\n{rows}\n", encoding="utf-8"
    )
    for name, ids in (scenarios or {}).items():
        directory = root / "tools" / "live" / "scenarios"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps({"useCases": ids}), encoding="utf-8")
    return root


def _test(
    nodeid: str, outcome: str = "passed", tier: str = "unit", **marks: list[str]
) -> dict[str, Any]:
    return {
        "nodeid": nodeid,
        "tier": tier,
        "outcome": outcome,
        "use_cases": marks.get("use_cases", []),
        "transitions": marks.get("transitions", []),
    }


GOOD_RESULTS = [
    _test("tests/unit/test_demo.py::test_a", use_cases=["UC-DEMO-001"]),
    _test("tests/unit/test_demo.py::test_t", transitions=["T01"]),
]


def _static(root: Path, commands: tuple[str, ...] = ("demo",)) -> list[str]:
    return catalog.check_static(root, commands)


def test_a_consistent_catalog_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert _static(root) == []
    problems, not_verified = catalog.check(root, GOOD_RESULTS, {"unit"}, ["demo"])
    assert (problems, not_verified) == ([], [])


def test_a_missing_or_empty_field_fails(tmp_path: Path) -> None:
    entry = _use_case(title=" ", surfaces=[])
    del entry["actor"]
    del entry["claims"]
    problems = _static(_repo(tmp_path, [entry]))
    assert "UC-DEMO-001: 缺少字段 title 或为空" in problems
    assert "UC-DEMO-001: 缺少字段 actor 或为空" in problems
    assert "UC-DEMO-001: 缺少字段 surfaces 或为空" in problems
    assert "UC-DEMO-001: 缺少字段 claims 或类型不对" in problems


def test_an_unknown_surface_fails(tmp_path: Path) -> None:
    problems = _static(_repo(tmp_path, [_use_case(surfaces=["cli", "web"])]))
    assert problems == ["UC-DEMO-001: surfaces 中有未知取值 ['web']"]


def test_a_tier_both_required_and_omitted_fails(tmp_path: Path) -> None:
    omitted = {**OMIT_ALL_BUT_UNIT, "unit": "其实不需要"}
    problems = _static(_repo(tmp_path, [_use_case(omittedTiers=omitted)]))
    assert problems == ["UC-DEMO-001: 层级 ['unit'] 同时列在 requiredTiers 与 omittedTiers"]


def test_an_unknown_or_unexplained_tier_fails(tmp_path: Path) -> None:
    omitted = {**OMIT_ALL_BUT_UNIT, "smoke": "x", "live": " "}
    entry = _use_case(requiredTiers=["unit", "e2e"], omittedTiers=omitted)
    problems = _static(_repo(tmp_path, [entry]))
    assert "UC-DEMO-001: 未知层级 ['e2e']" in problems
    assert "UC-DEMO-001: omittedTiers 中有未知层级 ['smoke']" in problems
    assert "UC-DEMO-001: 层级 live 未列入 requiredTiers，需在 omittedTiers 写明理由" in problems


def test_a_malformed_or_duplicated_id_fails(tmp_path: Path) -> None:
    entries = [_use_case(), _use_case(claims=[]), _use_case("UC-demo-1", claims=[])]
    problems = _static(_repo(tmp_path, entries))
    assert "UC-DEMO-001: id 重复" in problems
    assert "UC-demo-1: id 格式应为 UC-<AREA>-<NNN>" in problems


def test_a_bad_status_fails(tmp_path: Path) -> None:
    problems = _static(_repo(tmp_path, [_use_case(status="draft")]))
    assert "UC-DEMO-001: status 必须是 active 或 retired" in problems


def test_transition_list_must_match_dn002(tmp_path: Path) -> None:
    problems = _static(_repo(tmp_path, dn002=("T01", "T02")))
    assert "迁移清单缺少 DN002 中的编号：['T02']" in problems


def test_a_transition_without_fields_or_tiers_fails(tmp_path: Path) -> None:
    items = [
        {"id": "T01", "status": "active", "requiredTiers": []},
        {"id": "T01", "kind": "transition", "status": "maybe", "requiredTiers": ["x"]},
    ]
    problems = _static(_repo(tmp_path, items=items))
    assert "T01: 缺少字段 kind 或为空" in problems
    assert "T01: active 的迁移必须列出 requiredTiers" in problems
    assert "T01: 迁移清单中重复" in problems
    assert "T01: status 必须是 active、planned 或 retired" in problems
    assert "T01: 未知层级 ['x']" in problems


def test_surfaces_must_be_claimed_and_claims_must_exist(tmp_path: Path) -> None:
    root = _repo(tmp_path, [_use_case(claims=["cli:demo", "mcp:gone"])])
    problems = catalog.check_static(root, ["demo", "extra"], tools=["run"], mcp_tools=[])
    assert "对外表面没有被任何 active use case 认领：cli:extra" in problems
    assert "对外表面没有被任何 active use case 认领：tool:run" in problems
    assert "UC-DEMO-001: 认领的表面不存在 mcp:gone" in problems


def test_a_retired_use_case_does_not_claim(tmp_path: Path) -> None:
    problems = _static(_repo(tmp_path, [_use_case(status="retired")]))
    assert problems == ["对外表面没有被任何 active use case 认领：cli:demo"]


def test_live_needs_a_scenario_that_references_the_use_case(tmp_path: Path) -> None:
    omitted = {k: v for k, v in OMIT_ALL_BUT_UNIT.items() if k != "live"}
    entry = _use_case(requiredTiers=["unit", "live"], omittedTiers=omitted)
    problems = _static(_repo(tmp_path, [entry], scenarios={"other": ["UC-OTHER-001"]}))
    assert problems == ["UC-DEMO-001: 要求 live 层级，但没有引用它的 live 场景定义"]
    assert _static(_repo(tmp_path / "b", [entry], scenarios={"demo": ["UC-DEMO-001"]})) == []


def test_a_required_tier_without_a_passing_test_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    results = [_test("tests/unit/test_demo.py::test_t", transitions=["T01"])]
    problems, _nv, report = catalog.check_coverage(root, results, {"unit"})
    assert problems == ["UC-DEMO-001: use case 要求的 unit 层级没有通过的测试"]
    assert report["useCases"]["UC-DEMO-001"]["tiers"]["unit"] == {"status": "failed", "tests": []}


def test_a_skipped_or_failed_required_test_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    results = [
        *GOOD_RESULTS,
        _test("tests/unit/test_demo.py::test_b", "skipped", use_cases=["UC-DEMO-001"]),
    ]
    problems, _nv, report = catalog.check_coverage(root, results, {"unit"})
    assert problems == ["UC-DEMO-001: 必需的测试未通过（skipped）tests/unit/test_demo.py::test_b"]
    assert report["useCases"]["UC-DEMO-001"]["tiers"]["unit"]["status"] == "failed"


def test_unknown_retired_and_planned_references_fail(tmp_path: Path) -> None:
    entries = [_use_case(), _use_case("UC-DEMO-002", status="retired", claims=[])]
    items = [
        {"id": "T01", "kind": "transition", "status": "active", "requiredTiers": ["unit"]},
        {"id": "T02", "kind": "transition", "status": "planned", "requiredTiers": []},
    ]
    root = _repo(tmp_path, entries, items, dn002=("T01", "T02"))
    results = [
        *GOOD_RESULTS,
        _test("t::x", use_cases=["UC-NOPE-001", "UC-DEMO-002"], transitions=["T09", "T02"]),
    ]
    problems, _nv, _report = catalog.check_coverage(root, results, {"unit"})
    assert problems == [
        "t::x: 引用了未知的 use case UC-NOPE-001",
        "t::x: 引用了已退役的 use case UC-DEMO-002",
        "t::x: 引用了未知的迁移 T09",
        "t::x: 引用了非 active 的迁移 T02",
    ]


def test_a_tier_that_did_not_run_is_not_verified_and_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    problems, not_verified, report = catalog.check_coverage(root, [], set())
    assert problems == []
    assert not_verified == ["UC-DEMO-001: 本次未运行 unit 层级", "T01: 本次未运行 unit 层级"]
    assert report["transitions"]["T01"]["tiers"]["unit"] == {"status": "not_run"}


def test_the_coverage_report_lists_the_tests_of_each_tier(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _problems, _nv, report = catalog.check_coverage(root, GOOD_RESULTS, {"unit"})
    assert report == {
        "tiersRun": ["unit"],
        "useCases": {
            "UC-DEMO-001": {
                "requiredTiers": ["unit"],
                "tiers": {
                    "unit": {
                        "status": "passed",
                        "tests": [
                            {"nodeid": "tests/unit/test_demo.py::test_a", "outcome": "passed"}
                        ],
                    }
                },
            }
        },
        "transitions": {
            "T01": {
                "requiredTiers": ["unit"],
                "tiers": {
                    "unit": {
                        "status": "passed",
                        "tests": [
                            {"nodeid": "tests/unit/test_demo.py::test_t", "outcome": "passed"}
                        ],
                    }
                },
            }
        },
    }
