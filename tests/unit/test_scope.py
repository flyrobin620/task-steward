"""CI's scope (tools/scope.py, tools/scope.json): which stages a change selects."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tools.gate import STAGE_JOBS, STAGE_STEPS
from tools.scope import STAGES, glob_regex, load_map, resolve, validate_map

ALL = ["lint", "docs", "test"]
DOCS_ONLY = ["lint", "docs"]


@pytest.fixture(scope="module")
def repo_map() -> dict[str, Any]:
    return load_map()


def test_the_repository_map_is_valid(repo_map: dict[str, Any]) -> None:
    assert validate_map(repo_map) == []
    assert repo_map["always"] == DOCS_ONLY


@pytest.mark.parametrize(
    "path",
    [
        "docs/design/DN006-plugin-layer.md",
        "docs/design/DN006-plugin-layer.html",
        "docs/design/claude-code/CC006-plugins.md",
        "docs/design/diagrams/task-state.svg",
        "docs/development/ci.md",
        "docs/decisions/0001-development-process.md",
        "README.md",
        "README.html",
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        ".github/pull_request_template.md",
        ".github/copilot-instructions.md",
        ".github/dependabot.yml",
    ],
)
def test_documentation_and_governance_run_only_lint_and_docs(
    repo_map: dict[str, Any], path: str
) -> None:
    plan = resolve(repo_map, [path])
    assert plan["stages"] == DOCS_ONLY
    assert plan["unmapped"] == []


@pytest.mark.parametrize(
    "path",
    [
        "tsw/cli.py",
        "plugin/hooks/hooks.json",
        ".claude-plugin/marketplace.json",
        "setup.ps1",
        "tests/unit/test_x.py",
        "tools/gate.py",
        "tools/scope.py",
        "tools/scope.json",
        "tools/live/scenarios/host-e2e.json",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "requirements-dev.txt",
        ".gitattributes",
        ".gitignore",
    ],
)
def test_runtime_tests_tools_and_pipeline_select_every_stage(
    repo_map: dict[str, Any], path: str
) -> None:
    assert resolve(repo_map, [path])["stages"] == ALL


@pytest.mark.parametrize(
    "path",
    [
        "docs/use-cases/catalog.json",
        "docs/use-cases/transitions.json",
        "docs/design/DN002-task-state-machine.md",
    ],
)
def test_inputs_of_the_catalog_check_and_the_tests_select_the_test_stage(
    repo_map: dict[str, Any], path: str
) -> None:
    plan = resolve(repo_map, [path])
    assert "test" in plan["stages"]
    assert "inputs of the tests and the catalog check" in plan["components"]


def test_a_path_no_component_owns_is_reported(repo_map: dict[str, Any]) -> None:
    plan = resolve(repo_map, ["docs/a.md", "stray.txt", "other/x.py"])
    assert plan["unmapped"] == ["stray.txt", "other/x.py"]
    assert plan["stages"] == DOCS_ONLY


def test_force_all_selects_every_stage_in_order(repo_map: dict[str, Any]) -> None:
    plan = resolve(repo_map, ["docs/a.md"], all_reason="label ci:all")
    assert plan["stages"] == ALL
    assert "ci:all" in plan["reason"]
    assert resolve(repo_map, [], all_reason="push to main")["stages"] == ALL


def test_no_change_runs_the_always_stages(repo_map: dict[str, Any]) -> None:
    plan = resolve(repo_map, [])
    assert plan == {
        "stages": DOCS_ONLY,
        "reason": "没有改动任何组件",
        "components": [],
        "unmapped": [],
        "changed": 0,
    }


def test_globs() -> None:
    assert glob_regex("docs/**").match("docs/a/b/c.md")
    assert glob_regex("**/AGENTS.md").match("AGENTS.md")
    assert glob_regex("**/AGENTS.md").match("tsw/host/AGENTS.md")
    assert not glob_regex("tools/*.py").match("tools/live/x.py")
    assert glob_regex("tools/?.py").match("tools/a.py")
    assert not glob_regex("README.md").match("READMExmd")


def test_a_map_without_the_test_stage_is_refused(repo_map: dict[str, Any]) -> None:
    broken = copy.deepcopy(repo_map)
    del broken["stages"]["test"]
    for component in broken["components"]:
        component["stages"] = [s for s in component["stages"] if s != "test"]
    problems = validate_map(broken)
    assert len(problems) == 1
    assert problems[0].startswith("阶段必须恰好是 ['lint', 'docs', 'test']")
    assert problems[0].endswith("实际为 ['lint', 'docs']")


def test_scope_and_gate_know_the_same_stages() -> None:
    assert tuple(STAGE_STEPS) == STAGES
    assert tuple(STAGE_JOBS) == STAGES


def test_an_invalid_map_is_refused(repo_map: dict[str, Any]) -> None:
    broken = copy.deepcopy(repo_map)
    broken["always"] = ["lint", "nope"]
    broken["components"].append({"name": "pipeline", "paths": [], "stages": ["later"]})
    problems = validate_map(broken)
    assert "always: 未知阶段 nope" in problems
    assert any("名称重复" in p for p in problems)
    assert "pipeline: 没有路径" in problems
    assert "pipeline: 未知阶段 later" in problems


def test_the_map_resolver_and_workflow_must_select_every_stage(repo_map: dict[str, Any]) -> None:
    broken = copy.deepcopy(repo_map)
    for component in broken["components"]:
        if component["name"] == "pipeline":
            component["stages"] = ["lint", "docs"]
    problems = validate_map(broken)
    assert "改动 tools/scope.json 必须选中全部阶段" in problems
    assert "改动 .github/workflows/ci.yml 必须选中全部阶段" in problems
