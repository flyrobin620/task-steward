"""The ``tsw`` CLI as a separate process: exit codes, stdout and stderr are asserted separately."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import child_env, run_json, run_tsw
from tsw import __version__
from tsw.cli import iter_commands


@pytest.fixture
def initialized() -> Path:
    code, out = run_json("init")
    assert code == 0, out
    assert out["created"] is True
    return Path(out["home"])


@pytest.mark.use_case("UC-HOME-001")
def test_init_and_home(initialized: Path) -> None:
    assert initialized == Path(os.environ["TSW_HOME"])
    code, again = run_json("init")
    assert (code, again["created"]) == (0, False)
    code, info = run_json("home")
    assert code == 0
    assert {k: info[k] for k in ("ok", "home", "initialized", "source")} == {
        "ok": True,
        "home": str(initialized),
        "initialized": True,
        "source": "TSW_HOME",
    }


@pytest.mark.use_case("UC-HOME-001")
def test_global_options_work_before_and_after_the_command(initialized: Path) -> None:
    proc = run_tsw("--json", "--home", str(initialized), "home")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["home"] == str(initialized)
    assert proc.stderr == ""


@pytest.mark.use_case("UC-HOME-001")
def test_version_and_usage_errors() -> None:
    proc = run_tsw("--version")
    assert (proc.returncode, proc.stdout.strip()) == (0, f"tsw {__version__}")
    proc = run_tsw("task", "register", "--title", "x")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "required" in proc.stderr


@pytest.mark.use_case("UC-HOME-001")
def test_commands_on_an_uninitialized_home_are_rejected() -> None:
    code, err = run_json("task", "list")
    assert code == 3
    assert err["code"] == "REJECTED"


@pytest.mark.use_case("UC-TASK-001")
@pytest.mark.transition("T01")
def test_register_list_show(initialized: Path) -> None:
    args = ["--title", "导出 CSV", "--repo", "demo", "--kind", "code", "--goal", "g"]
    code, out = run_json("task", "register", *args, "--acceptance", "a", "--command-id", "c1")
    assert (code, out["task_id"], out["duplicate"]) == (0, "T001", False)
    code, out = run_json("task", "register", *args, "--acceptance", "a", "--command-id", "c1")
    assert (code, out["duplicate"]) == (0, True)
    code, err = run_json("task", "register", *args, "--acceptance", "b", "--command-id", "c1")
    assert (code, err["details"]["reason"]) == (3, "command_conflict")
    code, listing = run_json("task", "list")
    assert [t["task_id"] for t in listing["tasks"]] == ["T001"]
    code, shown = run_json("task", "show", "T001")
    assert shown["task"]["title"] == "导出 CSV"
    assert (initialized / "todo" / "T001.md").is_file()


@pytest.mark.use_case("UC-TASK-001")
@pytest.mark.transition("T01")
def test_register_rejects_an_incomplete_spec(initialized: Path) -> None:
    proc = run_tsw(
        "task", "register", "--title", "x", "--repo", "demo", "--kind", "code", "--goal", " "
    )
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert proc.stderr.startswith("错误 [REJECTED] 任务书缺少必填项")


@pytest.mark.use_case("UC-TASK-002")
def test_note_with_expected_version(initialized: Path) -> None:
    run_json(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "research",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    code, out = run_json("task", "note", "T001", "--text", "hi", "--expected-version", "1")
    assert (code, out["sequence"]) == (0, 2)
    code, err = run_json("task", "note", "T001", "--text", "hi", "--expected-version", "1")
    assert (code, err["details"]["reason"]) == (3, "version_conflict")


@pytest.mark.use_case("UC-LEDGER-002")
def test_tail_and_audit(initialized: Path) -> None:
    code, tail = run_json("ledger", "tail")
    assert (code, tail["sequence"]) == (0, 0)
    run_json(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "design",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    code, report = run_json("ledger", "audit")
    assert (code, report["ok"], report["events"]) == (0, True, 1)
    segment = next((initialized / "ledger" / "events").glob("*.jsonl"))
    segment.write_bytes(segment.read_bytes().replace(b'"t"', b'"T"'))
    proc = run_tsw("ledger", "audit", "--json")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["ok"] is False


@pytest.mark.use_case("UC-LEDGER-003")
def test_rebuild(initialized: Path) -> None:
    run_json(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "code",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    (initialized / "cache" / "state.sqlite").unlink()
    code, out = run_json("ledger", "rebuild")
    assert (code, out["projected_seq"]) == (0, 1)


@pytest.mark.use_case("UC-STORE-001")
def test_quiesce_backup_restore(initialized: Path, tmp_path: Path) -> None:
    run_json(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "code",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    code, out = run_json("quiesce")
    assert (code, out["quiesced"]) == (0, True)
    code, err = run_json("task", "note", "T001", "--text", "x")
    assert (code, err["code"]) == (4, "QUIESCED")
    code, _ = run_json("resume-writes")
    assert code == 0
    code, out = run_json("backup", str(tmp_path / "bk"))
    assert (code, out["ledger_tail"]["sequence"]) == (0, 1)
    env = child_env(TSW_HOME=str(tmp_path / "restored"))
    code, out = run_json("restore", str(tmp_path / "bk"), env=env)
    assert (code, out["projected_seq"]) == (0, 1)
    code, listing = run_json("task", "list", env=env)
    assert [t["task_id"] for t in listing["tasks"]] == ["T001"]


@pytest.mark.use_case("UC-TASK-001")
def test_human_output_is_readable(initialized: Path) -> None:
    proc = run_tsw(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "code",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    assert proc.returncode == 0
    assert "task_id: T001" in proc.stdout.splitlines()


def test_command_inventory_is_known() -> None:
    assert iter_commands() == sorted(
        [
            "backup",
            "config show",
            "config validate",
            "guard",
            "home",
            "host install-hint",
            "host render",
            "import orchestrator-agent",
            "init",
            "ledger audit",
            "ledger rebuild",
            "ledger tail",
            "lease acquire",
            "lease release",
            "lease show",
            "lease takeover",
            "mcp",
            "quiesce",
            "restore",
            "resume-writes",
            "review run",
            "status serve",
            "task accept",
            "task cancel",
            "task list",
            "task note",
            "task register",
            "task reject",
            "task show",
            "tool list",
            "tool run",
        ]
    )
