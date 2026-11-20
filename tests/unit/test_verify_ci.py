"""The aggregate CI ``Gate`` check (``tools/gate.py --verify-ci``) on synthetic artifacts: the
plan and the stages that ran must agree both ways, and each selected stage leaves fresh evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.gate import CI_TIERS, verify_ci, verify_plan

EXPECTED = ["lint", "docs", "test-3.10", "test-3.13"]
RUN = "4242"
SHA = "abc123"


@pytest.fixture(autouse=True)
def github_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", RUN)
    monkeypatch.setenv("GITHUB_SHA", SHA)


def _summary(root: Path, name: str, steps: tuple[str, ...], **changes: Any) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    tiers = {}
    if name.startswith("test-"):
        tiers = {tier: {"collected": 3} for tier in CI_TIERS}
        for file in [f"{tier}.json" for tier in CI_TIERS] + [
            "coverage.txt",
            "catalog-coverage.json",
        ]:
            (directory / file).write_text("{}", encoding="utf-8")
    summary: dict[str, Any] = {
        "ok": True,
        "github_run": {"id": RUN},
        "steps": {step: {"status": "passed"} for step in steps},
        "tiers": tiers,
    }
    summary.update(changes)
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


def _run(root: Path, stages: list[str], **plan: Any) -> dict[str, Any]:
    """Artifacts and the ``needs`` context of a run that followed ``stages``."""
    (root / "plan").mkdir(parents=True)
    record = {"stages": stages, "head": SHA, "unmapped": [], **plan}
    (root / "plan" / "plan.json").write_text(json.dumps(record), encoding="utf-8")
    _summary(root, "lint", ("lint", "stdlib"))
    _summary(root, "docs", ("docs", "catalog-static"))
    if "test" in stages:
        for name in ("test-3.10", "test-3.13"):
            _summary(root, name, ("tests", "catalog"))
    return {
        "scope": {"result": "success", "outputs": {"stages": json.dumps(stages)}},
        "lint": {"result": "success", "outputs": {}},
        "docs": {"result": "success", "outputs": {}},
        "test": {"result": "success" if "test" in stages else "skipped", "outputs": {}},
    }


def test_a_full_run_passes(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs", "test"])
    assert verify_plan(tmp_path, needs, EXPECTED) == []
    assert verify_ci(tmp_path, json.dumps(needs), EXPECTED) == 0


def test_a_docs_only_run_with_test_skipped_passes(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"])
    assert verify_plan(tmp_path, needs, EXPECTED) == []


def test_a_stage_the_plan_did_not_select_must_be_skipped(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"])
    needs["test"]["result"] = "success"
    _summary(tmp_path, "test-3.10", ("tests", "catalog"))
    assert verify_plan(tmp_path, needs, EXPECTED) == [
        "计划没有选中的阶段 test 的结果为 success，应为 skipped",
        "计划没有选中的阶段 test 留下了证据 test-3.10",
    ]


def test_a_selected_stage_that_was_skipped_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs", "test"])
    needs["test"]["result"] = "skipped"
    problems = verify_plan(tmp_path, needs, EXPECTED)
    assert problems == ["计划选中的阶段 test 的结果为 skipped"]


def test_a_failed_scope_or_a_missing_plan_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"])
    needs["scope"]["result"] = "failure"
    (tmp_path / "plan" / "plan.json").unlink()
    assert verify_plan(tmp_path, needs, EXPECTED) == [
        "任务 scope 的结果为 failure",
        "缺少证据：plan/plan.json",
    ]


def test_a_plan_for_another_commit_or_unlike_the_one_used_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"], head="old", unmapped=["stray.txt"])
    needs["scope"]["outputs"]["stages"] = json.dumps(["lint", "docs", "test"])
    assert verify_plan(tmp_path, needs, EXPECTED) == [
        f"计划不是为本次提交生成的：old ≠ {SHA}",
        "计划中有不属于任何组件的路径：['stray.txt']",
        "记录的计划 ['lint', 'docs'] 与各阶段使用的计划 ['lint', 'docs', 'test'] 不一致",
    ]


def test_an_unknown_stage_in_the_plan_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs", "deploy"])
    assert "计划中有未知阶段 ['deploy']" in verify_plan(tmp_path, needs, EXPECTED)


def test_stale_failing_or_incomplete_evidence_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs", "test"])
    _summary(tmp_path, "lint", ("lint",), github_run={"id": "1"})
    _summary(tmp_path, "docs", ("docs",), ok=False)
    _summary(tmp_path, "test-3.10", ("tests", "catalog"), tiers={"unit": {"collected": 0}})
    (tmp_path / "test-3.13" / "catalog-coverage.json").unlink()
    assert verify_plan(tmp_path, needs, EXPECTED) == [
        "lint: 证据不是本次运行生成的",
        "lint: 步骤 stdlib 没有通过",
        "docs: 门禁未通过",
        "docs: 步骤 catalog-static 没有通过",
        "test-3.10: unit 层级没有发现测试",
        "test-3.10: 测试层级不完整 ['unit']",
        "test-3.13: 缺少证据文件 catalog-coverage.json",
    ]


def test_missing_evidence_of_a_selected_stage_fails(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"])
    (tmp_path / "docs" / "summary.json").unlink()
    assert verify_plan(tmp_path, needs, EXPECTED) == ["缺少证据：docs/summary.json"]
    assert verify_ci(tmp_path, json.dumps(needs), EXPECTED) == 1


def test_another_needed_job_must_succeed(tmp_path: Path) -> None:
    needs = _run(tmp_path, ["lint", "docs"])
    needs["extra"] = {"result": "cancelled"}
    assert verify_plan(tmp_path, needs, EXPECTED) == ["任务 extra 的结果为 cancelled"]
